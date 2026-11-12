#!/usr/bin/env python3
"""
FloodDetection_Video.py  -  Flood monitoring on recorded footage
================================================================
Thesis: "Development of a CCTV-Based Flood Monitoring and Routing Advisory
         System Using Computer Vision"

Upload a video (existing CCTV footage, a phone recording, ...) and run the full
OpenCV pipeline of FloodMonitoring2.py on it - nothing is copied, the same
classes are used:
  * FLOOD ROI (cyan polygon) and MARKER ROI (magenta 4-corner shape)
  * physical marker -> camera pose -> VIRTUAL marker on a post / wall
  * dry reference frame, camera-shift compensation, perspective compensation
  * water level, road flood coverage, moving-object (car / person) rejection
  * day AND night footage: separate dry references ('b' takes the one for the
    current light), brown / dark-green color check by day, road-pattern /
    darkness / rippling-reflection check at night (see FloodMonitoring2.py)
  * Kalman filter with fast confirmation of real level changes
  * the live data feed in the terminal (text, or JSON lines with --json)

Only the video file is used - no CCTV / webcam connection is opened. The
footage's own clock (frame number / fps) drives the analysis, so all timings
(e.g. "rise confirmed after 2 s") are in VIDEO seconds at any playback speed.

Every video gets its own setup folder  video_setups/<video name>/  (ROIs,
dry reference, anchor frame), so the saved setup is reused when the same
footage is opened again. Each run is logged to a results CSV in that folder
and a summary is printed at the end.

Run
---
    python FloodDetection_Video.py                      (opens a file picker)
    python FloodDetection_Video.py footage.mp4
    python FloodDetection_Video.py footage.mp4 --copy-setup ../flood_config.json
    python FloodDetection_Video.py footage.mp4 --no-display      (batch: feed + summary)
    python FloodDetection_Video.py night_flood.mp4 --reference-night dry_night_frame.png
  The video path must come first. All detection / Kalman options of
  FloodMonitoring2.py work here too (e.g. --rise-confirm 2 --diff-thresh 22 --json),
  see  python FloodMonitoring2.py --help

First time with a video
-----------------------
  The video starts PAUSED on the first frame with default ROIs:
    1. drag the FLOOD ROI onto the road and the MARKER ROI onto the pole /
       post (bottom edge = road, top edge = REF HEIGHT, [ ] to change it)
    2. seek to a moment where the road is DRY and empty, press b
    3. press s to save, SPACE to play

Keys (video window)
-------------------
  Setup (same as FloodMonitoring2.py)
    f / m : new FLOOD ROI / MARKER ROI      drag handles / regions to adjust
    [ ] , . : REF HEIGHT -/+ 0.05 / 0.01 m  b : dry reference (current frame)
    t : virtual marker on a post / wall     x : physical marker removed
    - / = : virtual marker width            s : save setup
    w : water overlay on/off                v : mask debug view
  Video
    SPACE / p : play / pause                n : next frame (while paused)
    a / d : -5 s / +5 s     A / D : -30 s / +30 s      (or drag the seek bar)
    1 2 3 4 : speed x0.5 / x1 / x2 / max    l : loop on/off
    g : restart from the beginning          o : open another video
    q / ESC : quit
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import FloodMonitoring2 as fm  # noqa: E402  (the OpenCV pipeline: ROIs, markers, detector, Kalman, feed)

LOG = logging.getLogger("FloodVideo")

VIDEO_TYPES = "*.mp4 *.avi *.mov *.mkv *.wmv *.m4v *.mpg *.mpeg *.ts *.dav *.asf *.flv *.webm *.3gp"
SPEEDS = {ord("1"): 0.5, ord("2"): 1.0, ord("3"): 2.0, ord("4"): 0.0}   # 0 = as fast as possible
SEEK_BAR = "seek (s)"


def fmt_time(t: float | None) -> str:
    if t is None:
        return "--:--"
    t = max(0.0, t)
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}"


def pick_video(initial_dir: str | None = None) -> str | None:
    """File dialog to upload / choose a video."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        LOG.error("tkinter is not available - pass the video path on the command line.")
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Upload flood footage", initialdir=initial_dir or os.getcwd(),
        filetypes=[("Video files", VIDEO_TYPES), ("All files", "*.*")])
    root.destroy()
    return path or None


def copy_setup(src_cfg: str, dst_cfg: str, camera_id: str):
    """Start a per-video setup from an existing one (ROIs + camera pose). The dry reference
    and the anchor frame belong to the other view / lighting, so they are not copied."""
    with open(src_cfg, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.pop("reference_image", None)
    data.pop("anchor_image", None)
    data["camera_id"] = camera_id
    with open(dst_cfg, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    LOG.info("Setup copied from %s (ROIs + camera pose). Capture a dry reference with 'b' on a dry "
             "frame of this video.", src_cfg)


class VideoFile:
    """Frame-accurate video reader; time = frame index / fps (the footage's own clock)."""

    def __init__(self, path: str):
        self.path = path
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open the video: {path}"
                               + ("  (.dav: export / convert it to .mp4 with the Dahua player or ffmpeg)"
                                  if path.lower().endswith(".dav") else ""))
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and 1 <= fps <= 240 else 25.0
        n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.frames = n if n > 0 else None
        self.idx = 0                                     # index of the next frame

    @property
    def duration(self) -> float | None:
        return self.frames / self.fps if self.frames else None

    def read(self):
        """Return (ok, frame, video_time_s)."""
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return False, None, self.idx / self.fps
        t = self.idx / self.fps
        self.idx += 1
        return True, frame, t

    def seek(self, t: float):
        i = max(0, int(round(t * self.fps)))
        if self.frames:
            i = min(i, self.frames - 1)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        self.idx = i

    def release(self):
        self.cap.release()


class VideoFeed(fm.TerminalFeed):
    """The FloodMonitoring2 terminal feed, stamped with the video time instead of the wall clock."""

    def format_line(self, d: dict, event: str | None) -> str:
        return d["video_time"] + super().format_line(d, event)[8:]


class RunStats:
    """Summary of one run over the footage (video seconds)."""

    def __init__(self):
        self.t_prev = None
        self.analysed_s = 0.0
        self.status_s = collections.Counter()
        self.max_level, self.t_max, self.status_max = 0.0, None, "SAFE"
        self.first_flood = None
        self.episodes = 0
        self._anomaly = False

    def restart_clock(self):
        self.t_prev = None

    def add(self, t: float, res, status: str, meas):
        dt = 0.0 if self.t_prev is None else float(np.clip(t - self.t_prev, 0.0, 1.0))
        self.t_prev = t
        self.analysed_s += dt
        self.status_s[status] += dt
        if res.level_m > self.max_level:
            self.max_level, self.t_max, self.status_max = res.level_m, t, status
        if self.first_flood is None and meas.flood_detected and not res.anomaly:
            self.first_flood = t
        if res.anomaly and not self._anomaly:
            self.episodes += 1
        self._anomaly = res.anomaly

    def report(self, name: str, csv_path: str | None) -> str:
        lines = [f"==== Summary: {name}  ({fmt_time(self.analysed_s)} of video analysed) ====",
                 f"  max water level   {self.max_level:.2f} m"
                 + (f" at {fmt_time(self.t_max)} ({self.status_max})" if self.t_max is not None else ""),
                 f"  first flood       {fmt_time(self.first_flood) if self.first_flood is not None else 'none'}",
                 "  time per status   " + " | ".join(f"{s} {fmt_time(self.status_s[s])}" for s in fm.STATUS_ORDER),
                 f"  anomaly / confirmation episodes: {self.episodes}"]
        if csv_path:
            lines.append(f"  results CSV       {csv_path}")
        return "\n".join(lines)


class VideoSession:
    """One video: setup, analysis loop, playback controls. Mirrors FloodMonitoring2.main()."""

    def __init__(self, path: str, vargs, args):
        self.path, self.vargs, self.args = path, vargs, args
        self.name = os.path.basename(path)
        self.display = not args.no_display
        stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(self.name)[0]) or "video"

        # ---- setup file: per video (default) or a shared one (--config) ----
        if vargs.config:
            self.cfg = os.path.abspath(vargs.config)
        else:
            d = os.path.join(vargs.setup_dir, stem)
            os.makedirs(d, exist_ok=True)
            self.cfg = os.path.join(d, "flood_config.json")
            if vargs.copy_setup and not os.path.isfile(self.cfg):
                copy_setup(vargs.copy_setup, self.cfg, stem[:32])
        self.cfg_dir = os.path.dirname(self.cfg)
        new_setup = not os.path.isfile(self.cfg)
        cal = fm.Calibration.load(self.cfg)
        if new_setup:
            cal.camera_id = stem[:32]
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
            cal.camera_height_m = fm.CAMERA_HEIGHT_M
        if args.marker_distance is not None:
            cal.marker_distance_m = args.marker_distance or None
        elif cal.marker_distance_m is None:
            cal.marker_distance_m = fm.MARKER_DISTANCE_M
        if args.base_elev is not None:
            cal.virtual_base_elev_m = args.base_elev

        # ---- video ----
        self.src = VideoFile(path)
        if vargs.start > 0:
            self.src.seek(vargs.start)
        ok, frame, t = self.src.read()
        if not ok:
            raise RuntimeError(f"No readable frames in {path}")
        self.H, self.W = frame.shape[:2]
        LOG.info("Video: %s  %dx%d  %.1f fps  %s  | setup: %s", self.name, self.W, self.H, self.src.fps,
                 fmt_time(self.src.duration), self.cfg)
        cal = cal.scaled_to(self.W, self.H)
        if cal.fill_defaults(self.W, self.H):
            LOG.warning("No saved ROIs for this video - default FLOOD ROI and MARKER ROI created. Drag them "
                        "into place, press 'b' on a dry frame, then 's' to save.")
        self.cal = cal

        # ---- camera pose / virtual marker ----
        if cal.marker_mode == "physical":
            self.solve_pose()
        elif not (self.current_pose() and cal.virtual_base and self.build_virtual(*cal.virtual_base)):
            LOG.warning("Virtual marker setup is incomplete (no camera pose) - using the saved marker corners.")

        # ---- detector + dry reference ----
        self.det = fm.FloodDetector(args)
        self.det.set_calibration(cal, frame.shape, args.hfov)
        fm.load_references(self.det, cal, self.cfg_path, frame)     # day + night references

        # ---- camera-shift compensation (anchor = the frame the setup was saved on) ----
        self.stab = None if args.no_align else fm.Stabilizer(args.align_every)
        anchor_img = None
        if cal.anchor_image:
            anchor_img = cv2.imread(self.cfg_path(cal.anchor_image))
            if anchor_img is not None and anchor_img.shape[:2] != (self.H, self.W):
                anchor_img = cv2.resize(anchor_img, (self.W, self.H))
        if self.stab is not None:
            if anchor_img is None and not cal.is_default:
                anchor_img = frame.copy()
                cal.anchor_image = f"anchor_{cal.camera_id}.png"
                cv2.imwrite(self.cfg_path(cal.anchor_image), anchor_img)
                self.save_config_field(anchor_image=cal.anchor_image)
            if anchor_img is not None:
                self.stab.set_anchor(anchor_img, self.exclude_polys())

        self.pole = fm.PoleWatch(args.removal_hold)
        if cal.pole_present and cal.pole_roi and not cal.is_default:
            self.pole.set(cal.pole_roi, anchor_img if anchor_img is not None else frame)

        self.kalman = fm.FloodKalman(args)
        self.tracker = fm.StatusTracker()
        self.feed = VideoFeed(args.print_interval, args.json, not args.no_color)
        self.stats = RunStats()
        self.editor = fm.SetupEditor(cal, self.W, self.H)
        self.editor.relocate = self.place_virtual_at
        self.editor.base_px = self.virtual_base_px

        # ---- results CSV ----
        self.csv_path = self.csv_file = self.csv_writer = None
        if not vargs.no_log:
            self.csv_path = os.path.join(self.cfg_dir, f"results_{datetime.now():%Y%m%d_%H%M%S}.csv")
            self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(["video_time_s", "video_time", "water_level_m", "raw_level_m", "status",
                                      "flood_coverage_pct", "level_state", "pending_level_m", "anomaly_reason",
                                      "motion_ratio", "confidence"])

        # ---- playback state ----
        self.first = (frame, t)
        self.raw_frame, self.t = frame, t
        self.meas = self.res = self.status = self.alignment = None
        self.paused = self.display and (cal.is_default or not self.det.uses_reference)
        self.step_once = False
        self.pending_seek = None
        self.speed = 0.0 if not self.display else vargs.speed
        self.loop = vargs.loop
        self.at_end = False
        self.clock = None                    # (wall time, video time) the playback pace is anchored to
        self.show_mask, self.show_water = False, True
        self.fps, self.t_fps, self.n_fps = 0.0, time.monotonic(), 0
        self.has_seek_bar = False
        self._seek_bar_busy = False
        self._seek_bar_pos = -1
        self.summary_done = False
        self.win = f"Flood Monitoring - {self.name}"
        if self.paused:
            LOG.info("Video PAUSED for setup: set the ROIs, 'b' on a dry frame, 's' to save, SPACE to play.")

    # ---- setup helpers (same behaviour as FloodMonitoring2.main) ----------
    def cfg_path(self, name: str) -> str:
        return name if os.path.isabs(name) else os.path.join(self.cfg_dir, name)

    def save_config_field(self, **fields):
        if not os.path.isfile(self.cfg):
            return
        try:
            with open(self.cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.update(fields)
            with open(self.cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (OSError, ValueError) as e:
            LOG.warning("Could not update %s: %s", self.cfg, e)

    def exclude_polys(self):
        return [self.cal.flood_roi, self.cal.marker_roi, self.cal.pole_roi]

    def current_pose(self) -> fm.CameraPose | None:
        try:
            return fm.CameraPose.from_dict(self.cal.camera_pose, self.W, self.H) if self.cal.camera_pose else None
        except (KeyError, TypeError, ValueError):
            return None

    def solve_pose(self):
        """Physical mode: the marker ROI is the pole -> solve the camera pose from it."""
        cal = self.cal
        if cal.marker_mode != "physical" or not cal.marker_roi or len(cal.marker_roi) != 4:
            return
        cal.pole_roi = [list(p) for p in cal.marker_roi]
        if not cal.camera_height_m:
            cal.camera_pose = None
            return
        try:
            pose, info = fm.CameraPose.solve(cal.marker_roi, cal.ref_height_m, cal.camera_height_m,
                                             self.W, self.H, self.args.hfov, cal.marker_distance_m)
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

    def build_virtual(self, X, Y) -> bool:
        pose = self.current_pose()
        q = pose.marker_quad(X, Y, self.cal.virtual_width_m, self.cal.ref_height_m) if pose else None
        if q is None:
            return False
        self.cal.virtual_base = [round(float(X), 4), round(float(Y), 4)]
        self.cal.marker_roi = q
        return True

    def to_virtual_mode(self, X, Y) -> bool:
        prev = self.cal.marker_mode
        self.cal.marker_mode = "virtual"
        if not self.build_virtual(X, Y):
            self.cal.marker_mode = prev
            return False
        return True

    def virtual_base_px(self):
        pose, cal = self.current_pose(), self.cal
        if pose and cal.virtual_base:
            p = pose.project((cal.virtual_base[0], cal.virtual_base[1], cal.virtual_base_elev_m))
            if p is not None:
                return float(p[0]), float(p[1])
        q = fm.order_quad(cal.marker_roi)
        return tuple((q[0] + q[1]) / 2)

    def apply_setup_change(self, msg: str, keep_level: bool = False):
        cal = self.cal
        if cal.marker_mode == "physical":
            self.solve_pose()
            if cal.pole_present and not cal.is_default:
                self.pole.set(cal.pole_roi, self.raw_frame)
        self.det.set_calibration(cal, self.raw_frame.shape, self.args.hfov)
        self.kalman.reset(keep_level)
        LOG.info("%s | mode %s | ref %.2f m", msg, cal.marker_mode, cal.ref_height_m)
        if self.paused:                      # show the effect on the paused frame right away
            self.analyse(self.raw_frame, self.t)

    def place_virtual_at(self, u, v, announce=False) -> bool:
        pose = self.current_pose()
        g = pose.ground_point(u, v, self.cal.virtual_base_elev_m) if pose else None
        if g is None or not self.to_virtual_mode(*g):
            LOG.warning("Cannot place the virtual marker there - click the FOOT of the post / wall "
                        "(on the road, below the horizon).")
            return False
        if announce:
            LOG.info("Virtual marker placed %.2f m from the camera. Press 's' to save.", math.hypot(*g))
        return True

    def pole_removed(self, how: str):
        cal, det = self.cal, self.det
        if not cal.pole_present:
            LOG.info("The physical marker is already marked as removed.")
            return
        cal.pole_present = False
        self.pole.set(None, None)
        if det.patch_reference(self.raw_frame, cal.pole_roi) and fm.save_active_reference(det, cal, self.cfg_path):
            LOG.info("Dry %s reference updated where the physical marker stood.", det.light)
            if self.res is not None and self.meas is not None and (
                    self.res.level_m >= 0.03 or self.meas.flood_coverage >= 0.02):
                LOG.warning("The road is not dry: water was copied into the dry reference where the pole "
                            "stood. Seek to a dry frame and press 'b'.")
        if cal.marker_mode == "physical":
            g = (cal.camera_pose or {}).get("pole_ground")
            if g and (cal.camera_pose or {}).get("pole_width_m"):
                cal.virtual_width_m = round(float(np.clip(cal.camera_pose["pole_width_m"], 0.04, 1.0)), 3)
            if not (g and self.to_virtual_mode(*g)):
                LOG.warning("No camera pose - the marker corners are kept as they are.")
            LOG.warning("The virtual marker stays where the pole stood; if that spot is open road, press 't' "
                        "and click the foot of a permanent post / wall.")
        self.apply_setup_change(f"Physical marker removed ({how})", keep_level=True)
        if not cal.is_default:
            cal.frame_size = [self.W, self.H]
            cal.save(self.cfg)

    # ---- analysis ----------------------------------------------------------
    def prepare(self, frame, t):
        """Camera-shift compensation: the analysis runs in the anchor frame's coordinates."""
        if self.stab is not None:
            self.stab.update(frame, t)
            frame = self.stab.warp(frame)
            if self.stab.reset_bg:
                self.stab.reset_bg = False
                self.det._reset_state()
        return frame

    def analyse(self, frame, t: float):
        det, args = self.det, self.args
        self.raw_frame, self.t = frame, t
        meas = det.measure(frame) if det.ready and self.editor.drag is None else None
        res = status = None
        if meas is not None:
            res = self.kalman.step(meas.raw_level_m, t, meas.motion_ratio, meas.flood_coverage)
            status = self.tracker.update(res.level_m)
            if (det.uses_reference and not res.anomaly and res.level_m < 0.03
                    and meas.flood_coverage < 0.02 and not meas.any_motion):
                det.adapt_reference(frame, args.ref_adapt)
            self.stats.add(t, res, status, meas)
        self.meas, self.res, self.status = meas, res, status
        if meas is not None and self.cal.pole_present:
            road_dry = res.level_m < 0.10 and meas.flood_coverage < 0.05
            if self.pole.update(frame, t, road_dry):
                self.pole_removed("detected automatically")

        self.n_fps += 1
        if time.monotonic() - self.t_fps >= 1.0:
            self.fps = self.n_fps / (time.monotonic() - self.t_fps)
            self.t_fps, self.n_fps = time.monotonic(), 0
        self.alignment = self.stab.status() if self.stab is not None else None
        payload = fm.build_payload(self.cal, "file", self.fps, self.meas, self.res, self.status, det,
                                   self.alignment)
        payload.update(video_file=self.name, video_time_s=round(t, 2), video_time=fmt_time(t))
        self.feed.emit(payload, time.monotonic())
        if self.csv_writer and self.res is not None:
            self.csv_writer.writerow([f"{t:.2f}", payload["video_time"], payload["water_level_m"],
                                      payload["raw_level_m"], self.status, payload["flood_coverage_pct"],
                                      payload["level_state"], payload["pending_level_m"],
                                      self.res.reason or "", payload["motion_ratio"], payload["confidence"]])

    # ---- playback ----------------------------------------------------------
    def seek(self, t: float):
        dur = self.src.duration
        t = float(np.clip(t, 0.0, max(0.0, dur - 1.0 / self.src.fps) if dur else max(t, 0.0)))
        self.src.seek(t)
        # the scene jumped: restart the motion model and the filter, keep the setup
        self.det._reset_state()
        self.kalman.reset()
        self.tracker = fm.StatusTracker()
        self.pole.reset()
        self.stats.restart_clock()
        self.at_end = False
        self.clock = None
        self.step_once = True
        LOG.info("Video position %s", fmt_time(t))

    def frame_wait_ms(self) -> int:
        """How long to wait so playback follows the video clock x speed."""
        if self.paused or self.at_end:
            return 30
        if self.speed <= 0:
            return 1
        now = time.monotonic()
        if self.clock is None:
            self.clock = (now, self.t)
        due = self.clock[0] + (self.t - self.clock[1]) / self.speed
        if now - due > 0.5:                  # processing is slower than real time: re-anchor
            self.clock = (now, self.t)
            return 1
        return max(1, int((due - now) * 1000))

    def _on_seek_bar(self, pos: int):
        if not self._seek_bar_busy:
            self.pending_seek = float(pos)

    def render(self):
        cal = self.cal
        if cal.is_default:
            hint = "DEFAULT ROIs: drag cyan (flood) & magenta (marker) corners, [ ] ref height, b dry ref, s save"
        elif self.editor.place is not None:
            hint = "CLICK THE FOOT of a permanent post / wall for the virtual marker"
        elif cal.marker_mode == "virtual":
            hint = "drag marker to move | - = width | t re-place | m new physical marker | x pole removed | s save"
        else:
            hint = "drag corners | [ ] , . ref height | f m new ROI | b dry ref | t virtual marker | x pole removed | s save"
        view = fm.draw_overlay(self.raw_frame.copy(), self.det, cal, self.res, self.meas, self.status,
                               self.fps, hint, self.show_water, alignment=self.alignment)
        if self.show_mask and self.meas is not None and self.det.geom is not None:
            x, y, w, h = self.det.geom["crop"]
            m = self.meas
            mv = cv2.merge([m.object_mask, np.zeros_like(m.water_mask), m.water_mask])
            view[y:y + h, x:x + w] = cv2.addWeighted(view[y:y + h, x:x + w], 0.4, mv, 0.6, 0)
            rv = cv2.cvtColor(cv2.resize(m.rect_view, (60, min(self.H - 20, 300))), cv2.COLOR_GRAY2BGR)
            view[10:10 + rv.shape[0], self.W - 70:self.W - 10] = rv
        state = "END" if self.at_end else "PAUSED" if self.paused else "PLAYING"
        speed = "max" if self.speed <= 0 else f"x{self.speed:g}"
        line = (f"VIDEO {fmt_time(self.t)} / {fmt_time(self.src.duration)}  {speed}  {state}"
                + ("  LOOP" if self.loop else "")
                + "  |  SPACE play/pause  a d -/+5s  A D -/+30s  n frame  1-4 speed  l loop  g restart  o open  q quit")
        fm._text(view, line, (8, self.H - 34), 0.45, (255, 255, 255), 1)
        return view

    def handle_key(self, key: int):
        """Returns "quit", a path to open next, or None."""
        cal, args = self.cal, self.args
        if key in (ord("q"), 27):
            return "quit"
        if key in (ord(" "), ord("p")):
            if self.at_end:
                self.seek(0.0)
                self.paused = False
            else:
                self.paused = not self.paused
            self.clock = None
        elif key == ord("n") and self.paused:
            self.step_once = True
        elif key in (ord("a"), ord("d"), ord("A"), ord("D")):
            step = {ord("a"): -5, ord("d"): 5, ord("A"): -30, ord("D"): 30}[key]
            self.pending_seek = self.t + step
        elif key == ord("g"):
            self.pending_seek = 0.0
        elif key in SPEEDS:
            self.speed, self.clock = SPEEDS[key], None
            LOG.info("Playback speed %s", "max" if self.speed <= 0 else f"x{self.speed:g}")
        elif key == ord("l"):
            self.loop = not self.loop
            LOG.info("Loop %s", "on" if self.loop else "off")
        elif key == ord("o"):
            nxt = pick_video(os.path.dirname(self.path))
            if nxt:
                return nxt
        elif key == ord("v"):
            self.show_mask = not self.show_mask
        elif key == ord("w"):
            self.show_water = not self.show_water
        elif key in (ord("f"), ord("r"), ord("m")):
            which = "marker" if key == ord("m") else "flood"
            r = cv2.selectROI(self.win, self.raw_frame, showCrosshair=True, fromCenter=False)
            if r[2] > 10 and r[3] > 10:
                setattr(cal, f"{which}_roi", fm.rect_to_polygon(*r))
                if which == "marker":                      # a new physical marker (pole)
                    cal.marker_mode, cal.pole_present = "physical", True
                self.apply_setup_change(f"New {which.upper()} ROI - drag its corners to fit")
            cv2.setMouseCallback(self.win, self.editor.on_mouse)
        elif key in (ord("["), ord("]"), ord(","), ord(".")):
            if cal.marker_mode == "virtual":
                LOG.info("The reference height belongs to the physical marker calibration and is locked "
                         "in virtual mode (press 'm' to set up a new physical marker).")
            else:
                step = {ord("["): -0.05, ord("]"): 0.05, ord(","): -0.01, ord("."): 0.01}[key]
                cal.ref_height_m = round(float(np.clip(cal.ref_height_m + step, 0.05, 5.0)), 2)
                self.apply_setup_change("Reference height changed")
        elif key == ord("t"):
            if self.current_pose() is None:
                LOG.warning("No camera pose yet: put the MARKER ROI on the physical pole and set the camera "
                            "height (--camera-height) first.")
            else:
                self.editor.place = lambda u, v: self.place_virtual_at(u, v, announce=True)
                LOG.info("Click the FOOT of a permanent post / wall (where it meets the road).")
        elif key in (ord("-"), ord("=")) and cal.marker_mode == "virtual" and cal.virtual_base:
            old = cal.virtual_width_m
            cal.virtual_width_m = round(float(np.clip(old + (0.02 if key == ord("=") else -0.02), 0.04, 1.0)), 2)
            if self.build_virtual(*cal.virtual_base):
                self.apply_setup_change(f"Virtual marker width {cal.virtual_width_m:.2f} m")
            else:
                cal.virtual_width_m = old
        elif key == ord("x"):
            self.pole_removed("key x")
        elif key == ord("b"):
            fm.capture_reference(self.det, cal, self.raw_frame, self.cfg_path)   # day or night reference
            self.kalman.reset()
            LOG.info("(reference frame taken at video time %s)", fmt_time(self.t))
            if self.paused:
                self.analyse(self.raw_frame, self.t)
        elif key == ord("s"):
            cal.frame_size = [self.W, self.H]
            if self.stab is not None:                      # the saved setup belongs to this view
                cal.anchor_image = f"anchor_{cal.camera_id}.png"
                cv2.imwrite(self.cfg_path(cal.anchor_image), self.raw_frame)
                self.stab.set_anchor(self.raw_frame, self.exclude_polys())
            if cal.pole_present and cal.pole_roi:
                self.pole.set(cal.pole_roi, self.raw_frame)
            cal.save(self.cfg)
        return None

    def print_summary(self):
        if self.summary_done:
            return
        self.summary_done = True
        print("\n" + self.stats.report(self.name, self.csv_path), file=sys.stderr if self.args.json else sys.stdout,
              flush=True)

    def run(self) -> str | None:
        """Play / analyse the video. Returns the next video to open ('o'), else None."""
        if self.display:
            cv2.namedWindow(self.win, cv2.WINDOW_NORMAL)
            cv2.setMouseCallback(self.win, self.editor.on_mouse)
            if self.src.duration:
                try:
                    cv2.createTrackbar(SEEK_BAR, self.win, 0, max(1, int(self.src.duration)), self._on_seek_bar)
                    self.has_seek_bar = True
                except cv2.error:
                    LOG.info("Seek bar not available in this OpenCV build - use a / d to seek.")
        frame, t = self.first
        self.analyse(self.prepare(frame, t), t)
        nxt = None
        try:
            while True:
                if self.pending_seek is not None:
                    target, self.pending_seek = self.pending_seek, None
                    self.seek(target)
                if not (self.paused or self.at_end) or self.step_once:
                    self.step_once = False
                    ok, frame, t = self.src.read()
                    if ok:
                        self.analyse(self.prepare(frame, t), t)
                    elif self.loop and self.src.idx > 0:
                        LOG.info("End of video - looping.")
                        self.seek(0.0)
                        continue
                    elif not self.display:
                        break
                    elif not self.at_end:
                        self.at_end = True
                        LOG.info("End of video. SPACE / g: play again, o: open another video, q: quit.")
                        self.print_summary()
                if not self.display:
                    continue

                wait = self.frame_wait_ms()
                cv2.imshow(self.win, self.render())
                if self.has_seek_bar and int(self.t) != self._seek_bar_pos:
                    self._seek_bar_busy, self._seek_bar_pos = True, int(self.t)
                    cv2.setTrackbarPos(SEEK_BAR, self.win, self._seek_bar_pos)
                    self._seek_bar_busy = False
                key = cv2.waitKey(wait) & 0xFF
                if self.editor.changed:
                    self.editor.changed = False
                    self.apply_setup_change("Setup updated")
                r = self.handle_key(key) if key != 255 else None
                if r == "quit":
                    break
                if r:
                    nxt = r
                    break
                try:
                    if cv2.getWindowProperty(self.win, cv2.WND_PROP_VISIBLE) < 1:
                        break                              # window closed with the X button
                except cv2.error:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.src.release()
            if self.csv_file:
                self.csv_file.close()
            if self.display:
                cv2.destroyWindow(self.win)
        self.print_summary()
        return nxt


def parse_args(argv):
    """Video options here; every other option is a FloodMonitoring2.py option (detection, Kalman, feed)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    video = argv.pop(0) if argv and not argv[0].startswith("-") else None
    p = argparse.ArgumentParser(
        description="Flood monitoring on uploaded video footage (OpenCV pipeline of FloodMonitoring2.py)",
        usage="%(prog)s [video] [options]",
        epilog="All detection / Kalman / feed options of FloodMonitoring2.py are accepted too, e.g. "
               "--rise-confirm 2 --persist-s 20 --diff-thresh 22 --json --no-display "
               "(see python FloodMonitoring2.py --help).")
    p.add_argument("--setup-dir", default=os.path.join(SCRIPT_DIR, "video_setups"),
                   help="folder for the per-video setups and results (default: video_setups next to this file)")
    p.add_argument("--config", help="use this setup file instead of the per-video one (shared - 's' overwrites it)")
    p.add_argument("--copy-setup", help="start a NEW per-video setup from this setup file "
                                        "(ROIs + camera pose; not the dry reference / anchor frame)")
    p.add_argument("--speed", type=float, default=1.0, help="playback speed (0 = as fast as possible)")
    p.add_argument("--start", type=float, default=0.0, help="start at this video time (s)")
    p.add_argument("--loop", action="store_true", help="loop the video")
    p.add_argument("--no-log", action="store_true", help="do not write the results CSV")
    vargs, rest = p.parse_known_args(argv)
    return video, vargs, fm.parse_args(rest)


def main(argv=None):
    video, vargs, args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    path = video or (pick_video() if not args.no_display else None)
    if not path:
        raise SystemExit("No video chosen.")
    while path:
        if not os.path.isfile(path):
            raise SystemExit(f"Video not found: {path}")
        try:
            session = VideoSession(os.path.abspath(path), vargs, args)
        except RuntimeError as e:
            LOG.error("%s", e)
            path = pick_video(os.path.dirname(os.path.abspath(path))) if not args.no_display else None
            continue
        if args.no_display and session.cal.is_default:
            raise SystemExit("No saved setup for this video - run it once with the window to set the ROIs "
                             "(or use --config / --copy-setup).")
        path = session.run()
        vargs.copy_setup = None                  # only for the first video


if __name__ == "__main__":
    main()