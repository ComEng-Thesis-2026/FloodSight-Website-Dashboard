#!/usr/bin/env python3
"""
FloodMonitoring.py  -  First Integration
=========================================
Thesis: "Development of a CCTV-Based Flood Monitoring and Routing Advisory
         System Using Computer Vision"

What this program does
----------------------
1. Reads a live video feed: Dahua CCTV (RTSP), laptop webcam, Iriun phone
   camera, or a recorded video file  (see CAMERA SETTINGS below).
2. Uses TWO user-adjustable regions:
     * FLOOD ROI  (cyan polygon)   - the road area where flood water is detected.
     * MARKER ROI (magenta 4-corner shape) - placed by the user on a real
       vertical surface (post, wall, curb, pillar). Its bottom edge is the
       road surface (0 m) and its top edge is a known height (REF HEIGHT).
3. Automatic perspective compensation (camera angle / height / distance):
   the 4 marker corners define a planar homography between the image and the
   real marker surface. Every level of the virtual marker is placed with that
   homography, so 0.10 m near the top of the marker is drawn shorter than
   0.10 m near the bottom when the camera looks down at it - exactly as a real
   staff gauge would look. Water levels are measured in a rectified
   (perspective-free) view of the marker where 1 row = 2.5 mm.
   The program also estimates camera tilt, camera height and distance.
4. Flood detection: comparison with a dry reference frame (CIE-Lab), a flood
   COLOR check (the flood water here is BROWN / muddy or DARK GREEN, so grey
   shadows, wet asphalt, glare and colorful clothes / cars are not water),
   temporal smoothing, moving-object (car / person) removal, flood coverage %
   of the road, and sub-row water-line refinement.
5. A Kalman filter smooths the level and rejects sudden spikes (vehicles,
   people) as anomalies / obstructions. A sudden jump with nothing moving at
   the marker is confirmed as real water within seconds (steady readings,
   backed by the road flood coverage); a jump that came with a car / person
   needs --persist-s seconds.
6. Flood marker levels:
       GREEN   0.00 - 0.30 m   SAFE
       YELLOW  0.30 - 0.50 m   ALERT
       RED     0.50 - 1.00 m   DANGER
7. Prints the live data feed in the terminal (text, or JSON lines with --json).
8. Marker-free operation (the physical pole is only needed during setup):
     * The pole (MARKER ROI in "physical" mode) + the camera height (and the
       camera-to-pole distance) solve the camera pose: tilt, roll, focal length.
     * 't' then click the FOOT of a permanent post / wall: a VIRTUAL marker of
       the correct size and perspective is placed there from the pose. The
       water line is measured on that permanent surface, so the pole can go.
       (The water line needs something vertical: on open road the water seen
       behind an empty spot does not show the depth.)
     * Removing the pole is detected automatically (or press 'x'): the dry
       reference is patched where the pole stood and the setup is saved.
     * Camera-shift compensation: every frame is aligned to the saved anchor
       frame (ORB features on the static scene), so wind, vibration or a small
       PTZ / stream change does not move the virtual marker off its surface.

Quick start
-----------
  Choose the camera in the "CAMERA SETTINGS" block below (CAMERA_MODE), then:
      python FloodMonitoring.py
  Or:  --camera laptop | --camera iriun | --camera dahua | --source clip.mp4
  Find the camera indexes on this PC:  --list-cameras

Setup in the video window (default ROIs are created if none exist)
------------------------------------------------------------------
  f : draw a new FLOOD ROI rectangle (ENTER) - then drag its vertices
  m : draw a new MARKER ROI rectangle (ENTER) - then drag its 4 corners onto
      the edges of the real post / wall. Bottom edge = road surface (0 m),
      top edge = REF HEIGHT.
  [ / ] : REF HEIGHT  -/+ 0.05 m        , / . : REF HEIGHT  -/+ 0.01 m
  drag a square handle         : move a vertex / corner
  drag inside a region         : move the whole region
  right-click FLOOD ROI edge   : add a vertex     right-click vertex : remove it
  b : capture the DRY reference frame (road NOT flooded and empty)
  t : then click the foot of a permanent post / wall -> virtual marker there
      (drag it to move it, - / = : virtual marker width)
  x : the physical marker (pole) has been removed
  s : save setup        w : water overlay on/off     v : mask debug view
  p : pause             q / ESC : quit
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime

import cv2
import numpy as np

LOG = logging.getLogger("FloodMonitoring")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================================================
#  CAMERA SETTINGS  -  SWITCH THE CAMERA HERE
# ==========================================================================
#  CAMERA_MODE options:
#    "laptop" : built-in laptop webcam                   (testing)
#    "iriun"  : phone camera through the Iriun Webcam app (testing)
#    "dahua"  : Dahua CCTV camera over RTSP              (deployment)
#    "file"   : recorded video file (VIDEO_FILE)         (testing)
#  The command line can override it, e.g.  --camera iriun
#  Run  --list-cameras  to see which index each camera uses on this PC.
CAMERA_MODE = "dahua"

LAPTOP_CAMERA_INDEX = 0     # laptop webcam ("USB2.0 VGA UVC WebCam" on this PC)
IRIUN_CAMERA_INDEX = 1      # "Iriun Webcam" virtual camera (open the Iriun app on PC + phone first)
DEVICE_RESOLUTION = None    # e.g. (1280, 720) to request a resolution; None = camera default

DAHUA_IP = "192.168.1.108"  # Dahua camera / NVR address
DAHUA_USER = "admin"
DAHUA_PASSWORD = "admin1234"         # leave empty and set env var DAHUA_PASSWORD instead (safer)
DAHUA_PORT = 554
DAHUA_CHANNEL = 1
DAHUA_SUBTYPE = 0           # 0 = main stream, 1 = sub stream (lighter)

VIDEO_FILE = ""             # used when CAMERA_MODE = "file"

# Horizontal field of view of the camera lens, ONLY used for the camera
# tilt / height / distance estimates shown on screen (the water level itself
# does not depend on it). Laptop webcam ~ 60-70 deg, Dahua 2.8 mm ~ 100 deg.
CAMERA_HFOV_DEG = 70.0

# Camera mounting, used to turn the PHYSICAL marker (freestanding pole) into a
# camera calibration so the VIRTUAL marker can be moved onto a permanent post /
# wall and the pole can be removed. Saved per site in the setup file; the
# command line (--camera-height / --marker-distance) overrides these.
CAMERA_HEIGHT_M = 4.0       # camera lens height above the road (test site: 4 m)
MARKER_DISTANCE_M = 7.0     # horizontal distance camera -> pole (test site: 7 m).
                            # None = use CAMERA_HFOV_DEG instead of solving the focal length
# ==========================================================================

# --------------------------------------------------------------------------
# Flood marker definition
# --------------------------------------------------------------------------
MARKER_MAX_M = 1.0
# (from_m, to_m, status, BGR color, hex color)
FLOOD_BANDS = [
    (0.00, 0.30, "SAFE",   (0, 200, 0),   "#22c55e"),
    (0.30, 0.50, "ALERT",  (0, 220, 255), "#facc15"),
    (0.50, 1.00, "DANGER", (0, 0, 255),   "#ef4444"),
]
STATUS_ORDER = ["SAFE", "ALERT", "DANGER"]
STATUS_BGR = {b[2]: b[3] for b in FLOOD_BANDS}
STATUS_HEX = {b[2]: b[4] for b in FLOOD_BANDS}
STATUS_COLOR_NAME = {"SAFE": "GREEN", "ALERT": "YELLOW", "DANGER": "RED"}

RECT_PPM = 400      # rectified marker: rows per meter (1 row = 2.5 mm)
RECT_W = 48         # rectified marker: columns across the marker

FLOOD_COLOR = (255, 200, 0)     # cyan  (BGR) - flood ROI
MARKER_COLOR = (255, 0, 255)    # magenta     - marker ROI


def classify_level(level_m: float) -> str:
    if level_m < 0.30:
        return "SAFE"
    if level_m < 0.50:
        return "ALERT"
    return "DANGER"


class StatusTracker:
    """Flood status with a small hysteresis so it does not flicker at a boundary."""

    def __init__(self, hysteresis_m: float = 0.02):
        self.h = hysteresis_m
        self.status = "SAFE"

    def update(self, level_m: float) -> str:
        cur = STATUS_ORDER.index(self.status)
        up = STATUS_ORDER.index(classify_level(level_m))
        if up > cur:
            self.status = STATUS_ORDER[up]
        else:
            down = STATUS_ORDER.index(classify_level(level_m + self.h))
            if down < cur:
                self.status = STATUS_ORDER[down]
        return self.status


# --------------------------------------------------------------------------
# Calibration: flood ROI polygon + marker ROI (4 corners) + reference height
# --------------------------------------------------------------------------
def rect_to_polygon(x, y, w, h) -> list:
    return [[int(x), int(y)], [int(x + w), int(y)], [int(x + w), int(y + h)], [int(x), int(y + h)]]


def normalize_polygon(pts, as_int: bool = True) -> list | None:
    """Accept [x,y,w,h], a flat [x1,y1,x2,y2,...] list, or [[x,y], ...]; return [[x,y], ...]."""
    if not pts:
        return None
    conv = (lambda v: int(round(v))) if as_int else (lambda v: round(float(v), 2))
    if isinstance(pts[0], (list, tuple)):
        return [[conv(p[0]), conv(p[1])] for p in pts]
    if len(pts) == 4:
        return rect_to_polygon(*pts)
    if len(pts) >= 6 and len(pts) % 2 == 0:
        return [[conv(pts[i]), conv(pts[i + 1])] for i in range(0, len(pts), 2)]
    raise ValueError(f"Invalid polygon: {pts}")


def order_quad(pts) -> np.ndarray:
    """Return the 4 corners as [bottom-left, bottom-right, top-right, top-left]."""
    p = np.asarray(pts, np.float64).reshape(4, 2)
    center = p.mean(axis=0)
    cycle = p[np.argsort(np.arctan2(p[:, 1] - center[1], p[:, 0] - center[0]))]
    contour = cycle.astype(np.float32).reshape(-1, 1, 2)
    if cv2.isContourConvex(contour):
        edge_lengths = np.linalg.norm(np.roll(cycle, -1, axis=0) - cycle, axis=1)
        pair_a = edge_lengths[0] + edge_lengths[2]
        pair_b = edge_lengths[1] + edge_lengths[3]
        short_edges = (0, 2) if pair_a <= pair_b else (1, 3)
        edge_centers = [(cycle[i] + cycle[(i + 1) % 4]) / 2 for i in range(4)]
        top_edge, bottom_edge = sorted(short_edges, key=lambda i: edge_centers[i][1])

        def left_to_right(edge):
            endpoints = cycle[[edge, (edge + 1) % 4]]
            return endpoints[np.argsort(endpoints[:, 0])]

        tl, tr = left_to_right(top_edge)
        bl, br = left_to_right(bottom_edge)
        return np.array([bl, br, tr, tl])

    idx = np.argsort(p[:, 1])
    top, bottom = p[idx[:2]], p[idx[2:]]
    tl, tr = top[np.argsort(top[:, 0])]
    bl, br = bottom[np.argsort(bottom[:, 0])]
    return np.array([bl, br, tr, tl])


def marker_quad_from_line(base, top, width_frac=0.12) -> list:
    """Old 2-point marker -> 4-corner marker ROI around that line."""
    b, t = np.asarray(base, float), np.asarray(top, float)
    v = t - b
    n = np.array([-v[1], v[0]]) / max(np.linalg.norm(v), 1e-6)
    half = max(6.0, np.linalg.norm(v) * width_frac / 2)
    return [list(map(int, np.round(q))) for q in (b - n * half, b + n * half, t + n * half, t - n * half)]


@dataclass
class Calibration:
    camera_id: str = "CAM-01"
    frame_size: list | None = None      # [w, h] the coordinates below refer to
    flood_roi: list | None = None       # polygon [[x, y], ...] (>= 3 vertices)
    marker_roi: list | None = None      # 4 corners: bottom edge = road (0 m), top edge = ref_height_m
    ref_height_m: float = 1.0           # real height of the marker ROI's top edge above the road
    marker_width_m: float | None = None  # optional real width of the marker surface (distance estimate)
    reference_image: str | None = None  # dry reference frame (png), daylight
    reference_image_night: str | None = None  # dry reference frame (png) for night footage
    # ---- marker-free operation ----
    marker_mode: str = "physical"       # "physical": marker_roi = the pole; "virtual": derived from the pose
    pole_roi: list | None = None        # 4 corners of the physical pole used for the calibration
    pole_present: bool = True           # False once the pole has been removed
    camera_height_m: float | None = None
    marker_distance_m: float | None = None   # horizontal camera -> pole distance (solves the focal length)
    camera_pose: dict | None = None     # solved from the pole (see CameraPose)
    virtual_base: list | None = None    # [X, Y] ground position (m) of the virtual marker
    virtual_width_m: float = 0.10       # width of the virtual marker strip on the post / wall
    virtual_base_elev_m: float = 0.0    # height of the clicked foot above the road (e.g. a curb)
    anchor_image: str | None = None     # anchor frame for camera-shift compensation (png)
    is_default: bool = field(default=False, compare=False)   # auto-created, not saved yet

    def __post_init__(self):
        self.flood_roi = normalize_polygon(self.flood_roi)
        if self.marker_roi is not None:     # sub-pixel corners keep the homography precise
            self.marker_roi = normalize_polygon(self.marker_roi, as_int=False)
        if self.pole_roi is not None:
            self.pole_roi = normalize_polygon(self.pole_roi, as_int=False)
        if self.marker_mode not in ("physical", "virtual"):
            self.marker_mode = "physical"

    @property
    def ready(self) -> bool:
        return bool(self.flood_roi and len(self.flood_roi) >= 3
                    and self.marker_roi and len(self.marker_roi) == 4)

    @classmethod
    def load(cls, path: str) -> "Calibration":
        if not os.path.isfile(path):
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # older setup files: "roi" + 2-point marker
        if "flood_roi" not in data and data.get("roi"):
            data["flood_roi"] = data["roi"]
        if "marker_roi" not in data and data.get("marker_base") and data.get("marker_top"):
            data["marker_roi"] = marker_quad_from_line(data["marker_base"], data["marker_top"])
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "is_default"}
        return cls(**known)

    def save(self, path: str) -> None:
        data = asdict(self)
        data.pop("is_default", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.is_default = False
        LOG.info("Setup saved to %s", path)

    def scaled_to(self, w: int, h: int) -> "Calibration":
        """Return a copy whose pixel coordinates match a frame of size w x h."""
        c = Calibration(**asdict(self))
        if not self.frame_size or list(self.frame_size) == [w, h]:
            c.frame_size = [w, h]
            return c
        sx, sy = w / self.frame_size[0], h / self.frame_size[1]
        if c.flood_roi:
            c.flood_roi = [[int(round(px * sx)), int(round(py * sy))] for px, py in c.flood_roi]
        if c.marker_roi:
            c.marker_roi = [[round(px * sx, 2), round(py * sy, 2)] for px, py in c.marker_roi]
        if c.pole_roi:
            c.pole_roi = [[round(px * sx, 2), round(py * sy, 2)] for px, py in c.pole_roi]
        c.frame_size = [w, h]           # camera_pose is stored size-independent (focal / width)
        return c

    def fill_defaults(self, w: int, h: int) -> bool:
        """Create default ROIs when none exist so the program can start right away."""
        made = False
        if not self.flood_roi:
            self.flood_roi = rect_to_polygon(int(w * 0.10), int(h * 0.55), int(w * 0.80), int(h * 0.40))
            made = True
        if not self.marker_roi:
            mw = max(24, int(w * 0.06))
            x0, yb, yt = w // 2 - mw // 2, int(h * 0.90), int(h * 0.40)
            self.marker_roi = [[x0, yb], [x0 + mw, yb], [x0 + mw, yt], [x0, yt]]
            made = True
        if made:
            self.is_default = True
        return made


# --------------------------------------------------------------------------
# Virtual marker geometry: homography = automatic perspective compensation
# --------------------------------------------------------------------------
class MarkerGeometry:
    """
    Maps between the image and the real (vertical) marker surface.

    The 4 marker corners are assumed to be the corners of a real rectangle on
    a vertical surface: the bottom edge on the road (0 m) and the top edge at
    `ref_height_m`. The homography between that rectangle and the image
    compensates for the camera's tilt (angle), mounting height and distance:
      * to_img(s, h)  : real position (s = 0..1 across, h meters up) -> pixel
      * to_height(x,y): pixel -> real height in meters
    The rectified view (RECT_W x rows, RECT_PPM rows per meter) is a
    perspective-free image of the marker in which the water line is measured.
    """

    def __init__(self, quad, ref_height_m: float):
        self.quad = order_quad(quad)                     # bl, br, tr, tl
        self.ref_h = float(ref_height_m)
        self.max_m = max(MARKER_MAX_M * 1.1, self.ref_h * 1.02)
        self.rows = int(round(self.max_m * RECT_PPM))
        self.w = RECT_W
        yb, yt = float(self.rows), self.rows - self.ref_h * RECT_PPM
        dst = np.float32([[0, yb], [self.w, yb], [self.w, yt], [0, yt]])
        self.M_img2rect = cv2.getPerspectiveTransform(self.quad.astype(np.float32), dst).astype(np.float64)
        self.M_rect2img = np.linalg.inv(self.M_img2rect)
        q = self.quad.astype(np.float32).reshape(-1, 1, 2)
        self.convex = bool(cv2.isContourConvex(q)) and cv2.contourArea(q) > 50

    def to_img(self, s: float, h: float) -> np.ndarray:
        v = self.M_rect2img @ np.array([s * self.w, self.rows - h * RECT_PPM, 1.0])
        return v[:2] / v[2]

    def to_height(self, x: float, y: float) -> float:
        v = self.M_img2rect @ np.array([x, y, 1.0])
        return float((self.rows - v[1] / v[2]) / RECT_PPM)

    def px_per_m(self, h0: float, h1: float) -> float:
        return float(np.linalg.norm(self.to_img(0.5, h1) - self.to_img(0.5, h0)) / (h1 - h0))

    def _pose(self, f: float, cy: float):
        """Camera tilt (deg, + = looking down) and height (m) from the vertical vanishing point."""
        bl, br, tr, tl = (np.append(p, 1.0) for p in self.quad)
        vp = np.cross(np.cross(bl, tl), np.cross(br, tr))       # vertical vanishing point
        tilt = 0.0
        if abs(vp[2]) > 1e-12:
            d = vp[1] / vp[2] - cy
            if abs(d) < 1e7:
                tilt = math.degrees(math.atan(f / d)) if d > 0 else -math.degrees(math.atan(f / -d))
        y_h = cy - f * math.tan(math.radians(tilt))               # horizon line
        cam_h = None
        a, b = self.to_img(0.5, 0.0), self.to_img(0.5, 1.0)
        if abs(b[1] - a[1]) > 1e-6 and abs(tilt) > 0.5:
            p = a + (y_h - a[1]) / (b[1] - a[1]) * (b - a)
            h = self.to_height(p[0], p[1])
            if 0.2 < h < 60:
                cam_h = float(h)
        return tilt, cam_h

    def _perturbed(self, px: float):
        """Marker geometries with each corner moved by +/- px (click / pixel error)."""
        for i in range(4):
            for dx, dy in ((px, 0), (-px, 0), (0, px), (0, -px)):
                q = self.quad.copy()
                q[i] += (dx, dy)
                try:
                    yield MarkerGeometry(q, self.ref_h)
                except (cv2.error, np.linalg.LinAlgError):
                    continue

    def setup_accuracy_cm(self, px: float = 1.0) -> dict:
        """How much the measured height changes if one corner is off by `px` pixels."""
        out = {}
        for h in (0.3, 0.5, 1.0):
            p = self.to_img(0.5, h)
            errs = [abs(g.to_height(p[0], p[1]) - h) for g in self._perturbed(px)]
            out[f"{h:.1f}m"] = round(100 * max(errs), 1) if errs else None
        return out

    def perspective_info(self, frame_w: int, frame_h: int, hfov_deg: float,
                         marker_width_m: float | None = None) -> dict:
        """Scale at the bottom / top of the marker, setup accuracy and camera pose estimates."""
        info = {
            "scale_px_per_m_bottom": round(self.px_per_m(0.0, 0.1), 1),
            "scale_px_per_m_top": round(self.px_per_m(MARKER_MAX_M - 0.1, MARKER_MAX_M), 1),
        }
        info["scale_change_pct"] = round(
            100.0 * (info["scale_px_per_m_top"] / max(info["scale_px_per_m_bottom"], 1e-6) - 1), 1)
        info["setup_accuracy_cm_per_px"] = self.setup_accuracy_cm(1.0)
        f = (frame_w / 2) / math.tan(math.radians(hfov_deg) / 2)
        cy = frame_h / 2
        tilt, cam_h = self._pose(f, cy)
        # only report the pose when it is stable against half-pixel corner errors
        tilts = [g._pose(f, cy)[0] for g in self._perturbed(0.5)]
        reliable = bool(tilts) and (max(tilts) - min(tilts)) < 6.0
        info["camera_tilt_deg_est"] = round(tilt, 1) if reliable else None
        info["camera_height_m_est"] = round(cam_h, 2) if reliable and cam_h else None
        if marker_width_m:
            wpx = float(np.linalg.norm(self.quad[1] - self.quad[0]))
            info["distance_m_est"] = round(f * marker_width_m / max(wpx, 1e-6), 2)
        return info


# --------------------------------------------------------------------------
# Camera pose from the physical marker -> virtual marker anywhere on the road
# --------------------------------------------------------------------------
class CameraPose:
    """
    Pinhole camera above a flat road. World: Z up, road surface Z = 0, camera at
    (0, 0, height) looking along +Y, tilted down by `tilt` and rolled by `roll`.
    Principal point = image center.

    `solve()` finds tilt, roll and the pole's ground position (and the focal
    length when the camera-to-pole distance is known) from the pole's base and
    top in the image, its real height and the camera height. With the pose, a
    vertical gauge of the correct size and perspective can be placed at the foot
    of any post / wall on the road: `marker_quad()`.
    """

    def __init__(self, tilt_deg: float, roll_deg: float, height_m: float, focal_px: float, W: int, H: int):
        self.tilt_deg, self.roll_deg = float(tilt_deg), float(roll_deg)
        self.height = float(height_m)
        self.f = float(focal_px)
        self.W, self.H = int(W), int(H)
        self.cx, self.cy = W / 2.0, H / 2.0
        t, r = math.radians(tilt_deg), math.radians(roll_deg)
        right = np.array([1.0, 0.0, 0.0])
        down = np.array([0.0, -math.sin(t), -math.cos(t)])
        fwd = np.array([0.0, math.cos(t), -math.sin(t)])
        self.R = np.vstack([math.cos(r) * right + math.sin(r) * down,
                            -math.sin(r) * right + math.cos(r) * down, fwd])
        self.C = np.array([0.0, 0.0, self.height])

    def to_dict(self) -> dict:
        return {"tilt_deg": round(self.tilt_deg, 4), "roll_deg": round(self.roll_deg, 4),
                "height_m": self.height, "focal_norm": round(self.f / self.W, 6)}

    @classmethod
    def from_dict(cls, d: dict, W: int, H: int) -> "CameraPose":
        return cls(d["tilt_deg"], d["roll_deg"], d["height_m"], d["focal_norm"] * W, W, H)

    def project(self, P) -> np.ndarray | None:
        pc = self.R @ (np.asarray(P, np.float64) - self.C)
        if pc[2] <= 1e-6:
            return None
        return np.array([self.cx + self.f * pc[0] / pc[2], self.cy + self.f * pc[1] / pc[2]])

    def ground_point(self, u: float, v: float, z: float = 0.0) -> np.ndarray | None:
        """Pixel -> [X, Y] where its ray meets the horizontal plane at height z."""
        d = self.R.T @ np.array([(u - self.cx) / self.f, (v - self.cy) / self.f, 1.0])
        if d[2] >= -1e-9:
            return None                                        # at / above the horizon
        s = (z - self.height) / d[2]
        return (self.C + s * d)[:2] if s > 0 else None

    def marker_quad(self, X: float, Y: float, width_m: float, top_m: float) -> list | None:
        """Image corners [bl, br, tr, tl] of a vertical rectangle facing the camera, standing
        on the road at (X, Y), from 0 m (road) to top_m."""
        r = math.hypot(X, Y)
        if r < 1e-6:
            return None
        e = np.array([Y / r, -X / r, 0.0]) * width_m / 2       # horizontal, across the view
        base = np.array([X, Y, 0.0])
        up = np.array([0.0, 0.0, top_m])
        pts = [self.project(p) for p in (base - e, base + e, base + e + up, base - e + up)]
        if any(p is None for p in pts):
            return None
        return [[round(float(p[0]), 2), round(float(p[1]), 2)] for p in pts]

    @classmethod
    def solve(cls, quad, ref_h: float, height_m: float, W: int, H: int, hfov_deg: float,
              distance_m: float | None = None):
        """Pose from the pole (marker ROI). Returns (pose, info) or raises ValueError."""
        if not height_m or height_m <= ref_h:
            raise ValueError(f"camera height ({height_m} m) must be above the marker top ({ref_h} m)")
        bl, br, tr, tl = order_quad(quad)
        base, top = (bl + br) / 2, (tl + tr) / 2
        f0 = (W / 2) / math.tan(math.radians(hfov_deg) / 2)
        solve_f = bool(distance_m)
        target = np.concatenate([base, top])
        w_dist = f0 / distance_m if solve_f else 0.0           # px per m, so the residuals are comparable

        def make(p):
            f = f0 * math.exp(p[4]) if solve_f else f0
            return cls(math.degrees(p[0]), math.degrees(p[1]), height_m, f, W, H)

        def residual(p):
            pose = make(p)
            ub, ut = pose.project((p[2], p[3], 0.0)), pose.project((p[2], p[3], ref_h))
            if ub is None or ut is None:
                return np.full(5 if solve_f else 4, 1e4)
            r = np.concatenate([ub, ut]) - target
            if solve_f:
                r = np.append(r, (math.hypot(p[2], p[3]) - distance_m) * w_dist)
            return r

        # coarse start: tilt (and focal) grid, pole position from the base ray
        best, best_cost = None, np.inf
        for lf in (np.log(np.linspace(0.4, 2.5, 22)) if solve_f else [0.0]):
            for tilt in np.radians(np.arange(1.0, 89.0, 1.0)):
                p = np.array([tilt, 0.0, 0.0, 0.0] + ([lf] if solve_f else []))
                g = make(p).ground_point(*base)
                if g is None:
                    continue
                p[2:4] = g
                cost = float(np.sum(residual(p) ** 2))
                if cost < best_cost:
                    best, best_cost = p, cost
        if best is None:
            raise ValueError("the marker base is above the horizon for every camera tilt")

        # Levenberg-Marquardt refinement
        p, lam = best, 1e-3
        r = residual(p)
        cost = float(r @ r)
        for _ in range(200):
            J = np.empty((r.size, p.size))
            for j in range(p.size):
                dp = np.zeros_like(p)
                dp[j] = 1e-6 * max(1.0, abs(p[j]))
                J[:, j] = (residual(p + dp) - r) / dp[j]
            A, g = J.T @ J, J.T @ r
            step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -g)
            r_new = residual(p + step)
            c_new = float(r_new @ r_new)
            if c_new < cost:
                p, r, lam = p + step, r_new, lam / 3
                done = cost - c_new < 1e-12
                cost = c_new
                if done:
                    break
            else:
                lam *= 4
                if lam > 1e8:
                    break
        pose = make(p)
        pole_w = None
        gl, gr = pose.ground_point(*bl), pose.ground_point(*br)
        if gl is not None and gr is not None:
            pole_w = float(np.linalg.norm(gl - gr))
        info = {
            "fit_px": round(float(np.sqrt(np.mean(r[:4] ** 2))), 3),
            "distance_m": round(math.hypot(p[2], p[3]), 3),
            "focal_px": round(pose.f, 1),
            "hfov_deg": round(math.degrees(2 * math.atan(W / 2 / pose.f)), 1),
            "pole_ground": [round(float(p[2]), 4), round(float(p[3]), 4)],
            "pole_width_m": round(pole_w, 3) if pole_w else None,
        }
        return pose, info


# --------------------------------------------------------------------------
# Camera-shift compensation (image stabilization against the anchor frame)
# --------------------------------------------------------------------------
class Stabilizer:
    """
    Keeps the analysis in the coordinates of the ANCHOR frame (the view the setup
    was made on). ORB features of the static scene (outside the flood ROI and the
    markers, where water and the pole change the picture) are matched to the
    anchor; a RANSAC similarity transform (shift + rotation + zoom) maps the anchor
    onto the live frame, and every frame is warped back with it. The ROIs, the
    virtual marker and the dry reference therefore stay on the same real spots
    when the camera moves a little (wind, vibration, PTZ preset, stream crop).
    """

    def __init__(self, every: int = 10, work_w: int = 960):
        self.every = max(1, int(every))
        self.work_w = work_w
        self.orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.anchor = None
        self.A = None                # 2x3, anchor -> live frame (None = identity)
        self.n = 0
        self.ok = None               # result of the last estimate
        self.inliers = 0
        self.lost_since = None
        self.reset_bg = False        # the applied transform jumped -> background model must restart

    @property
    def ready(self) -> bool:
        return self.anchor is not None

    def _small(self, gray):
        s = self.work_w / gray.shape[1]
        return (cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray), min(s, 1.0)

    def _mask(self, shape, polys, s):
        m = np.full(shape, 255, np.uint8)
        for poly in polys:
            if poly:
                cv2.fillPoly(m, [np.round(np.asarray(poly, np.float64) * s).astype(np.int32)], 0)
        return cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))

    def set_anchor(self, frame_bgr, exclude_polys):
        """frame_bgr must be in anchor coordinates (a raw frame, or an already stabilized one)."""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        small, s = self._small(gray)
        self.mask_polys = [p for p in exclude_polys if p]
        kp, des = self.orb.detectAndCompute(small, self._mask(small.shape, self.mask_polys, s))
        self.anchor = (kp, des, s, gray.shape)
        if des is None or len(kp) < 40:
            LOG.warning("Camera-shift compensation: only %d features in the anchor frame (static scene "
                        "outside the ROIs) - alignment may be unreliable.", 0 if kp is None else len(kp))

    IDENTITY = np.float64([[1, 0, 0], [0, 1, 0]])

    def _disp(self, A, B=None) -> float:
        """Largest image-corner displacement (px) between transforms A and B (default: current)."""
        h, w = self.anchor[3]
        c = np.float64([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]]).T
        if B is None:
            B = self.A if self.A is not None else self.IDENTITY
        return float(np.abs(A @ c - B @ c).max())

    def update(self, raw_bgr, t: float):
        self.n += 1
        if self.anchor is None or (self.n - 1) % self.every:
            return
        kp_a, des_a, s, _ = self.anchor
        gray = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2GRAY)
        small, _ = self._small(gray)
        kp, des = self.orb.detectAndCompute(small, self._mask(small.shape, self.mask_polys, s))
        A, inl = None, 0
        if des is not None and des_a is not None and len(kp) >= 8:
            good = [m[0] for m in self.matcher.knnMatch(des_a, des, k=2)
                    if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
            if len(good) >= 12:
                src = np.float32([kp_a[m.queryIdx].pt for m in good])
                dst = np.float32([kp[m.trainIdx].pt for m in good])
                A_s, keep = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                        ransacReprojThreshold=2.0, maxIters=3000,
                                                        confidence=0.995)
                if A_s is not None:
                    inl = int(keep.sum())
                    if inl >= 20 and inl >= 0.2 * len(good):
                        A = A_s.astype(np.float64)
                        A[:, 2] /= s                               # back to full resolution
        self.inliers = inl
        if A is None:
            if self.ok is not False:
                LOG.warning("Camera-shift compensation lost (too few matching scene features: %d) - "
                            "keeping the last alignment.", inl)
                self.lost_since = t
            self.ok = False
            return
        if self.ok is False:
            LOG.info("Camera-shift compensation recovered (%d features).", inl)
        self.ok, self.lost_since = True, None
        d = self._disp(A)
        if d < 0.6:                                                # ignore jitter of the estimate itself
            return
        total = self._disp(A, self.IDENTITY)
        self.A = None if total < 0.3 else A
        if d > 2.0:
            self.reset_bg = True
            LOG.info("Camera moved - view realigned (shift %.1f px, rotation %.2f deg, %d features).",
                     total, math.degrees(math.atan2(A[1, 0], A[0, 0])), inl)
            if total > 0.08 * self.anchor[3][1]:
                LOG.warning("The camera view moved a lot (%.0f px) - check that the markers are still on "
                            "their real spots.", total)

    def warp(self, raw_bgr):
        if self.A is None:
            return raw_bgr
        h, w = raw_bgr.shape[:2]
        return cv2.warpAffine(raw_bgr, self.A, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def status(self) -> dict:
        if self.anchor is None:
            return {"enabled": False}
        A = self.A if self.A is not None else self.IDENTITY
        return {"enabled": True, "ok": self.ok is not False, "inliers": self.inliers,
                "dx_px": round(float(A[0, 2]), 1), "dy_px": round(float(A[1, 2]), 1),
                "rotation_deg": round(math.degrees(math.atan2(A[1, 0], A[0, 0])), 2),
                "scale": round(float(math.hypot(A[0, 0], A[1, 0])), 4)}


# --------------------------------------------------------------------------
# Detection of the physical marker (pole) being taken away
# --------------------------------------------------------------------------
class PoleWatch:
    """
    Compares the pole's upper part (above the usual water levels) in the live
    frame with the same spot in the anchor frame (taken while the pole stood
    there), with normalized cross-correlation (insensitive to brightness).
    Removal = the similarity drops ABRUPTLY (someone takes the pole) and stays
    low and steady for `hold_s` seconds while the road is dry. A slow drop
    (dusk, weather) is ignored.
    """

    PRESENT, CHECK_S = 0.5, 0.5

    def __init__(self, hold_s: float = 15.0):
        self.hold_s = hold_s
        self.M = None
        self.ref = None
        self.reset()

    def reset(self):
        self.hist = collections.deque()     # (t, ncc)
        self.low_since = None
        self.abrupt = False
        self.t_last = -1e9
        self.ncc = None

    def set(self, pole_quad, anchor_bgr) -> bool:
        self.M = self.ref = None
        self.reset()
        if not pole_quad or anchor_bgr is None:
            return False
        q = order_quad(pole_quad)
        rows = 96
        # rectified strip of the pole from 35 % to 100 % of its height
        a = q[0] + 0.35 * (q[3] - q[0])
        b = q[1] + 0.35 * (q[2] - q[1])
        src = np.float32([a, b, q[2], q[3]])
        dst = np.float32([[0, rows], [24, rows], [24, 0], [0, 0]])
        self.M = cv2.getPerspectiveTransform(src, dst)
        self.size = (24, rows)
        gray = cv2.cvtColor(anchor_bgr, cv2.COLOR_BGR2GRAY)
        if self._patch(gray, normalize=False).std() < 4.0:
            LOG.info("Physical-marker removal detection off: the marker area has no texture.")
            self.M = None
            return False
        self.ref = self._patch(gray)
        return True

    def _patch(self, gray, normalize: bool = True):
        p = cv2.warpPerspective(gray, self.M, self.size, flags=cv2.INTER_AREA).astype(np.float32)
        return (p - p.mean()) / (p.std() + 1e-6) if normalize else p

    def update(self, frame_bgr, t: float, road_dry: bool) -> bool:
        if self.ref is None or t - self.t_last < self.CHECK_S:
            return False
        self.t_last = t
        ncc = float(np.mean(self._patch(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)) * self.ref))
        self.ncc = ncc
        self.hist.append((t, ncc))
        while self.hist and t - self.hist[0][0] > 6.0:
            self.hist.popleft()
        if ncc >= self.PRESENT:
            self.low_since, self.abrupt = None, False
            return False
        if self.low_since is None:
            self.low_since = t
            self.abrupt = max(v for _, v in self.hist) - ncc > 0.3
        # the empty spot is still; a person standing in front of the pole fidgets
        steady = float(np.std([v for tt, v in self.hist if t - tt <= 5.0])) < 0.06
        return bool(self.abrupt and road_dry and steady and t - self.low_since >= self.hold_s)


# --------------------------------------------------------------------------
# Video sources: Dahua RTSP / device camera / file
# --------------------------------------------------------------------------
def dahua_rtsp_url(ip, user, password, port=554, channel=1, subtype=0) -> str:
    """Dahua RTSP format. subtype 0 = main stream, 1 = sub stream (lighter)."""
    u = urllib.parse.quote(user or "", safe="")
    p = urllib.parse.quote(password or "", safe="")
    auth = f"{u}:{p}@" if user else ""
    return f"rtsp://{auth}{ip}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"


def mask_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(":" + parts.password + "@", ":****@")
            return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    except ValueError:
        pass
    return url


class VideoSource:
    """
    Live sources (RTSP / device) are read in a background thread that keeps only
    the newest frame, so the analysis never lags behind the real scene, and the
    stream reconnects automatically if the CCTV drops.  Files are read
    sequentially with their own timestamps (deterministic, good for testing).
    """

    def __init__(self, spec, kind: str, realtime_file: bool = True, loop_file: bool = False,
                 label: str | None = None, resolution=None):
        self.spec, self.kind = spec, kind
        self.label = label or kind
        self.resolution = resolution
        self.realtime_file, self.loop_file = realtime_file, loop_file
        self.cap = None
        self._frame = None
        self._t = 0.0
        self._seq = 0
        self._last_seq = 0
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self.fps_hint = 15.0
        self._file_idx = 0
        self._wall0 = None
        if kind == "file":
            self._open()
            if not self.cap or not self.cap.isOpened():
                raise RuntimeError(f"Cannot open video file: {spec}")
        else:
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()

    @property
    def is_live(self) -> bool:
        return self.kind != "file"

    def describe(self) -> str:
        return f"{self.label} ({mask_url(str(self.spec))})"

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        if self.kind == "device":
            idx = int(self.spec)
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
            if not cap.isOpened():
                cap = cv2.VideoCapture(idx)
            if self.resolution:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        elif self.kind in ("dahua", "url"):
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            cap = cv2.VideoCapture(
                str(self.spec), cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000],
            )
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(str(self.spec))
        self.cap = cap
        if cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps and 1 <= fps <= 120:
                self.fps_hint = fps

    def _reader(self):
        backoff = 1.0
        while not self._stop.is_set():
            self._open()
            if not self.cap.isOpened():
                LOG.warning("Cannot open %s - retrying in %.0fs", self.describe(), backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            LOG.info("Connected to %s (%.1f fps)", self.describe(), self.fps_hint)
            backoff = 1.0
            fails = 0
            while not self._stop.is_set():
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 25:
                        LOG.warning("Stream lost (%s) - reconnecting", self.describe())
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                with self._cond:
                    self._frame, self._t = frame, time.monotonic()
                    self._seq += 1
                    self._cond.notify_all()
        if self.cap:
            self.cap.release()

    def read(self, timeout: float = 2.0):
        """Return (ok, frame, timestamp_seconds, end_of_stream)."""
        if self.is_live:
            with self._cond:
                self._cond.wait_for(lambda: self._seq != self._last_seq or self._stop.is_set(), timeout)
                if self._seq == self._last_seq:
                    return False, None, time.monotonic(), False
                self._last_seq = self._seq
                return True, self._frame.copy(), self._t, False
        ok, frame = self.cap.read()
        if not ok:
            if self.loop_file:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.cap.read()
            if not ok:
                return False, None, 0.0, True
        t = self._file_idx / self.fps_hint
        self._file_idx += 1
        if self.realtime_file:
            if self._wall0 is None:
                self._wall0 = time.monotonic() - t
            delay = self._wall0 + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return True, frame, t, False

    def release(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if not self.is_live and self.cap:
            self.cap.release()


# --------------------------------------------------------------------------
# Flood detection + water level measurement
# --------------------------------------------------------------------------
@dataclass
class Measurement:
    raw_level_m: float
    motion_ratio: float          # share of the marker zone covered by moving objects
    confidence: float
    flood_coverage: float        # share of the flood ROI covered by water (0..1)
    flood_detected: bool
    objects: list                # obstruction boxes [x, y, w, h] INSIDE the marker ROI only (frame coords)
    any_motion: bool = False     # something moved anywhere in the work area (internal use only)
    water_mask: np.ndarray | None = None   # work-crop coordinates
    object_mask: np.ndarray | None = None
    rect_view: np.ndarray | None = None    # rectified marker (debug)


def _median_gray(img_bgr) -> float:
    """Median brightness (0-255) of a frame, on a small copy (fast)."""
    h, w = img_bgr.shape[:2]
    small = cv2.resize(img_bgr, (160, max(1, h * 160 // w)), interpolation=cv2.INTER_AREA)
    return float(np.median(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)))


class FloodDetector:
    """
    Water detection inside a work crop that contains both ROIs:
      1. Water evidence per pixel
           * "reference" method (recommended): CIE-Lab distance to a DRY
             reference frame of the same scene, with brightness compensation,
             AND the pixel must have a flood color (see _flood_colored).
           * "color" method (fallback): brown or dark green flood water in HSV.
      2. Temporal smoothing (exponential average of the water evidence) so
         ripples, glare and sensor noise do not flicker.
      3. Moving objects (cars, people) from MOG2 background subtraction; only
         blobs larger than --min-object-area count. They are removed from the
         flood coverage and reported to the Kalman filter as obstructions.
      4. FLOOD ROI  -> flood coverage % of the road.
         MARKER ROI -> warped to the rectified marker (perspective removed);
         scanned from the road surface upward; the water line is refined to
         sub-row precision where the water profile crosses the threshold.
    """

    def __init__(self, args):
        self.method = args.method
        self.diff_thresh = args.diff_thresh
        self.row_frac = args.row_fraction
        self.gap_m = max(0.0, float(args.gap_m))
        self.smooth = float(np.clip(args.mask_smooth, 0.0, 0.95))
        self.min_obj_frac = args.min_object_area
        self.flood_min_cov = args.flood_min_coverage
        self.min_obstruction_m = args.min_obstruction_height
        self.hsv_lo = np.array(args.water_hsv_low, np.uint8)
        self.hsv_hi = np.array(args.water_hsv_high, np.uint8)
        self.green_lo = np.array(args.green_hsv_low, np.uint8)
        self.green_hi = np.array(args.green_hsv_high, np.uint8)
        self.color_gate = not args.no_color_gate
        self.color_shift = args.color_shift
        self.color_blur = max(0.0, args.color_blur)
        self.fill_gaps = max(0.0, args.fill_gaps)
        self.hue_lo, self.hue_hi = args.water_hue
        self.chroma_lo, self.chroma_hi = args.water_chroma
        self.light_lo, self.light_hi = (v * 2.55 for v in args.water_lightness)   # L* % -> 8-bit Lab
        self.k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.k7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.k9 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self.ref_bgr = None
        self.ref_lab = None
        self.ref_gray_mean = None
        self.ref_median = None
        # ---- night: own dry reference, own water rule (see _night_evidence) ----
        self.light_mode = args.light                    # "auto" / "day" / "night"
        self.night_level = args.night_level
        self.night_change = args.night_change
        self.is_night = args.light == "night"
        self._light_known = args.light != "auto"
        self.night_bgr = None
        self.night = None                               # prepared night reference
        self._warned_t = -1e9
        self.cal: Calibration | None = None
        self.geom = None
        self.mg: MarkerGeometry | None = None
        self.perspective: dict = {}
        self._reset_state()

    def _reset_state(self):
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        self.prob = None
        self.frames = 0

    # ---- setup ---------------------------------------------------------
    def set_calibration(self, cal: Calibration, frame_shape, hfov_deg: float = CAMERA_HFOV_DEG):
        self.cal = cal
        self.geom = None
        self.mg = None
        self._reset_state()
        if not cal.ready:
            return
        H, W = frame_shape[:2]
        cal.frame_size = [W, H]
        try:
            mg = MarkerGeometry(cal.marker_roi, cal.ref_height_m)
        except (cv2.error, np.linalg.LinAlgError):
            LOG.error("Marker ROI is degenerate - drag its 4 corners apart.")
            return
        if not mg.convex:
            LOG.warning("Marker ROI corners are crossed or too small - drag them to form a proper 4-sided shape.")
            return
        flood = np.array(cal.flood_roi, np.int32)
        pts = np.vstack([flood, mg.quad.astype(np.int32)])
        x, y, w, h = cv2.boundingRect(pts)
        x, y = max(0, x - 4), max(0, y - 4)
        w, h = min(W - x, w + 8), min(H - y, h + 8)
        if w < 8 or h < 8:
            LOG.error("ROIs are outside the image.")
            return
        off = np.array([x, y], np.int32)
        flood_mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(flood_mask, [flood - off], 255)
        T = np.array([[1, 0, x], [0, 1, y], [0, 0, 1]], np.float64)
        M = mg.M_img2rect @ T                                  # crop -> rectified marker
        valid = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (mg.w, mg.rows),
                                    flags=cv2.INTER_NEAREST, borderValue=0) > 0
        if valid[-int(0.3 * RECT_PPM):].mean() < 0.5:
            LOG.warning("The lower part of the marker ROI is outside the image.")
        marker_mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(marker_mask, [np.round(mg.quad).astype(np.int32) - off], 255)
        self.geom = dict(crop=(x, y, w, h), flood_mask=flood_mask, marker_mask=marker_mask,
                         min_overlap_px=max(15, int(0.02 * cv2.countNonZero(marker_mask))), M=M, valid=valid,
                         min_obj_px=max(30, int(self.min_obj_frac * W * H)))
        k = max(7, int(round(self.fill_gaps * W)) | 1)       # rectangle: fast even when large
        self.k_fill = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        self.mg = mg
        self.perspective = mg.perspective_info(W, H, hfov_deg, cal.marker_width_m)
        acc = self.perspective["setup_accuracy_cm_per_px"].get("1.0m")
        if acc is not None and acc > 3.0:
            LOG.warning("Marker setup is sensitive: a 1-pixel corner error changes the 1.0 m reading by "
                        "%.1f cm. Make the marker ROI taller (higher reference point) or use a higher "
                        "camera resolution / zoom.", acc)
        self._prepare_reference()

    def set_reference(self, frame_bgr, night: bool = False):
        """Dry reference for daylight, or (night=True) for night footage."""
        if night:
            self.night_bgr = frame_bgr.copy()
        else:
            self.ref_bgr = frame_bgr.copy()
        self.prob = None
        self._prepare_night() if night else self._prepare_day()

    def _prepare_reference(self):
        self._prepare_day()
        self._prepare_night()

    def _prepare_day(self):
        if self.geom is None or self.ref_bgr is None:
            return
        W, H = self.cal.frame_size
        if self.ref_bgr.shape[:2] != (H, W):
            self.ref_bgr = cv2.resize(self.ref_bgr, (W, H))
        x, y, w, h = self.geom["crop"]
        self.ref_lab = self._to_lab(self.ref_bgr[y:y + h, x:x + w])
        self.ref_small = self._color_lab(self.ref_lab, W)
        self.ref_gray_mean = float(cv2.cvtColor(self.ref_bgr, cv2.COLOR_BGR2GRAY).mean())
        self.ref_median = _median_gray(self.ref_bgr)

    def _night_window(self, frame_w: int):
        """Wide, flat window for the night statistics: wide enough to see the road's pattern,
        flat so the (horizontal) water line on the marker stays sharp (~1 cm)."""
        return max(3, int(round(0.022 * frame_w)) | 1), max(3, int(round(0.005 * frame_w)) | 1)

    def _prepare_night(self):
        if self.geom is None or self.night_bgr is None:
            return
        W, H = self.cal.frame_size
        if self.night_bgr.shape[:2] != (H, W):
            self.night_bgr = cv2.resize(self.night_bgr, (W, H))
        x, y, w, h = self.geom["crop"]
        R = cv2.cvtColor(self.night_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
        k = self._night_window(W)
        mu = cv2.blur(R, k)
        sd = np.sqrt(np.maximum(cv2.blur(R * R, k) - mu * mu, 0))
        self.night = dict(gray=R, mu=mu, sd=sd, k=k, median=_median_gray(self.night_bgr),
                          mean=float(cv2.cvtColor(self.night_bgr, cv2.COLOR_BGR2GRAY).mean()))

    @staticmethod
    def _to_lab(crop_bgr) -> np.ndarray:
        return cv2.cvtColor(cv2.GaussianBlur(crop_bgr, (3, 3), 0), cv2.COLOR_BGR2LAB).astype(np.float32)

    def _color_lab(self, lab, frame_w: int) -> np.ndarray:
        """Half-resolution Lab whose color (a*, b*) is averaged HORIZONTALLY: pale muddy water
        has so little color that the hue of a single pixel is mostly camera noise. Averaging only
        along the (horizontal) water line keeps the line where it is - a square average would
        spread the water color up the marker and read the level several cm too high.
        Half resolution is enough for an averaged color and keeps 1080p fast; the lightness
        used for the water line stays at full resolution in _water_evidence."""
        h, w = lab.shape[:2]
        f = 2 if min(h, w) >= 200 else 1
        small = cv2.resize(lab, (w // f, h // f), interpolation=cv2.INTER_AREA) if f > 1 else lab
        k = int(round(self.color_blur * frame_w / f)) | 1
        if k > 1:
            L, a, b = cv2.split(small)
            small = cv2.merge([L, cv2.blur(a, (k, 1)), cv2.blur(b, (k, 1))])
        return small

    def patch_reference(self, frame_bgr, quad, grow_px: int = 12) -> bool:
        """Replace the dry reference inside `quad` (+ a margin) with the current frame, e.g.
        where the physical marker stood before it was removed. Only the reference of the
        current light (day / night) is patched - the other one was not taken with this frame."""
        attr = "night_bgr" if self.is_night else "ref_bgr"
        ref = getattr(self, attr)
        if ref is None or not quad:
            return False
        if ref.shape[:2] != frame_bgr.shape[:2]:
            ref = cv2.resize(ref, frame_bgr.shape[1::-1])
        m = np.zeros(frame_bgr.shape[:2], np.uint8)
        cv2.fillPoly(m, [np.round(np.asarray(quad, np.float64)).astype(np.int32)], 255)
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow_px + 1, 2 * grow_px + 1)))
        ref[m > 0] = frame_bgr[m > 0]
        setattr(self, attr, ref)
        self.prob = None
        self._prepare_night() if self.is_night else self._prepare_day()
        return True

    def adapt_reference(self, frame_bgr, alpha: float):
        """Slowly follow light changes while the road is dry (never while flooded); adapts the
        reference of the current light (day / night)."""
        if alpha <= 0:
            return
        if self.is_night and self.night_bgr is not None:
            self.night_bgr = cv2.addWeighted(self.night_bgr, 1 - alpha, frame_bgr, alpha, 0)
            self._prepare_night()
        elif not self.is_night and self.ref_bgr is not None:
            self.ref_bgr = cv2.addWeighted(self.ref_bgr, 1 - alpha, frame_bgr, alpha, 0)
            self._prepare_day()

    @property
    def ready(self) -> bool:
        return self.geom is not None

    @property
    def uses_reference(self) -> bool:
        """A dry reference exists for the current light (day / night)."""
        if self.method == "color":
            return False
        return self.night is not None if self.is_night else self.ref_lab is not None

    @property
    def light(self) -> str:
        return "night" if self.is_night else "day"

    def update_light(self, frame_bgr):
        """Day or night, from the MEDIAN brightness of the frame (street lights and their
        reflections make the mean useless at night). With a day and a night reference the closer
        one wins, so a dark rainy day is not taken for night; hysteresis avoids flipping at dusk."""
        if self.light_mode != "auto":
            return
        med = _median_gray(frame_bgr)
        night = self.is_night
        d_med = self.ref_median if self.ref_bgr is not None else None
        n_med = self.night["median"] if self.night is not None else None
        if d_med is not None and n_med is not None and abs(d_med - n_med) > 16:
            dn, dd = abs(med - n_med), abs(med - d_med)
            if not self._light_known or abs(dn - dd) > 8:
                night = dn < dd
        elif not self._light_known:
            night = med < self.night_level
        elif night and med > self.night_level + 8:
            night = False
        elif not night and med < self.night_level - 8:
            night = True
        if night != self.is_night or not self._light_known:
            if self._light_known:
                LOG.info("Light changed to %s (median brightness %.0f)", "NIGHT" if night else "DAY", med)
                self.prob = None
            self.is_night, self._light_known = night, True

    # ---- per-frame -----------------------------------------------------
    def _water_evidence(self, frame_bgr, crop_bgr) -> np.ndarray:
        """Per-pixel water evidence (0/1). No morphology here: at long range one pixel can be
        several cm of water depth, so thin water bands must survive; noise is removed by the
        temporal smoothing instead."""
        if self.is_night and self.method != "color":
            if self.night is None:
                if time.monotonic() - self._warned_t > 30:
                    self._warned_t = time.monotonic()
                    LOG.warning("NIGHT footage but no dry NIGHT reference - water is NOT measured (a night "
                                "frame against a daylight reference would read the whole road as water). "
                                "Press 'b' on a dry night frame, or use --reference-night.")
                return np.zeros(crop_bgr.shape[:2], np.float32)
            return self._night_evidence(frame_bgr, crop_bgr).astype(np.float32)
        if self.uses_reference:
            lab = self._to_lab(crop_bgr)
            gain = float(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).mean() + 1) / (self.ref_gray_mean + 1)
            gain = float(np.clip(gain, 0.5, 2.0))               # clouds / auto-exposure
            dl = (lab[..., 0] - self.ref_lab[..., 0] * gain) * 0.5   # brightness counts less than color
            if not self.color_gate:
                da = lab[..., 1] - self.ref_lab[..., 1]
                db = lab[..., 2] - self.ref_lab[..., 2]
                changed = cv2.sqrt(dl * dl + da * da + db * db) > self.diff_thresh
            else:
                # color part at half resolution (averaged color), lightness at full resolution
                s, r = self._color_lab(lab, frame_bgr.shape[1]), self.ref_small
                da, db = s[..., 1] - r[..., 1], s[..., 2] - r[..., 2]
                a, b = s[..., 1] - 128.0, s[..., 2] - 128.0
                # `toward` = how much the color moved TOWARD the pixel's own brown / green hue.
                # Water dyes the surface (toward > 0); a shadow / wet patch only darkens and greys
                # it (toward < 0), so a big change counts only if the color did not fade. Murky
                # water close to the road color (pale grey-brown on wet asphalt) is caught by a
                # clear shift toward the hue alone.
                toward = (a * da + b * db) / np.maximum(cv2.magnitude(a, b), 1e-3)
                colored = self._flood_colored(s)
                size = (lab.shape[1], lab.shape[0])
                up = lambda m: cv2.resize(m.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
                dab2 = cv2.resize(da * da + db * db, size, interpolation=cv2.INTER_LINEAR)
                changed = ((up(colored & (toward > 0)) & (dl * dl + dab2 > self.diff_thresh ** 2))
                           | up(colored & (toward > self.color_shift)))
            mask = changed.astype(np.uint8) * 255
        else:
            hsv = cv2.cvtColor(cv2.GaussianBlur(crop_bgr, (3, 3), 0), cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, self.hsv_lo, self.hsv_hi) | cv2.inRange(hsv, self.green_lo, self.green_hi)
        return mask.astype(np.float32) / 255.0

    def _flood_colored(self, lab) -> np.ndarray:
        """Flood water at the site is BROWN (muddy) or DARK GREEN. In CIE-Lab that is a
        yellow-to-green hue (brown ~55-85 deg, dark / olive green ~110-165 deg) with some color
        but not a vivid one, and not bright. Fails: grey shadows / wet asphalt / black, white
        glare / white cars (no color), red / blue / purple clothes (hue), orange / yellow
        raincoats (too vivid / bright)."""
        a = lab[..., 1] - 128.0
        b = lab[..., 2] - 128.0
        chroma = cv2.magnitude(a, b)
        hue = cv2.phase(a, b, angleInDegrees=True)                  # atan2(b, a), 0..360
        light = lab[..., 0]
        return ((hue >= self.hue_lo) & (hue <= self.hue_hi)
                & (chroma >= self.chroma_lo) & (chroma <= self.chroma_hi)
                & (light >= self.light_lo) & (light <= self.light_hi))

    def _night_evidence(self, frame_bgr, crop_bgr) -> np.ndarray:
        """Night: color is unreliable (street lights, IR) and, pixel by pixel, flood water looks
        like wet asphalt. Compared with a DRY NIGHT reference (a wet road is fine), flood water
          * COVERS the road's pattern (lane lines, curb, cracks): low local correlation;
          * is DARKER than the lit road (dark flood water), or
          * is BRIGHTER and ROUGH with a changed pattern (rippling water reflecting the lights).
        Reflections on a wet but NOT flooded road keep the road's pattern and only change their
        brightness; roughness is measured relative to brightness, so they do not count."""
        nr = self.night
        G = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gain = float(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).mean() + 1) / (nr["mean"] + 1)
        G *= 1.0 / float(np.clip(gain, 0.5, 2.0))                  # exposure / street-light changes
        k, R, muR, sdR = nr["k"], nr["gray"], nr["mu"], nr["sd"]
        muG = cv2.blur(G, k)
        sdG = np.sqrt(np.maximum(cv2.blur(G * G, k) - muG * muG, 0))
        ncc = (cv2.blur(G * R, k) - muG * muR) / (sdG * sdR + 1e-3)
        textured = sdR > 6.0                                        # the reference shows a pattern here
        pattern_changed = ~textured | (ncc < 0.7)
        covered = textured & (ncc < 0.5)
        d = self.night_change
        darker = ((muR - muG) > d) & (muR < 200)                    # not where the reference is blown out
        rough = (sdG * (muR + 10) / (muG + 10) > np.maximum(2 * sdR, sdR + 8)) & pattern_changed
        brighter = ((muG - muR) > d) & pattern_changed & (textured | rough)
        return covered | darker | rough | brighter

    def _objects(self, crop_bgr):
        """
        Moving objects (MOG2). Returns:
          obj_all    - every moving object (only used to keep cars / people out of the
                       road-water percentage; NOT reported as obstructions)
          obj_marker - the part of moving objects that is inside the MARKER ROI
          boxes      - obstruction boxes, only where an object covers the marker
        """
        fg = self.bg.apply(crop_bgr)
        self.frames += 1
        h, w = fg.shape
        obj_all = np.zeros((h, w), np.uint8)
        obj_marker = np.zeros((h, w), np.uint8)
        boxes = []
        if self.frames <= 10:                                   # background model warming up
            return obj_all, obj_marker, boxes
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.k3)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self.k5)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
        big = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= self.geom["min_obj_px"]]
        if not big:
            return obj_all, obj_marker, boxes
        objects = np.isin(labels, big).astype(np.uint8) * 255
        obj_all = cv2.dilate(objects, self.k9)
        # obstruction = the part of an object that is inside the marker ROI
        inside = cv2.bitwise_and(objects, self.geom["marker_mask"])
        n2, _, st2, _ = cv2.connectedComponentsWithStats(inside, connectivity=8)
        x0, y0 = self.geom["crop"][:2]
        for i in range(1, n2):
            if st2[i, cv2.CC_STAT_AREA] >= self.geom["min_overlap_px"]:
                boxes.append([int(st2[i, 0] + x0), int(st2[i, 1] + y0), int(st2[i, 2]), int(st2[i, 3])])
        if boxes:
            obj_marker = inside
        return obj_all, obj_marker, boxes

    def measure(self, frame_bgr) -> Measurement | None:
        g = self.geom
        if g is None:
            return None
        x, y, w, h = g["crop"]
        crop = frame_bgr[y:y + h, x:x + w]
        self.update_light(frame_bgr)
        evidence = self._water_evidence(frame_bgr, crop)
        self.prob = evidence if self.prob is None else cv2.addWeighted(
            self.prob, self.smooth, evidence, 1.0 - self.smooth, 0)
        obj, obj_marker, boxes = self._objects(crop)
        # cleaned water mask for the road coverage (area measure, so morphology is safe here)
        water_u8 = (self.prob >= 0.5).astype(np.uint8) * 255
        water_u8 = cv2.morphologyEx(water_u8, cv2.MORPH_OPEN, self.k3)
        water_u8 = cv2.morphologyEx(water_u8, cv2.MORPH_CLOSE, self.k_fill)   # a flood is one sheet
        water = water_u8 > 0
        obj_b = obj > 0

        # ---- flood coverage of the road (objects excluded) ----
        flood = g["flood_mask"] > 0
        free = flood & ~obj_b
        n_free = int(free.sum())
        coverage = float((water & free).sum() / n_free) if n_free > 50 else 0.0

        # ---- water level on the rectified (perspective-free) marker ----
        mg = self.mg
        r_prob = cv2.warpPerspective(self.prob, g["M"], (mg.w, mg.rows), flags=cv2.INTER_LINEAR, borderValue=0)
        r_obj = cv2.warpPerspective(obj_marker, g["M"], (mg.w, mg.rows), flags=cv2.INTER_NEAREST,
                                    borderValue=0) > 0
        valid = g["valid"]
        vcount = np.maximum(valid.sum(axis=1), 1)
        w_up = (((r_prob >= 0.5) & valid).sum(axis=1) / vcount)[::-1]    # index 0 = road surface
        o_up = ((r_obj & valid).sum(axis=1) / vcount)[::-1]
        win = max(3, int(RECT_PPM * 0.02) | 1)                            # 2 cm smoothing
        pad = win // 2
        w_s = np.convolve(np.pad(w_up, pad, mode="edge"), np.ones(win) / win, mode="valid")
        gap = max(2, int(self.gap_m * RECT_PPM))
        last = -1
        for k in range(mg.rows):
            if w_s[k] >= self.row_frac:
                last = k
            elif k - last > gap:
                break
        if last < 0:
            level = 0.0
        else:
            frac = 0.5
            if last + 1 < mg.rows:                                  # sub-row water line
                a, b = float(w_s[last]), float(w_s[last + 1])
                frac = float(np.clip((a - self.row_frac) / (a - b), 0, 1)) if a > b else 0.5
            level = min((last + 0.5 + frac) / RECT_PPM, mg.max_m)

        # Obstruction = a moving object covering a TALL part of the marker (car, person).
        # The rising / rippling water line also "moves", but only as a thin horizontal band.
        covered = np.concatenate([[0], (o_up > 0.3).astype(np.int8), [0]])
        edges = np.flatnonzero(np.diff(covered))
        longest_m = float((edges[1::2] - edges[0::2]).max() / RECT_PPM) if edges.size else 0.0
        obstructed = longest_m >= self.min_obstruction_m
        zone = max(1, int(max(level, 0.5) * RECT_PPM))
        motion_ratio = float(o_up[:zone].mean()) if obstructed else 0.0
        if not obstructed:
            boxes, obj_marker = [], np.zeros_like(obj_marker)
        if last >= 0:
            confidence = float(w_up[:last + 1].mean())
        else:
            confidence = float(1.0 - w_up[: int(0.05 * RECT_PPM)].mean())
        if level >= 0.05 and coverage < 0.02:            # marker says water, road does not
            confidence *= 0.5
        flood_detected = coverage >= self.flood_min_cov or level >= 0.03
        rect_view = (r_prob * 255).astype(np.uint8)
        return Measurement(level, motion_ratio, confidence, coverage, flood_detected, boxes,
                           bool(obj.any()), water_u8, obj_marker, rect_view)


# --------------------------------------------------------------------------
# Kalman filter: smoothing + obstruction / anomaly rejection
# --------------------------------------------------------------------------
@dataclass
class FilterResult:
    level_m: float
    rate_m_per_min: float
    anomaly: bool
    reason: str | None
    innovation_sigma: float
    pending_m: float | None = None     # level waiting for confirmation (None = nothing pending)


class FloodKalman:
    """
    1-D constant-velocity Kalman filter (OpenCV cv2.KalmanFilter).
        state       = [water_level, rise_rate]
        measurement = raw water level from the marker

    Flood water rises/falls slowly. A car, motorcycle or person crossing the
    marker produces a sudden high spike. Every measurement is tested against the
    filter prediction (innovation gate, in standard deviations). Measurements
    that fail the gate, or that come with a moving object at the marker, are
    flagged as an ANOMALY (obstruction) and are NOT used to update the level.

    A jump is CONFIRMED as a real (fast) change of the water level, and the
    filter re-locks onto it, when nothing moves at the marker and the readings
    have been steady at the new level (most of the last `window_s` seconds,
    every frame counted, within `tol` of their median) for:
      * `rise_s` (rise) or `fall_s` (fall);
      * `persist_s` if something was moving at the marker shortly before the
        steady readings (a car that parks there looks like water).
    A jump ends only after `grace_s` without anomalous readings, so a
    flickering water line does not restart the confirmation.

    A RECESSION is handled differently: the water line keeps moving down, so
    the readings never sit steadily at one level and the confirmation above
    would hold the reported level at the peak until the flood has fully
    disappeared. Instead, once the readings slide continuously downward for
    `trend_s` seconds, the filter FOLLOWS the water line down (occlusions only
    ever raise the reading, so a persistent slide is real water receding).
    """

    def __init__(self, args):
        self.r = args.meas_std ** 2
        self.q = args.accel_std ** 2
        self.gate = args.gate_sigma
        self.min_jump = args.min_jump
        self.motion_thresh = args.motion_thresh
        self.persist_s = args.persist_s
        self.rise_s = args.rise_confirm
        self.fall_s = args.fall_confirm
        self.window_s = args.confirm_window
        self.tol = args.confirm_tol
        self.agree_frac = 0.7    # share of the window that must be within `tol` of its median
        self.cov_agree = 0.05    # road flood coverage change that backs up a jump
        self.grace_s = 1.0
        self.max_rate = 0.05     # m/s - faster "rises" are never extrapolated
        self.adapt_thresh = 0.8  # |mean normalized innovation| that means "filter is lagging"
        self.adapt_factor = 50.0 # process-noise boost while lagging
        self.trend_window = 3.0  # history length for the recession (falling-trend) test
        self.trend_s = max(0.5, float(getattr(args, "fall_trend", 1.5)))
        self.kf = None
        self.t_prev = None
        self.anomaly_since = None
        self.last_anomaly_t = None
        self.last_motion_t = None  # last time something moved at the marker
        self.episode_motion = False
        self.episode_reason = None
        self.steady_since = None  # since when the readings sit steadily at `steady_level`
        self.steady_level = None
        self.anomaly_hist = collections.deque()   # (t, z, coverage) of every frame of the current jump
        self.cov_base = None     # road flood coverage at the accepted level
        self.bias = 0.0          # running mean of the signed, normalized innovation
        self.hist = collections.deque()           # (t, z) raw readings, for the recession test
        self.trend_since = None  # since when the readings slide steadily downward
        self.trend_level = None  # filter level when the slide started (the level we left)
        self.follow_rate = None  # observed slide slope, used to keep tracking between frames
        self.following = False   # currently following a sustained recession

    def reset(self, keep_level: bool = False):
        """keep_level: restart the filter at its current level (e.g. physical -> virtual
        marker at the same spot), so the reading does not depend on the first new frame."""
        level = float(self.kf.statePost[0, 0]) if keep_level and self.kf is not None else None
        self.kf = None
        self._end_episode()
        self.bias = 0.0
        self.hist.clear()
        self.trend_since = self.trend_level = self.follow_rate = None
        self.following = False
        self.last_motion_t = None
        if level is not None:
            self._init(level, self.t_prev)
        else:
            self.cov_base = None

    def _end_episode(self):
        self.anomaly_since = self.last_anomaly_t = self.episode_reason = None
        self.steady_since = self.steady_level = None
        self.episode_motion = False
        self.anomaly_hist.clear()

    def _init(self, z: float, t: float):
        kf = cv2.KalmanFilter(2, 1, 0, cv2.CV_32F)
        kf.measurementMatrix = np.array([[1, 0]], np.float32)
        kf.measurementNoiseCov = np.array([[self.r]], np.float32)
        kf.statePost = np.array([[z], [0.0]], np.float32)
        kf.errorCovPost = np.diag([self.r, 1e-4]).astype(np.float32)
        self.kf, self.t_prev = kf, t
        self._end_episode()

    def _confirmed_level(self, t: float, level: float, moving: bool) -> float | None:
        """The new level if the current jump is confirmed as real water, else None."""
        recent = [(z, c) for tt, z, c in self.anomaly_hist if t - tt <= self.window_s]
        zs = np.array([z for z, _ in recent])
        z_med = float(np.median(zs))
        steady = (len(zs) >= 3 and np.mean(np.abs(zs - z_med) <= self.tol) >= self.agree_frac
                  and abs(z_med - level) > self.tol)
        if not steady:
            self.steady_since = self.steady_level = None
            return None
        if self.steady_level is None or abs(z_med - self.steady_level) > self.tol:
            self.steady_since, self.steady_level = t, z_med
        if moving:
            return None
        if self.last_motion_t is not None and t - self.last_motion_t <= 2 * self.window_s:
            # something was moving at the marker just before these steady readings
            # (e.g. a car that stopped / left) - demand the long confirmation
            need = self.persist_s
        else:
            rising = z_med > level
            if rising:
                need = self.rise_s
                covs = [c for _, c in recent if c is not None]
                d = float(np.median(covs)) - self.cov_base if covs and self.cov_base is not None else 0.0
                if not d > self.cov_agree:
                    need *= 2
            else:
                # a fall does NOT need the coverage to drop in agreement: the road stays
                # wet long after the water line has receded, so the penalty would delay
                # every real recession
                need = self.fall_s
        return z_med if t - self.steady_since >= need else None

    def _following(self, t: float, level: float) -> bool:
        """True while the raw readings slide steadily downward away from `level`.

        A receding water line keeps MOVING, so the readings never sit steadily at one
        level and the steady-confirmation in _confirmed_level() can never accept the
        descent - the reported level would stay frozen at the peak until the flood has
        fully disappeared. This test accepts the slide itself: the newest third of the
        recent readings sits clearly below the older third (and below the accepted
        level) for `trend_s` seconds. Occlusions (car, person) only ever RAISE the
        reading, so a persistent slide is the water receding, not an object."""
        if len(self.hist) < 4:
            self.trend_since = self.trend_level = None
            self.follow_rate = None
            self.following = False
            return False
        zs = np.array([z for _, z in self.hist])
        k = max(1, len(zs) // 3)
        old_med = float(np.median(zs[:k]))
        new_med = float(np.median(zs[-k:]))
        if self.trend_level is not None:
            level = self.trend_level          # keep comparing against where the slide started
        sliding = new_med <= old_med - 0.5 * self.tol and new_med < level - 0.5 * self.tol
        if not sliding:
            self.trend_since = self.trend_level = None
            self.follow_rate = None
            self.following = False
            return False
        if self.trend_since is None:
            self.trend_since, self.trend_level = t, level
            ts = np.array([tt for tt, _ in self.hist])
            span = float(ts[-1] - ts[0])
            slope = (float(zs[-1]) - float(zs[0])) / span if span >= 0.3 else 0.0
            self.follow_rate = float(np.clip(slope, -self.max_rate, 0.0))
        return t - self.trend_since >= self.trend_s

    def step(self, z: float, t: float, motion_ratio: float, coverage: float | None = None) -> FilterResult:
        if self.kf is None:
            self._init(z, t)
            self.cov_base = coverage
            self.hist.append((t, z))
            return FilterResult(z, 0.0, False, None, 0.0)
        dt = float(np.clip(t - self.t_prev, 1e-3, 5.0))
        self.t_prev = t
        kf = self.kf
        kf.transitionMatrix = np.array([[1, dt], [0, 1]], np.float32)
        # Adaptive process noise: if the measurements keep landing on the same side of
        # the prediction (water started / stopped rising), let the filter catch up faster.
        q = self.q * (self.adapt_factor if abs(self.bias) > self.adapt_thresh else 1.0)
        kf.processNoiseCov = (q * np.array([[dt ** 4 / 4, dt ** 3 / 2],
                                            [dt ** 3 / 2, dt ** 2]])).astype(np.float32)
        pred = float(kf.predict()[0, 0])          # also copies prediction to statePost
        S = float(kf.errorCovPre[0, 0]) + self.r
        innov = z - pred
        sigma = abs(innov) / np.sqrt(S)

        moving = motion_ratio > self.motion_thresh
        if moving:
            self.last_motion_t = t

        # every raw reading feeds the recession test
        self.hist.append((t, z))
        while self.hist and t - self.hist[0][0] > self.trend_window:
            self.hist.popleft()

        if self._following(t, pred):
            # sustained recession: follow the water line down (a car / person only ever
            # RAISES the reading, so a persistent slide is real water going down)
            if not self.following:
                self.following = True
                LOG.info("Sustained recession detected - following the water line down")
            if self.anomaly_since is not None:
                self._end_episode()
            self.bias = 0.9 * self.bias + 0.1 * float(innov / np.sqrt(S))
            if self.follow_rate is not None:
                kf.statePost[1, 0] = self.follow_rate
            kf.correct(np.array([[z]], np.float32))
            if coverage is not None:
                self.cov_base = coverage if self.cov_base is None else 0.9 * self.cov_base + 0.1 * coverage
            level = float(np.clip(kf.statePost[0, 0], 0.0, MARKER_MAX_M * 1.1))
            kf.statePost[0, 0] = level
            return FilterResult(level, float(kf.statePost[1, 0]) * 60.0, False, None, float(sigma), None)

        reasons = []
        if moving:
            reasons.append("moving object at marker")
        if sigma > self.gate and abs(innov) > self.min_jump:
            reasons.append("sudden spike (rise)" if innov > 0 else "sudden drop")

        if not reasons:
            self.bias = 0.9 * self.bias + 0.1 * float(innov / np.sqrt(S))
            kf.correct(np.array([[z]], np.float32))
            if self.anomaly_since is not None and t - self.last_anomaly_t > self.grace_s:
                self._end_episode()
            if self.anomaly_since is None and coverage is not None:
                self.cov_base = coverage if self.cov_base is None else 0.9 * self.cov_base + 0.1 * coverage
        else:
            if self.anomaly_since is None:
                self.anomaly_since = t
            self.last_anomaly_t = t
            self.episode_motion |= moving
            self.episode_reason = " + ".join(reasons)
            # keep predicting with the current rise rate (flood keeps rising while a car
            # passes), but never extrapolate an implausible rate
            kf.statePost[1, 0] = float(np.clip(kf.statePost[1, 0], -self.max_rate, self.max_rate))

        pending = None
        if self.anomaly_since is not None:
            # every frame of an open jump counts, so flicker cannot fake a steady level
            self.anomaly_hist.append((t, z, coverage))
            while self.anomaly_hist and t - self.anomaly_hist[0][0] > self.window_s:
                self.anomaly_hist.popleft()
            pending = float(np.median([v for _, v, _ in self.anomaly_hist]))
            z_new = self._confirmed_level(t, float(kf.statePost[0, 0]), moving)
            if z_new is not None:
                LOG.info("Level change confirmed after %.1fs (%s) -> accepting %.2f m",
                         t - self.anomaly_since,
                         "a moving object was at the marker" if self.episode_motion else "steady, no motion",
                         z_new)
                self._init(z_new, t)
                self.cov_base = coverage
                pending = None

        anomaly = self.anomaly_since is not None
        reason = self.episode_reason if anomaly else None
        kf = self.kf
        level = float(np.clip(kf.statePost[0, 0], 0.0, MARKER_MAX_M * 1.1))
        kf.statePost[0, 0] = level
        rate = float(kf.statePost[1, 0]) * 60.0
        return FilterResult(level, rate, anomaly, reason, float(sigma), pending)


# --------------------------------------------------------------------------
# Live data feed (terminal). The payload dict is the future website format.
# --------------------------------------------------------------------------
def build_payload(cal: Calibration, source_kind: str, fps: float, meas: Measurement | None,
                  res: FilterResult | None, status: str | None, det: FloodDetector,
                  alignment: dict | None = None) -> dict:
    payload = {
        "camera_id": cal.camera_id,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "calibrated": cal.ready and not cal.is_default,
        "source": source_kind,
        "fps": round(fps, 1),
        "marker_mode": cal.marker_mode,
        "physical_marker_present": cal.pole_present,
        "camera_pose": cal.camera_pose,
        "alignment": alignment or {"enabled": False},
    }
    if res is None:
        payload.update({"water_level_m": None, "status": "NOT_CALIBRATED"})
        return payload
    payload.update({
        "water_level_m": round(res.level_m, 3),
        "raw_level_m": round(meas.raw_level_m, 3),
        "rate_m_per_min": round(res.rate_m_per_min, 4),
        "status": status,
        "status_color": STATUS_COLOR_NAME[status],
        "status_hex": STATUS_HEX[status],
        "flood_detected": meas.flood_detected,
        "flood_coverage_pct": round(meas.flood_coverage * 100, 1),
        "anomaly": res.anomaly,
        "anomaly_reason": res.reason,
        "level_state": "CONFIRMING" if res.anomaly else "CONFIRMED",
        "pending_level_m": round(res.pending_m, 3) if res.pending_m is not None else None,
        "obstruction_at_marker": bool(meas.objects),
        "motion_ratio": round(meas.motion_ratio, 3),
        "confidence": round(meas.confidence, 3),
        "method": ("night-reference" if det.is_night else "reference") if det.uses_reference else "color",
        "light": det.light,
        "ref_height_m": cal.ref_height_m,
        "perspective": det.perspective,
    })
    return payload


def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class TerminalFeed:
    """
    Prints the live readings in the terminal:
      * one line every `interval` seconds, and
      * immediately when the flood status changes or an obstruction starts/ends.
    """

    ANSI = {"SAFE": "\033[92m", "ALERT": "\033[93m", "DANGER": "\033[91m",
            "WARN": "\033[38;5;208m", "END": "\033[0m"}

    def __init__(self, interval_s: float, json_mode: bool, color: bool):
        self.interval = interval_s
        self.json_mode = json_mode
        self.color = color and not json_mode and _enable_ansi()
        self._last_print = -1e9
        self._last_status = None
        self._last_anomaly = False

    def _c(self, key, text):
        return f"{self.ANSI[key]}{text}{self.ANSI['END']}" if self.color else text

    def format_line(self, d: dict, event: str | None) -> str:
        ts = d["timestamp"][11:19]
        if d.get("water_level_m") is None:
            return f"{ts}  {d['camera_id']}  NOT CALIBRATED  (f: flood ROI, m: marker ROI, b: dry reference, s: save)"
        st = d["status"]
        line = (f"{ts}  {d['camera_id']}  LEVEL {d['water_level_m']:.2f} m  "
                + self._c(st, f"[{st:<6} {d['status_color']:<6}]")
                + f"  raw {d['raw_level_m']:.2f} m  rate {d['rate_m_per_min']:+.3f} m/min"
                + f"  flood {d['flood_coverage_pct']:5.1f}%  conf {d['confidence']:.2f}"
                + f"  {d['fps']:4.1f} fps  ")
        if d["anomaly"]:
            tag = "OBSTRUCTION" if d["obstruction_at_marker"] else "CONFIRMING"
            pend = f" -> {d['pending_level_m']:.2f} m" if d.get("pending_level_m") is not None else ""
            line += self._c("WARN", f"{tag}: {d['anomaly_reason']}{pend}")
        else:
            line += "OK"
        line += f"  [{d['marker_mode']}{', NIGHT' if d.get('light') == 'night' else ''}]"
        al = d.get("alignment") or {}
        if al.get("enabled") and not al.get("ok"):
            line += self._c("WARN", "  ALIGNMENT LOST")
        if not d["calibrated"]:
            line += "  (default ROIs - adjust & press s)"
        if event:
            line += "  " + self._c("WARN" if "OBSTRUCTION" in event else st, f"<< {event}")
        return line

    def emit(self, d: dict, now: float):
        event = None
        st, an = d.get("status"), bool(d.get("anomaly"))
        if d.get("water_level_m") is not None:
            if st != self._last_status:
                event = "STATUS CHANGED" if self._last_status not in (None, "NOT_CALIBRATED") else "START"
            elif an and not self._last_anomaly:
                event = "OBSTRUCTION DETECTED"
            elif not an and self._last_anomaly:
                event = "OBSTRUCTION CLEARED"
        self._last_status, self._last_anomaly = st, an
        if event is None and now - self._last_print < self.interval:
            return
        self._last_print = now
        if self.json_mode:
            print(json.dumps(dict(d, event=event)), flush=True)
        else:
            print(self.format_line(d, event), flush=True)


# --------------------------------------------------------------------------
# Interactive setup editor: drag vertices / corners / whole regions
# --------------------------------------------------------------------------
def _seg_dist(p, a, b) -> float:
    p, a, b = (np.asarray(v, np.float64) for v in (p, a, b))
    ab = b - a
    t = float(np.clip(np.dot(p - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1))
    return float(np.linalg.norm(p - (a + t * ab)))


def _inside(poly, x, y) -> bool:
    return bool(poly) and cv2.pointPolygonTest(np.array(poly, np.float32), (float(x), float(y)), False) >= 0


class SetupEditor:
    """
    Mouse editing of both ROIs in the video window:
      left-drag a magenta corner  -> move a MARKER ROI corner
      left-drag a cyan vertex     -> move a FLOOD ROI vertex
      left-drag inside a region   -> move the whole region (marker has priority)
      right-click FLOOD ROI edge  -> insert a vertex
      right-click FLOOD ROI vertex-> delete it (keeps at least 3)
    Virtual marker mode: the marker has no corners to drag; dragging it moves
    its foot along the road (`relocate(x, y)` rebuilds it from the camera pose).
    `place` (set by the 't' key) is called with the next left click.
    `changed` becomes True when an edit is finished.
    """

    HANDLE_R = 12

    def __init__(self, cal: Calibration, frame_w: int, frame_h: int):
        self.cal = cal
        self.W, self.H = frame_w, frame_h
        self.drag = None            # ("marker", i) / ("flood", i) / ("move_marker"|"move_flood", x0, y0, orig)
        self.changed = False
        self.place = None           # one-shot click callback (place the virtual marker)
        self.relocate = None        # callback(x, y) -> move the virtual marker foot
        self.base_px = None         # callback() -> current virtual marker foot pixel

    def _clamp(self, x, y):
        return int(np.clip(x, 0, self.W - 1)), int(np.clip(y, 0, self.H - 1))

    def _hit_vertex(self, x, y):
        best, best_d = None, self.HANDLE_R
        names = ("flood",) if self.cal.marker_mode == "virtual" else ("marker", "flood")
        for name in names:
            for i, p in enumerate(getattr(self.cal, f"{name}_roi") or []):
                d = float(np.hypot(p[0] - x, p[1] - y))
                if d < best_d or (d <= best_d and name == "marker"):
                    best, best_d = (name, i), d
        return best

    def on_mouse(self, event, x, y, flags=0, param=None):
        x, y = self._clamp(x, y)
        cal = self.cal
        if event == cv2.EVENT_LBUTTONDOWN:
            if self.place is not None:
                place, self.place = self.place, None
                if place(x, y):
                    self.changed = True
                return
            virtual = cal.marker_mode == "virtual" and self.relocate and self.base_px
            hit = self._hit_vertex(x, y)
            if hit:
                self.drag = hit
            elif _inside(cal.marker_roi, x, y):
                if virtual:
                    self.drag = ("move_virtual", x, y, self.base_px())
                else:
                    self.drag = ("move_marker", x, y, [list(p) for p in cal.marker_roi])
            elif _inside(cal.flood_roi, x, y):
                self.drag = ("move_flood", x, y, [list(p) for p in cal.flood_roi])
        elif event == cv2.EVENT_MOUSEMOVE and self.drag is not None:
            kind = self.drag[0]
            if kind in ("marker", "flood"):
                getattr(cal, f"{kind}_roi")[self.drag[1]] = [x, y]
            elif kind == "move_virtual":
                bx, by = self.drag[3]
                self.relocate(bx + x - self.drag[1], by + y - self.drag[2])
            else:
                dx, dy = x - self.drag[1], y - self.drag[2]
                moved = [list(self._clamp(px + dx, py + dy)) for px, py in self.drag[3]]
                setattr(cal, "marker_roi" if kind == "move_marker" else "flood_roi", moved)
        elif event == cv2.EVENT_LBUTTONUP and self.drag is not None:
            self.drag = None
            self.changed = True
        elif event == cv2.EVENT_RBUTTONDOWN and cal.flood_roi:
            hit = self._hit_vertex(x, y)
            if hit and hit[0] == "flood":
                if len(cal.flood_roi) > 3:
                    del cal.flood_roi[hit[1]]
                    self.changed = True
                return
            roi = cal.flood_roi
            dists = [_seg_dist((x, y), roi[i], roi[(i + 1) % len(roi)]) for i in range(len(roi))]
            i = int(np.argmin(dists))
            if dists[i] <= self.HANDLE_R:
                roi.insert(i + 1, [x, y])
                self.changed = True


# --------------------------------------------------------------------------
# Drawing: flood ROI, perspective-correct virtual marker, info panel
# --------------------------------------------------------------------------
def _pt(p):
    return int(round(p[0])), int(round(p[1]))


def _text(img, text, org, scale, color, thick=1, bg=True):
    font = cv2.FONT_HERSHEY_SIMPLEX
    if bg:
        (tw, th), base = cv2.getTextSize(text, font, scale, thick)
        x, y = org
        cv2.rectangle(img, (x - 2, y - th - 3), (x + tw + 2, y + base), (0, 0, 0), -1)
    cv2.putText(img, text, org, font, scale, color, thick, cv2.LINE_AA)


class LabelLayout:
    """Places text labels in priority order and skips any label that would overlap another."""

    def __init__(self):
        self.boxes = []

    def place(self, img, text, org, scale, color, thick=1, bg=True, force=False) -> bool:
        (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x, y = org
        box = (x - 2, y - th - 3, x + tw + 2, y + base)
        if not force and any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                             for b in self.boxes):
            return False
        self.boxes.append(box)
        _text(img, text, org, scale, color, thick, bg)
        return True


def draw_virtual_marker(out, mg: MarkerGeometry, level: float | None, raw: float | None, anomaly: bool):
    """Virtual staff gauge drawn through the homography (accurate under perspective)."""
    def P(s, h):
        return _pt(mg.to_img(s, h))

    def band_poly(a, b):
        return np.array([P(0, a), P(1, a), P(1, b), P(0, b)], np.int32)

    # faint full gauge
    layer = out.copy()
    for a, b, _, bgr, _ in FLOOD_BANDS:
        cv2.fillPoly(layer, [band_poly(a, b)], bgr)
    cv2.addWeighted(layer, 0.30, out, 0.70, 0, out)
    # strong fill up to the current water level (thermometer style)
    if level is not None and level > 0:
        layer = out.copy()
        for a, b, _, bgr, _ in FLOOD_BANDS:
            top = min(b, level)
            if top > a:
                cv2.fillPoly(layer, [band_poly(a, top)], bgr)
        cv2.addWeighted(layer, 0.75, out, 0.25, 0, out)
    cv2.polylines(out, [band_poly(0, MARKER_MAX_M)], True, (255, 255, 255), 1, cv2.LINE_AA)

    # ticks: every 0.05 m (minor) and 0.10 m (major), adaptive labels
    sp_top = mg.px_per_m(MARKER_MAX_M - 0.1, MARKER_MAX_M) * 0.1      # px per 0.10 m at the top
    label_all = sp_top >= 14
    minor = sp_top >= 10
    for i in range(0, 21):
        h = i * 0.05
        if i % 2 == 0:
            cv2.line(out, P(0, h), P(1, h), (255, 255, 255), 1, cv2.LINE_AA)
        elif minor:
            cv2.line(out, P(0, h), P(0.3, h), (255, 255, 255), 1, cv2.LINE_AA)
            cv2.line(out, P(0.7, h), P(1, h), (255, 255, 255), 1, cv2.LINE_AA)
    def side(h, s, gap):
        """Label anchor beside the marker at height h (s=1 right side, s=0 left side)."""
        edge = mg.to_img(s, h)
        across = mg.to_img(1, h) - mg.to_img(0, h)
        across = across / max(np.linalg.norm(across), 1e-6)
        return edge + across * (gap if s == 1 else -gap)

    # water line (perspective-correct)
    if level is not None:
        lv = min(level, mg.max_m)
        a, b = mg.to_img(-0.6, lv), mg.to_img(1.6, lv)
        cv2.line(out, _pt(a), _pt(b), (0, 0, 0), 4, cv2.LINE_AA)
        cv2.line(out, _pt(a), _pt(b), (255, 255, 0), 2, cv2.LINE_AA)
    if raw is not None:
        cv2.circle(out, P(-0.3, min(raw, mg.max_m)), 4, (0, 140, 255) if anomaly else (255, 255, 255), -1)

    # labels in priority order; lower-priority labels are skipped if they would overlap
    lay = LabelLayout()
    if level is not None:                              # value tag at the end of the water line
        tag = mg.to_img(1.6, min(level, mg.max_m)) + np.array([4, 5])
        lay.place(out, f"{level:.2f} m", _pt(tag), 0.5, (255, 255, 0), 1, force=True)
    right = [(0.5, "0.50 m DANGER", STATUS_BGR["DANGER"], 0.42),
             (0.3, "0.30 m ALERT", STATUS_BGR["ALERT"], 0.42),
             (0.0, "0.00 m road", (255, 255, 255), 0.40)]
    if abs(mg.ref_h - MARKER_MAX_M) < 1e-6:
        right.append((MARKER_MAX_M, "1.00 m = REF", (255, 255, 255), 0.42))
    else:
        right.append((MARKER_MAX_M, "1.00 m", (255, 255, 255), 0.42))
        if mg.ref_h < mg.max_m:
            right.append((mg.ref_h, f"REF {mg.ref_h:.2f} m", MARKER_COLOR, 0.40))
    if label_all:
        right += [(round(i * 0.1, 1), f"{i * 0.1:.1f}", (230, 230, 230), 0.38) for i in (1, 2, 4, 6, 7, 8, 9)]
    for h, text, color, scale in right:
        lay.place(out, text, _pt(side(h, 1, 8) + np.array([0, 4])), scale, color, 1)
    for a, b, name, bgr, _ in FLOOD_BANDS:            # band names on the left side
        (tw, _), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
        lay.place(out, name, _pt(side((a + b) / 2, 0, tw + 10) + np.array([0, 4])), 0.38, bgr, 1)


def draw_overlay(frame, det: FloodDetector, cal: Calibration, res: FilterResult | None,
                 meas: Measurement | None, status: str | None, fps: float, hint: str | None,
                 show_water: bool = True, show_handles: bool = True, alignment: dict | None = None):
    out = frame
    virtual = cal.marker_mode == "virtual"
    # ---- physical marker (pole) used for the calibration, while it still stands ----
    if virtual and cal.pole_present and cal.pole_roi:
        pq = np.round(np.array(cal.pole_roi)).astype(np.int32)
        cv2.polylines(out, [pq], True, (170, 170, 170), 1, cv2.LINE_AA)
        top = pq[np.argmin(pq[:, 1])]
        _text(out, "PHYSICAL MARKER (calibration) - x: removed", (int(top[0]) - 20, max(12, int(top[1]) - 8)),
              0.38, (200, 200, 200), 1)
    # ---- flood ROI + detected water ----
    if cal.flood_roi:
        if show_water and meas is not None and det.geom is not None:
            x, y, w, h = det.geom["crop"]
            wm = (meas.water_mask > 0) & (det.geom["flood_mask"] > 0) & (meas.object_mask == 0)
            if wm.any():
                sub = out[y:y + h, x:x + w]
                tint = sub.copy()
                tint[wm] = (230, 140, 40)
                out[y:y + h, x:x + w] = cv2.addWeighted(sub, 0.6, tint, 0.4, 0)
        poly = np.array(cal.flood_roi, np.int32)
        cv2.polylines(out, [poly], True, FLOOD_COLOR, 2, cv2.LINE_AA)
        tl = min(cal.flood_roi, key=lambda p: p[0] + p[1])
        label = "FLOOD ROI" + (f"  water {meas.flood_coverage * 100:.0f}%" if meas is not None else "")
        _text(out, label, (tl[0] + 8, tl[1] + 20), 0.45, FLOOD_COLOR, 1)
        if show_handles:
            for px, py in cal.flood_roi:
                cv2.rectangle(out, (px - 5, py - 5), (px + 5, py + 5), FLOOD_COLOR, -1)
                cv2.rectangle(out, (px - 5, py - 5), (px + 5, py + 5), (0, 0, 0), 1)
    # ---- marker ROI + virtual marker ----
    if cal.marker_roi:
        quad = np.array(cal.marker_roi, np.int32)
        cv2.polylines(out, [quad], True, MARKER_COLOR, 1, cv2.LINE_AA)
        if det.mg is not None:
            draw_virtual_marker(out, det.mg, res.level_m if res else None,
                                meas.raw_level_m if meas else None, bool(res and res.anomaly))
        # obstructions are detected ONLY inside the marker ROI
        if meas is not None and meas.objects:
            cv2.polylines(out, [np.round(np.array(cal.marker_roi)).astype(np.int32)], True,
                          (0, 140, 255), 2, cv2.LINE_AA)
            for bx, by, bw, bh in meas.objects:
                cv2.rectangle(out, (bx, by), (bx + bw, by + bh), (0, 140, 255), 2)
            bx, by = meas.objects[0][:2]
            _text(out, "OBSTRUCTION", (bx, max(12, by - 6)), 0.42, (0, 140, 255), 1)
        if virtual:                                    # no corners: drag the whole marker to move its foot
            q = order_quad(cal.marker_roi)
            foot = _pt((q[0] + q[1]) / 2)
            cv2.circle(out, foot, 5, (0, 0, 0), 3)
            cv2.circle(out, foot, 5, MARKER_COLOR, -1)
            _text(out, f"VIRTUAL MARKER  w {cal.virtual_width_m:.2f} m", (foot[0] - 40, foot[1] + 20),
                  0.38, MARKER_COLOR, 1)
        elif show_handles:                             # small hollow handles (grab radius stays 12 px)
            for px, py in cal.marker_roi:
                px, py = int(round(px)), int(round(py))
                cv2.rectangle(out, (px - 4, py - 4), (px + 4, py + 4), (0, 0, 0), 3)
                cv2.rectangle(out, (px - 4, py - 4), (px + 4, py + 4), MARKER_COLOR, 1)
    # ---- info panel ----
    layer = out.copy()
    cv2.rectangle(layer, (0, 0), (380, 190), (0, 0, 0), -1)
    cv2.addWeighted(layer, 0.6, out, 0.4, 0, out)
    font = cv2.FONT_HERSHEY_SIMPLEX
    if res is not None and status:
        cv2.putText(out, f"WATER LEVEL: {res.level_m:.2f} m", (8, 24), font, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, f"{status} ({STATUS_COLOR_NAME[status]})", (8, 50), font, 0.65,
                    STATUS_BGR[status], 2, cv2.LINE_AA)
        cv2.putText(out, f"raw {meas.raw_level_m:.2f} m  rate {res.rate_m_per_min:+.3f} m/min",
                    (8, 72), font, 0.45, (220, 220, 220), 1, cv2.LINE_AA)
        fl = "FLOOD DETECTED" if meas.flood_detected else "no flood"
        cv2.putText(out, f"{fl}  road water {meas.flood_coverage * 100:.0f}%  marker {'OBSTRUCTED' if meas.objects else 'clear'}",
                    (8, 92), font, 0.45, (230, 180, 60), 1, cv2.LINE_AA)
        if res.anomaly:
            cv2.putText(out, f"ANOMALY: {res.reason}", (8, 112), font, 0.45, (0, 140, 255), 1, cv2.LINE_AA)
    else:
        cv2.putText(out, "SETUP NEEDED", (8, 30), font, 0.7, (0, 200, 255), 2, cv2.LINE_AA)
        cv2.putText(out, "f: flood ROI  m: marker ROI  b: dry ref  s: save", (8, 56), font, 0.42,
                    (255, 255, 255), 1, cv2.LINE_AA)
    pi = det.perspective
    if pi:
        tilt = f"{pi['camera_tilt_deg_est']:.0f} deg" if pi.get("camera_tilt_deg_est") is not None else "n/a"
        cam_h = f"{pi['camera_height_m_est']:.1f} m" if pi.get("camera_height_m_est") else "n/a"
        acc = pi.get("setup_accuracy_cm_per_px", {}).get("1.0m")
        acc_txt = f"  +/-{acc:.1f}cm/px" if acc is not None else ""
        cv2.putText(out, f"tilt {tilt}  cam h {cam_h}  "
                         f"{pi['scale_px_per_m_bottom']:.0f}/{pi['scale_px_per_m_top']:.0f} px/m{acc_txt}",
                    (8, 132), font, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
    mode = ("NIGHT  " if det.is_night else "") + ("VIRTUAL marker" if virtual else "PHYSICAL marker")
    mode += "  pole: " + ("present" if cal.pole_present else "removed")
    al = alignment or {}
    if not al.get("enabled"):
        mode += "  align: off"
    elif al.get("ok"):
        mode += f"  align ok {al['dx_px']:+.1f},{al['dy_px']:+.1f} px"
    cv2.putText(out, mode, (8, 152), font, 0.38, (255, 0, 255) if virtual else (220, 220, 220), 1, cv2.LINE_AA)
    if al.get("enabled") and not al.get("ok"):
        cv2.putText(out, "ALIGN LOST", (290, 152), font, 0.38, (0, 140, 255), 1, cv2.LINE_AA)
    cp = cal.camera_pose
    if cp:
        cv2.putText(out, f"pose: tilt {cp['tilt_deg']:.1f}  roll {cp['roll_deg']:.1f}  H {cp['height_m']:.2f} m  "
                         f"D {cp.get('distance_m', 0):.2f} m  fit {cp.get('fit_px', 0):.1f}px",
                    (8, 170), font, 0.36, (180, 180, 180), 1, cv2.LINE_AA)
    else:
        cv2.putText(out, "pose: not solved (needs camera height + pole)", (8, 170), font, 0.36,
                    (180, 180, 180), 1, cv2.LINE_AA)
    cv2.putText(out, f"{fps:4.1f} fps", (320, 186), font, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
    if hint:
        _text(out, hint, (8, out.shape[0] - 10), 0.45, (0, 255, 255), 1)
    return out


# --------------------------------------------------------------------------
# CLI / main
# --------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CCTV-based flood monitoring (OpenCV + Kalman filter)")
    src = p.add_argument_group("video source (default: CAMERA_MODE in the CAMERA SETTINGS block)")
    src.add_argument("--camera", choices=["laptop", "iriun", "dahua", "file"],
                     help=f"camera to use (overrides CAMERA_MODE = '{CAMERA_MODE}')")
    src.add_argument("--list-cameras", action="store_true", help="probe local camera indexes and exit")
    src.add_argument("--dahua-ip", help=f"Dahua camera / NVR IP address (default {DAHUA_IP})")
    src.add_argument("--dahua-user", default=DAHUA_USER)
    src.add_argument("--dahua-password", default=os.environ.get("DAHUA_PASSWORD", DAHUA_PASSWORD),
                     help="or set environment variable DAHUA_PASSWORD")
    src.add_argument("--dahua-port", type=int, default=DAHUA_PORT)
    src.add_argument("--dahua-channel", type=int, default=DAHUA_CHANNEL)
    src.add_argument("--dahua-subtype", type=int, default=DAHUA_SUBTYPE, help="0 = main stream, 1 = sub stream")
    src.add_argument("--device", type=int, help="any local camera index (overrides --camera)")
    src.add_argument("--source", help="video file path or any RTSP/HTTP stream URL (overrides --camera)")
    src.add_argument("--loop", action="store_true", help="loop a video file")
    src.add_argument("--fast", action="store_true", help="process a video file as fast as possible")

    cal = p.add_argument_group("setup / calibration")
    cal.add_argument("--config", default=os.path.join(SCRIPT_DIR, "flood_config.json"))
    cal.add_argument("--camera-id", help="camera name shown in the data feed")
    cal.add_argument("--flood-roi", "--roi", dest="flood_roi",
                     help="x,y,w,h rectangle or x1,y1,x2,y2,x3,y3,... polygon (overrides config)")
    cal.add_argument("--marker-roi", help="x,y,w,h rectangle or 4 corners x1,y1,...,x4,y4 (overrides config)")
    cal.add_argument("--marker", help="(old format) base_x,base_y,top_x,top_y")
    cal.add_argument("--ref-height", type=float, help="real height (m) of the marker ROI's top edge")
    cal.add_argument("--marker-width", type=float, help="real width (m) of the marker surface (distance estimate)")
    cal.add_argument("--hfov", type=float, default=CAMERA_HFOV_DEG, help="lens horizontal field of view (deg)")
    cal.add_argument("--reference", help="dry reference image (overrides config)")
    cal.add_argument("--reference-night", help="dry reference image for NIGHT footage (overrides config)")
    cal.add_argument("--camera-height", type=float,
                     help=f"camera height above the road in m (default: setup file, else {CAMERA_HEIGHT_M})")
    cal.add_argument("--marker-distance", type=float,
                     help="horizontal camera -> pole distance in m; 0 = unknown, use --hfov "
                          f"(default: setup file, else {MARKER_DISTANCE_M})")
    cal.add_argument("--base-elev", type=float,
                     help="height (m) of the post / wall foot above the road, e.g. 0.15 for a curb")

    mf = p.add_argument_group("marker-free operation")
    mf.add_argument("--no-align", action="store_true", help="turn camera-shift compensation off")
    mf.add_argument("--align-every", type=int, default=10, help="estimate the camera shift every N frames")
    mf.add_argument("--removal-hold", type=float, default=15.0,
                    help="seconds the pole must be gone before its removal is accepted")

    det = p.add_argument_group("flood detection tuning")
    det.add_argument("--method", choices=["auto", "reference", "color"], default="auto")
    det.add_argument("--diff-thresh", type=float, default=22.0, help="Lab color distance for water vs dry road")
    det.add_argument("--row-fraction", type=float, default=0.55, help="share of a marker row that must be water")
    det.add_argument("--gap-m", type=float, default=0.06, help="tolerated non-water gap (m) while scanning up")
    det.add_argument("--mask-smooth", type=float, default=0.5, help="temporal smoothing of water mask (0-0.95)")
    det.add_argument("--min-object-area", type=float, default=0.003,
                     help="smallest moving object, as a fraction of the frame area")
    det.add_argument("--min-obstruction-height", type=float, default=0.10,
                     help="moving object must cover at least this much of the marker height (m)")
    det.add_argument("--flood-min-coverage", type=float, default=0.10,
                     help="road water share that counts as 'flood detected'")
    det.add_argument("--no-color-gate", action="store_true",
                     help="count ANY change vs the dry reference as water (not only brown / dark green)")
    # defaults tuned on real footage of the site: pale grey-brown flood water has a chroma of only
    # ~3-6 at hue ~45-70 deg, while the wet asphalt next to it is neutral / cool (hue ~300 deg)
    det.add_argument("--water-hue", type=float, nargs=2, default=[30.0, 170.0],
                     help="Lab hue range (deg) of flood water: brown ~40-85, dark green ~110-165")
    det.add_argument("--water-chroma", type=float, nargs=2, default=[3.0, 50.0],
                     help="Lab chroma range of flood water (grey asphalt < 3, vivid clothes > 50)")
    det.add_argument("--water-lightness", type=float, nargs=2, default=[8.0, 75.0],
                     help="Lab lightness range (L*, %%) of flood water (glare / white > 75)")
    det.add_argument("--color-shift", type=float, default=3.0,
                     help="a flood-colored pixel whose color moved this much TOWARD brown / green vs the dry "
                          "road is water even if --diff-thresh is not reached (murky water close to the road color)")
    det.add_argument("--color-blur", type=float, default=0.015,
                     help="average the color (not the brightness) over this fraction of the frame width")
    det.add_argument("--fill-gaps", type=float, default=0.03,
                     help="road coverage: fill gaps in the water sheet up to this fraction of the frame width")
    det.add_argument("--water-hsv-low", type=int, nargs=3, default=[5, 40, 40],
                     help="color method: brown water HSV low")
    det.add_argument("--water-hsv-high", type=int, nargs=3, default=[35, 255, 230])
    det.add_argument("--green-hsv-low", type=int, nargs=3, default=[35, 40, 20],
                     help="color method: dark green water HSV low")
    det.add_argument("--green-hsv-high", type=int, nargs=3, default=[90, 255, 160])
    det.add_argument("--light", choices=["auto", "day", "night"], default="auto",
                     help="day / night detection (auto: from the frame brightness and the two references)")
    det.add_argument("--night-level", type=float, default=105.0,
                     help="median frame brightness (0-255) below which footage counts as night "
                          "(used while only one dry reference exists)")
    det.add_argument("--night-change", type=float, default=6.0,
                     help="night: brightness change (0-255) of the road that counts as water")
    det.add_argument("--ref-adapt", type=float, default=0.002,
                     help="dry reference update speed while dry and empty (0 = off)")

    kal = p.add_argument_group("Kalman filter / anomaly")
    kal.add_argument("--meas-std", type=float, default=0.015, help="measurement noise std (m)")
    kal.add_argument("--accel-std", type=float, default=0.002, help="process noise, level acceleration std (m/s^2)")
    kal.add_argument("--gate-sigma", type=float, default=3.0, help="innovation gate in standard deviations")
    kal.add_argument("--min-jump", type=float, default=0.06, help="smallest jump (m) treated as a spike")
    kal.add_argument("--motion-thresh", type=float, default=0.25, help="object share of marker zone = obstruction")
    kal.add_argument("--persist-s", type=float, default=20.0,
                     help="seconds before a jump that came with a car / person at the marker is accepted")
    kal.add_argument("--rise-confirm", type=float, default=2.0,
                     help="seconds a steady rise must last to be accepted (x2 if the road coverage disagrees)")
    kal.add_argument("--fall-confirm", type=float, default=5.0,
                     help="seconds a steady fall must last to be accepted")
    kal.add_argument("--fall-trend", type=float, default=1.5,
                     help="seconds of continuously falling readings before the filter follows the "
                          "receding water line down (a recession never sits still, so the steady "
                          "fall confirmation alone would hold the level until the flood is gone)")
    kal.add_argument("--confirm-window", type=float, default=1.5,
                     help="seconds of readings checked for steadiness")
    kal.add_argument("--confirm-tol", type=float, default=0.03, help="steady = within this many m of the median")

    out = p.add_argument_group("data feed / output")
    out.add_argument("--print-interval", type=float, default=1.0,
                     help="seconds between terminal lines (status changes print immediately)")
    out.add_argument("--json", action="store_true", help="print the feed as JSON lines")
    out.add_argument("--no-color", action="store_true", help="plain text without terminal colors")
    out.add_argument("--log-csv", help="also append every reading to this CSV file")
    out.add_argument("--no-display", action="store_true", help="run without a video window")
    out.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def build_source(args) -> VideoSource:
    """Explicit --device / --source / --dahua-ip win; otherwise --camera, else CAMERA_MODE."""
    if args.device is not None:
        return VideoSource(args.device, "device", label=f"camera index {args.device}",
                           resolution=DEVICE_RESOLUTION)
    if args.source:
        if "://" in args.source:
            return VideoSource(args.source, "url", label="stream")
        return VideoSource(args.source, "file", realtime_file=not args.fast, loop_file=args.loop,
                           label="video file")
    mode = "dahua" if args.dahua_ip and not args.camera else (args.camera or CAMERA_MODE)
    if mode == "laptop":
        return VideoSource(LAPTOP_CAMERA_INDEX, "device", label="laptop camera",
                           resolution=DEVICE_RESOLUTION)
    if mode == "iriun":
        return VideoSource(IRIUN_CAMERA_INDEX, "device", label="Iriun phone camera",
                           resolution=DEVICE_RESOLUTION)
    if mode == "dahua":
        url = dahua_rtsp_url(args.dahua_ip or DAHUA_IP, args.dahua_user, args.dahua_password,
                             args.dahua_port, args.dahua_channel, args.dahua_subtype)
        return VideoSource(url, "dahua", label="Dahua CCTV")
    if mode == "file":
        if not VIDEO_FILE:
            raise SystemExit("CAMERA_MODE is 'file' but VIDEO_FILE is empty (or use --source path).")
        return VideoSource(VIDEO_FILE, "file", realtime_file=not args.fast, loop_file=args.loop,
                           label="video file")
    raise SystemExit(f"Unknown camera mode: {mode}")


def list_cameras(max_index: int = 5):
    """Print which local camera indexes open, so LAPTOP/IRIUN_CAMERA_INDEX can be set."""
    if os.name == "nt":
        try:
            names = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('Camera','Image') }"
                 " | ForEach-Object { $_.Name }"],
                capture_output=True, text=True, timeout=20).stdout.split("\n")
            names = [n.strip() for n in names if n.strip()]
            print("Cameras installed in Windows:", ", ".join(names) or "none found")
        except (OSError, subprocess.SubprocessError):
            pass
    print("Probing OpenCV camera indexes (the camera light may blink):")
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
        if not cap.isOpened():
            print(f"  index {i}: not available")
            continue
        frame = None
        for _ in range(10):
            ok, f = cap.read()
            if ok:
                frame = f
        cap.release()
        if frame is None:
            print(f"  index {i}: opens but gives no frames")
        else:
            h, w = frame.shape[:2]
            note = "  <- black image (Iriun not connected?)" if frame.mean() < 3 else ""
            print(f"  index {i}: {w}x{h}{note}")
    print(f"Current settings: LAPTOP_CAMERA_INDEX = {LAPTOP_CAMERA_INDEX}, "
          f"IRIUN_CAMERA_INDEX = {IRIUN_CAMERA_INDEX}, CAMERA_MODE = '{CAMERA_MODE}'")


def _floats(text):
    return [float(v) for v in text.split(",")]


def load_references(det: FloodDetector, cal: Calibration, cfg_path, frame_bgr=None):
    """Load the daylight and the night dry reference of the setup (whichever exist)."""
    for name, night in ((cal.reference_image, False), (cal.reference_image_night, True)):
        if not name:
            continue
        ref = cv2.imread(cfg_path(name))
        if ref is None:
            LOG.warning("Dry %s reference image not found: %s", "night" if night else "day", cfg_path(name))
            continue
        det.set_reference(ref, night=night)
        LOG.info("Dry %s reference loaded: %s", "NIGHT" if night else "day", cfg_path(name))
    if frame_bgr is not None:
        det.update_light(frame_bgr)
    if not det.uses_reference and det.method != "color":
        if det.is_night:
            LOG.warning("NIGHT footage without a dry night reference: press 'b' on a dry night frame.")
        else:
            LOG.warning("No dry reference frame - using the color method. Press 'b' on a dry, empty road.")


def capture_reference(det: FloodDetector, cal: Calibration, frame_bgr, cfg_path) -> str:
    """'b': the current frame becomes the dry reference of the current light (day / night)."""
    night = det.is_night
    name = f"reference_dry_{'night_' if night else ''}{cal.camera_id}.png"
    cv2.imwrite(cfg_path(name), frame_bgr)
    if night:
        cal.reference_image_night = name
    else:
        cal.reference_image = name
    det.set_reference(frame_bgr, night=night)
    LOG.info("Dry %s reference captured (%s). Press 's' to save.", "NIGHT" if night else "day", name)
    return name


def save_active_reference(det: FloodDetector, cal: Calibration, cfg_path) -> bool:
    """Write the reference of the current light back to its file (after patch_reference)."""
    name, img = ((cal.reference_image_night, det.night_bgr) if det.is_night else (cal.reference_image, det.ref_bgr))
    if not name or img is None:
        return False
    cv2.imwrite(cfg_path(name), img)
    return True


def main(argv=None):
    args = parse_args(argv)
    # Log messages go to stderr so the data feed on stdout stays clean.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.list_cameras:
        list_cameras()
        return

    cal = Calibration.load(args.config)
    if args.camera_id:
        cal.camera_id = args.camera_id
    if args.ref_height:
        cal.ref_height_m = args.ref_height
    if args.marker_width:
        cal.marker_width_m = args.marker_width
    if args.reference:
        cal.reference_image = os.path.abspath(args.reference)
    if args.reference_night:
        cal.reference_image_night = os.path.abspath(args.reference_night)
    if args.camera_height:
        cal.camera_height_m = args.camera_height
    elif cal.camera_height_m is None:
        cal.camera_height_m = CAMERA_HEIGHT_M
    if args.marker_distance is not None:
        cal.marker_distance_m = args.marker_distance or None
    elif cal.marker_distance_m is None:
        cal.marker_distance_m = MARKER_DISTANCE_M
    if args.base_elev is not None:
        cal.virtual_base_elev_m = args.base_elev
    cfg_dir = os.path.dirname(os.path.abspath(args.config))

    def cfg_path(name):
        return name if os.path.isabs(name) else os.path.join(cfg_dir, name)

    source = build_source(args)
    LOG.info("Video source: %s", source.describe())

    ok, frame, t, eos = source.read(timeout=15)
    while not ok and not eos and source.is_live:
        LOG.info("Waiting for the first frame ...")
        ok, frame, t, eos = source.read(timeout=10)
    if not ok:
        raise SystemExit("No video frames received.")
    if source.kind == "device":                   # webcams send dark frames while warming up
        t_end = time.monotonic() + 2.0
        while frame.mean() < 3 and time.monotonic() < t_end:
            ok2, f2, t2, _ = source.read(timeout=1.0)
            if ok2:
                frame, t = f2, t2
        if frame.mean() < 3:
            if "Iriun" in source.label:
                LOG.warning("Iriun camera gives a black image: open the Iriun app on the phone and on the "
                            "PC (same Wi-Fi or USB cable). Check IRIUN_CAMERA_INDEX with --list-cameras.")
            else:
                LOG.warning("%s gives a black image (lens covered or wrong index? try --list-cameras).",
                            source.describe())
    H, W = frame.shape[:2]
    cal = cal.scaled_to(W, H)
    if args.flood_roi:                            # CLI coordinates refer to the actual frame
        cal.flood_roi = normalize_polygon(_floats(args.flood_roi))
    if args.marker_roi:
        q = normalize_polygon(_floats(args.marker_roi))
        if len(q) != 4:
            raise SystemExit("--marker-roi needs a rectangle x,y,w,h or exactly 4 corners")
        cal.marker_roi = q
    elif args.marker:
        m = _floats(args.marker)
        if len(m) != 4:
            raise SystemExit("--marker needs base_x,base_y,top_x,top_y")
        cal.marker_roi = marker_quad_from_line(m[:2], m[2:])
    if args.marker_roi or args.marker:            # a marker given on the command line is a physical one
        cal.marker_mode, cal.pole_present = "physical", True
    if cal.fill_defaults(W, H):
        LOG.warning("No saved ROIs - default FLOOD ROI and MARKER ROI created. Drag them into place, "
                    "set the reference height ([ ]), then press 's'.")

    # ---- camera pose from the physical marker / virtual marker from the pose ----
    def current_pose() -> CameraPose | None:
        try:
            return CameraPose.from_dict(cal.camera_pose, W, H) if cal.camera_pose else None
        except (KeyError, TypeError, ValueError):
            return None

    def solve_pose():
        """Physical mode: the marker ROI is the pole -> solve the camera pose from it."""
        if cal.marker_mode != "physical" or not cal.marker_roi or len(cal.marker_roi) != 4:
            return
        cal.pole_roi = [list(p) for p in cal.marker_roi]
        if not cal.camera_height_m:
            cal.camera_pose = None
            return
        try:
            pose, info = CameraPose.solve(cal.marker_roi, cal.ref_height_m, cal.camera_height_m, W, H,
                                          args.hfov, cal.marker_distance_m)
        except (ValueError, np.linalg.LinAlgError) as e:
            cal.camera_pose = None
            LOG.warning("Camera pose could not be solved from the physical marker: %s", e)
            return
        cal.camera_pose = dict(pose.to_dict(), **info)
        LOG.info("Camera pose from the physical marker: tilt %.1f deg, roll %.1f deg, height %.2f m, "
                 "distance %.2f m, focal %.0f px (HFOV %.0f deg), fit %.2f px",
                 pose.tilt_deg, pose.roll_deg, pose.height, info["distance_m"], info["focal_px"],
                 info["hfov_deg"], info["fit_px"])
        if info["fit_px"] > 2.0:
            LOG.warning("The pole does not fit the camera model well (%.1f px) - check the marker corners.",
                        info["fit_px"])
        if cal.marker_distance_m and not 20 <= info["hfov_deg"] <= 140:
            LOG.warning("Solved lens field of view %.0f deg is implausible - check the camera height "
                        "(%.2f m) and the camera-to-pole distance (%.2f m).", info["hfov_deg"],
                        cal.camera_height_m, cal.marker_distance_m)

    def build_virtual(X, Y) -> bool:
        pose = current_pose()
        q = pose.marker_quad(X, Y, cal.virtual_width_m, cal.ref_height_m) if pose else None
        if q is None:
            return False
        cal.virtual_base = [round(float(X), 4), round(float(Y), 4)]
        cal.marker_roi = q
        return True

    def to_virtual_mode(X, Y) -> bool:
        prev = cal.marker_mode
        cal.marker_mode = "virtual"
        if not build_virtual(X, Y):
            cal.marker_mode = prev
            return False
        return True

    def virtual_base_px():
        pose = current_pose()
        if pose and cal.virtual_base:
            p = pose.project((cal.virtual_base[0], cal.virtual_base[1], cal.virtual_base_elev_m))
            if p is not None:
                return float(p[0]), float(p[1])
        q = order_quad(cal.marker_roi)
        return tuple((q[0] + q[1]) / 2)

    if cal.marker_mode == "physical":
        solve_pose()
    elif not (current_pose() and cal.virtual_base and build_virtual(*cal.virtual_base)):
        LOG.warning("Virtual marker setup is incomplete (no camera pose) - using the saved marker corners.")

    det = FloodDetector(args)
    det.set_calibration(cal, frame.shape, args.hfov)
    load_references(det, cal, cfg_path, frame)
    if det.perspective:
        LOG.info("Perspective compensation: %s", det.perspective)

    def save_config_field(**fields):
        """Update single fields of the saved setup file without saving unsaved edits."""
        if not os.path.isfile(args.config):
            return
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.update(fields)
            with open(args.config, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (OSError, ValueError) as e:
            LOG.warning("Could not update %s: %s", args.config, e)

    def exclude_polys():
        return [cal.flood_roi, cal.marker_roi, cal.pole_roi]

    # ---- camera-shift compensation: anchor frame = the view the setup belongs to ----
    stab = None if args.no_align else Stabilizer(args.align_every)
    anchor_img = None
    if cal.anchor_image:
        anchor_img = cv2.imread(cfg_path(cal.anchor_image))
        if anchor_img is None:
            LOG.warning("Anchor frame not found: %s", cfg_path(cal.anchor_image))
        elif anchor_img.shape[:2] != (H, W):
            anchor_img = cv2.resize(anchor_img, (W, H))
    if stab is not None:
        if anchor_img is None and not cal.is_default:
            anchor_img = frame.copy()
            cal.anchor_image = f"anchor_{cal.camera_id}.png"
            cv2.imwrite(cfg_path(cal.anchor_image), anchor_img)
            save_config_field(anchor_image=cal.anchor_image)
            LOG.info("No anchor frame yet - the current view is the reference for camera-shift "
                     "compensation (%s).", cal.anchor_image)
        if anchor_img is not None:
            stab.set_anchor(anchor_img, exclude_polys())

    # ---- detection of the physical marker being removed ----
    pole = PoleWatch(args.removal_hold)
    if cal.pole_present and cal.pole_roi and not cal.is_default:
        pole.set(cal.pole_roi, anchor_img if anchor_img is not None else frame)

    kalman = FloodKalman(args)
    tracker = StatusTracker()
    feed = TerminalFeed(args.print_interval, args.json, not args.no_color)

    csv_file = csv_writer = None
    if args.log_csv:
        new = not os.path.isfile(args.log_csv)
        csv_file = open(args.log_csv, "a", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        if new:
            csv_writer.writerow(["timestamp", "t_s", "water_level_m", "raw_level_m", "status",
                                 "flood_coverage_pct", "anomaly", "anomaly_reason", "motion_ratio",
                                 "confidence"])

    win = "Flood Monitoring"
    editor = SetupEditor(cal, W, H)
    if not args.no_display:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(win, editor.on_mouse)

    show_mask, show_water, paused = False, True, False
    fps, t_fps, n_fps = 0.0, time.monotonic(), 0
    raw_frame = frame                 # the analysed frame (camera-shift compensated)
    meas = res = status = alignment = None

    def apply_setup_change(msg, keep_level=False):
        if cal.marker_mode == "physical":
            solve_pose()
            if cal.pole_present and not cal.is_default:
                pole.set(cal.pole_roi, raw_frame)          # the pole stands there right now
        det.set_calibration(cal, raw_frame.shape, args.hfov)
        kalman.reset(keep_level)
        LOG.info("%s | mode %s | ref %.2f m | perspective %s", msg, cal.marker_mode, cal.ref_height_m,
                 det.perspective)

    def place_virtual_at(u, v, announce=False) -> bool:
        pose = current_pose()
        g = pose.ground_point(u, v, cal.virtual_base_elev_m) if pose else None
        if g is None or not to_virtual_mode(*g):
            LOG.warning("Cannot place the virtual marker there - click the FOOT of the post / wall "
                        "(on the road, below the horizon).")
            return False
        if announce:
            LOG.info("Virtual marker placed %.2f m from the camera (ground point %.2f, %.2f m). "
                     "Press 's' to save.", math.hypot(*g), g[0], g[1])
        return True

    def pole_removed(how: str):
        if not cal.pole_present:
            LOG.info("The physical marker is already marked as removed.")
            return
        cal.pole_present = False
        pole.set(None, None)
        if det.patch_reference(raw_frame, cal.pole_roi) and save_active_reference(det, cal, cfg_path):
            LOG.info("Dry %s reference updated where the physical marker stood.", det.light)
            if res is not None and meas is not None and (res.level_m >= 0.03 or meas.flood_coverage >= 0.02):
                LOG.warning("The road is not dry: water was copied into the dry reference where the pole "
                            "stood. Press 'b' once the road is dry and empty.")
        if cal.marker_mode == "physical":
            g = (cal.camera_pose or {}).get("pole_ground")
            if g and (cal.camera_pose or {}).get("pole_width_m"):
                cal.virtual_width_m = round(float(np.clip(cal.camera_pose["pole_width_m"], 0.04, 1.0)), 3)
            if not (g and to_virtual_mode(*g)):
                LOG.warning("No camera pose - the marker corners are kept as they are.")
            LOG.warning("The virtual marker stays where the pole stood, but the water line needs a VERTICAL "
                        "surface there. If that spot is open road, press 't' and click the foot of a "
                        "permanent post / wall.")
        apply_setup_change(f"Physical marker removed ({how})", keep_level=True)
        if not cal.is_default:
            cal.frame_size = [W, H]
            cal.save(args.config)

    editor.relocate = place_virtual_at
    editor.base_px = virtual_base_px

    try:
        while True:
            if not paused:
                if frame is None:
                    ok, frame, t, eos = source.read()
                    if eos:
                        LOG.info("End of video.")
                        break
                    if not ok:
                        continue
                if frame.shape[:2] != (H, W):
                    frame = cv2.resize(frame, (W, H))
                # camera-shift compensation: analysis runs in the anchor frame's coordinates
                if stab is not None:
                    stab.update(frame, t)
                    frame = stab.warp(frame)
                    if stab.reset_bg:
                        stab.reset_bg = False
                        det._reset_state()                     # the view jumped: restart motion model
                raw_frame, frame = frame, None

                # no measuring while a region is being dragged
                meas = det.measure(raw_frame) if det.ready and editor.drag is None else None
                res = status = None
                if meas is not None:
                    res = kalman.step(meas.raw_level_m, t, meas.motion_ratio, meas.flood_coverage)
                    status = tracker.update(res.level_m)
                    if (det.uses_reference and not res.anomaly and res.level_m < 0.03
                            and meas.flood_coverage < 0.02 and not meas.any_motion):
                        det.adapt_reference(raw_frame, args.ref_adapt)
                    road_dry = res.level_m < 0.10 and meas.flood_coverage < 0.05
                    if cal.pole_present and pole.update(raw_frame, t, road_dry):
                        pole_removed("detected automatically")

                n_fps += 1
                if time.monotonic() - t_fps >= 1.0:
                    fps = n_fps / (time.monotonic() - t_fps)
                    t_fps, n_fps = time.monotonic(), 0

                alignment = stab.status() if stab is not None else None
                payload = build_payload(cal, source.kind, fps, meas, res, status, det, alignment)
                feed.emit(payload, time.monotonic())
                if csv_writer and res is not None:
                    csv_writer.writerow([payload["timestamp"], f"{t:.2f}", payload["water_level_m"],
                                         payload["raw_level_m"], status, payload["flood_coverage_pct"],
                                         int(res.anomaly), res.reason or "", payload["motion_ratio"],
                                         payload["confidence"]])

            if args.no_display:
                continue

            # ---- window: redraw every loop so dragging is smooth even while paused
            if cal.is_default:
                hint = "DEFAULT ROIs: drag cyan (flood) & magenta (marker) corners, [ ] ref height, s save"
            elif editor.place is not None:
                hint = "CLICK THE FOOT of a permanent post / wall for the virtual marker"
            elif paused:
                hint = "PAUSED - drag corners / regions | right-click flood edge: add / remove vertex"
            elif cal.marker_mode == "virtual":
                hint = "drag marker to move | - = width | t re-place | m new physical marker | x pole removed | s save"
            else:
                hint = "drag corners | [ ] , . ref height | f m new ROI | b dry ref | t virtual marker | x pole removed | s save"
            view = draw_overlay(raw_frame.copy(), det, cal, res, meas, status, fps, hint, show_water,
                                alignment=alignment)
            if show_mask and meas is not None and det.geom is not None:
                x, y, w, h = det.geom["crop"]
                mv = cv2.merge([meas.object_mask, np.zeros_like(meas.water_mask), meas.water_mask])
                view[y:y + h, x:x + w] = cv2.addWeighted(view[y:y + h, x:x + w], 0.4, mv, 0.6, 0)
                rv = cv2.cvtColor(cv2.resize(meas.rect_view, (60, min(H - 20, 300))), cv2.COLOR_GRAY2BGR)
                view[10:10 + rv.shape[0], W - 70:W - 10] = rv
            cv2.imshow(win, view)
            key = cv2.waitKey(1 if not paused else 30) & 0xFF

            if editor.changed:
                editor.changed = False
                apply_setup_change("Setup updated")

            if key in (ord("q"), 27):
                break
            elif key == ord("p"):
                paused = not paused
            elif key == ord("v"):
                show_mask = not show_mask
            elif key == ord("w"):
                show_water = not show_water
            elif key in (ord("f"), ord("r"), ord("m")):
                which = "marker" if key == ord("m") else "flood"
                r = cv2.selectROI(win, raw_frame, showCrosshair=True, fromCenter=False)
                if r[2] > 10 and r[3] > 10:
                    setattr(cal, f"{which}_roi", rect_to_polygon(*r))
                    if which == "marker":                  # a new physical marker (pole)
                        cal.marker_mode, cal.pole_present = "physical", True
                    apply_setup_change(f"New {which.upper()} ROI - drag its corners to fit")
                cv2.setMouseCallback(win, editor.on_mouse)
            elif key in (ord("["), ord("]"), ord(","), ord(".")):
                if cal.marker_mode == "virtual":
                    LOG.info("The reference height belongs to the physical marker calibration and is locked "
                             "in virtual mode (press 'm' to set up a new physical marker).")
                else:
                    step = {ord("["): -0.05, ord("]"): 0.05, ord(","): -0.01, ord("."): 0.01}[key]
                    cal.ref_height_m = round(float(np.clip(cal.ref_height_m + step, 0.05, 5.0)), 2)
                    apply_setup_change("Reference height changed")
            elif key == ord("t"):
                if current_pose() is None:
                    LOG.warning("No camera pose yet: put the MARKER ROI on the physical pole and set the camera "
                                "height (CAMERA_HEIGHT_M / --camera-height) first.")
                else:
                    editor.place = lambda u, v: place_virtual_at(u, v, announce=True)
                    LOG.info("Click the FOOT of a permanent post / wall (where it meets the road).")
            elif key in (ord("-"), ord("=")) and cal.marker_mode == "virtual" and cal.virtual_base:
                old = cal.virtual_width_m
                cal.virtual_width_m = round(float(np.clip(old + (0.02 if key == ord("=") else -0.02), 0.04, 1.0)), 2)
                if build_virtual(*cal.virtual_base):
                    apply_setup_change(f"Virtual marker width {cal.virtual_width_m:.2f} m")
                else:
                    cal.virtual_width_m = old
            elif key == ord("x"):
                pole_removed("key x")
            elif key == ord("b"):
                capture_reference(det, cal, raw_frame, cfg_path)
                kalman.reset()
            elif key == ord("s"):
                cal.frame_size = [W, H]
                if stab is not None:                       # the saved setup belongs to this view
                    cal.anchor_image = f"anchor_{cal.camera_id}.png"
                    cv2.imwrite(cfg_path(cal.anchor_image), raw_frame)
                    stab.set_anchor(raw_frame, exclude_polys())
                if cal.pole_present and cal.pole_roi:
                    pole.set(cal.pole_roi, raw_frame)
                cal.save(args.config)
    except KeyboardInterrupt:
        pass
    finally:
        source.release()
        if csv_file:
            csv_file.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
