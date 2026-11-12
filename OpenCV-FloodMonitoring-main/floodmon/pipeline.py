"""
Per-frame processing WITHOUT any window.

    frame -> lens undistortion -> ArUco re-alignment -> quality / stabilisation
          -> water detection + level -> Kalman filter -> status -> payload

The interactive program (app.py), the evaluation tools and the unit tests all use
this same Monitor, so what is tested is exactly what runs on the camera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .bands import StatusTracker
from .calibration import Calibration
from .detector import FloodDetector, Measurement
from .feed import build_payload
from .kalman import FilterResult, FloodKalman
from .lens import LensModel
from .modes import VirtualMarker
from .quality import QualityReport
from .tags import TagAligner, TagDetector, fit_marker_from_tags

LOG = logging.getLogger("FloodMonitoring")


@dataclass
class FrameResult:
    frame: np.ndarray                 # the frame as measured (undistorted, aligned, stabilised)
    meas: Measurement | None
    res: FilterResult | None
    status: str | None
    quality: QualityReport
    payload: dict
    tag_info: dict | None = None


class Monitor:
    def __init__(self, args, cal: Calibration, frame_shape, source_kind: str = "file",
                 start_time: datetime | None = None, lens: LensModel | None = None,
                 tag_layout: dict | None = None, save_setup=None):
        self.args = args
        self.cal = cal
        self.source_kind = source_kind
        self.start_time = start_time
        H, W = frame_shape[:2]
        self.lens = lens
        self.undistort = lens.undistorter(W, H) if lens is not None else None
        self.det = FloodDetector(args)
        self.det.set_calibration(cal, frame_shape, args.hfov)
        self.kalman = FloodKalman(args)
        self.tracker = StatusTracker(args.status_hysteresis)
        self.vmark = VirtualMarker(cal, self.det, self.kalman, save_setup or (lambda: None))
        self.fps = 0.0
        self.last_motion_t = -1e9
        self.last_level = None
        # ---- ArUco anchor tags ----
        self.tag_layout = tag_layout
        self.tag_detector = None
        self.aligner = None
        if not args.no_tags and (tag_layout or cal.tag_anchors):
            dictionary = (tag_layout or {}).get("dictionary", "DICT_4X4_50")
            self.tag_detector = TagDetector(dictionary)
            if cal.tag_anchors:
                self.aligner = TagAligner(cal.tag_anchors, self.tag_detector, args.tag_max_err)

    # ---- setup helpers -------------------------------------------------
    def set_calibration(self, frame_shape):
        self.det.set_calibration(self.cal, frame_shape, self.args.hfov)
        self.kalman.reset()

    def prepare(self, frame):
        """Lens correction only (what the operator sets the ROIs on)."""
        return self.undistort(frame) if self.undistort is not None else frame

    def record_anchors(self, frame, overwrite: bool = False) -> int:
        """Remember where the tags are in the setup view (frame = as displayed / measured)."""
        if self.tag_detector is None or (self.cal.tag_anchors and not overwrite):
            return 0
        det = self.tag_detector.detect(frame)
        if not det:
            return 0
        self.cal.tag_anchors = {str(k): np.round(v, 3).tolist() for k, v in det.items()}
        self.aligner = TagAligner(self.cal.tag_anchors, self.tag_detector, self.args.tag_max_err)
        LOG.info("Tag anchors recorded: %s", ", ".join(str(k) for k in sorted(det)))
        return len(det)

    def fit_marker_from_tags(self, frame) -> str:
        """'g' key: place the marker ROI from the ArUco tags (physical marker mode)."""
        if self.tag_layout is None or self.tag_detector is None:
            return "No tag layout: add 'tag_layout' to the setup or start with --tags layout.json."
        if self.cal.marker_locked:
            return "The marker is locked (virtual marker) - press Shift+X to unlock it first."
        width = self.cal.marker_width_m
        if not width:
            return "Set the real marker width first (--marker-width, in meters)."
        det = self.tag_detector.detect(frame)
        fit = fit_marker_from_tags(self.tag_layout, det, width, self.cal.ref_height_m)
        if fit is None:
            return "No layout tags visible - check the tags are in view and well lit."
        self.cal.marker_roi = fit["quad"]
        self.record_anchors(frame, overwrite=True)
        return (f"Marker ROI fitted from tags {fit['tags']} (fit error {fit['rms_px']:.2f} px). "
                "Check it, capture the dry reference (b) and save (s).")

    # ---- per frame -------------------------------------------------------
    def timestamp(self, t: float) -> datetime:
        """Time the frame was recorded: video time for files, wall clock for live cameras."""
        if self.source_kind == "file" and self.start_time is not None:
            return self.start_time + timedelta(seconds=float(t))
        return datetime.now().astimezone()

    def process(self, frame, t: float, measuring: bool = True, undistorted: bool = False) -> FrameResult:
        if self.undistort is not None and not undistorted:
            frame = self.undistort(frame)
        tag_info = None
        if self.aligner is not None:
            frame, tag_info = self.aligner.update(frame)
        meas = None
        q = self.det.quality
        if measuring:
            frame, meas, q = self.det.process(frame, t)
            if meas is not None and self.vmark.blocking:          # physical marker being removed
                meas.reliable = False
                meas.unreliable_reason = "virtual marker setup in progress"
        res = status = None
        if meas is not None:
            res = self.kalman.step(meas.raw_level_m, t, meas.motion_ratio, meas.reliable,
                                   meas.unreliable_reason, meas.trust,
                                   support=meas.flood_coverage >= self.args.relock_support)
            status = self.tracker.update(res.level_m) if res.valid else self.tracker.status
            self.det.note_result(t, meas, res.level_m, res.anomaly)
            self.last_level = res.level_m
            if meas.any_motion:
                self.last_motion_t = t
        self.vmark.observe(t, meas)
        extra = {"lens_corrected": self.undistort is not None,
                 "camera_shift_px": round(float(np.hypot(*q.shift)), 2)}
        if tag_info is not None:
            extra["tags"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tag_info.items()}
        payload = build_payload(self.cal, self.source_kind, self.fps, meas, res, status, self.det,
                                timestamp=self.timestamp(t), t_s=t, extra=extra)
        return FrameResult(frame, meas, res, status, q, payload, tag_info)
