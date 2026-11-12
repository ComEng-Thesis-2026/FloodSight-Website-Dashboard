"""Marker geometry: homography between the image and the real marker surface."""

from __future__ import annotations

import math

import cv2
import numpy as np

from .bands import MARKER_MAX_M
from .calibration import order_quad

RECT_PPM = 400      # rectified marker: rows per meter (1 row = 2.5 mm)
RECT_W = 48         # rectified marker: columns across the marker


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

    def cm_per_px_at(self, h: float) -> float:
        """Real resolution at height h: how many cm of water depth one image pixel represents.
        This, not the 2.5 mm rectified row, is the honest precision of a reading."""
        h0 = float(np.clip(h - 0.05, 0.0, self.max_m - 0.1))
        return round(100.0 / max(self.px_per_m(h0, h0 + 0.1), 1e-6), 2)

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
