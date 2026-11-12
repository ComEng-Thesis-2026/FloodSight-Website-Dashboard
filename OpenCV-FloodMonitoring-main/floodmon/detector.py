"""Flood detection + water level measurement on the marker."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import cv2
import numpy as np

from .calibration import Calibration
from .geometry import RECT_PPM, MarkerGeometry
from .quality import SMALL, QualityMonitor, QualityReport, RefImage, classify_lighting

LOG = logging.getLogger("FloodMonitoring")


@dataclass
class Measurement:
    raw_level_m: float
    motion_ratio: float          # share of the marker zone covered by moving objects
    confidence: float
    flood_coverage: float        # share of the flood ROI covered by water (0..1)
    flood_detected: bool
    objects: list                # obstruction boxes [x, y, w, h] INSIDE the marker ROI only (frame coords)
    any_motion: bool = False     # something moved anywhere in the work area (internal use only)
    reliable: bool = True        # False -> do not use this reading (bad image / marker not visible)
    unreliable_reason: str | None = None
    trust: float = 1.0           # 0..1 how much the Kalman filter should trust this reading
    method: str = "scan"         # "scan" (pixel scan) or "step" (illumination-invariant water-line step)
    scan_level_m: float | None = None      # level found by the pixel scan (diagnostics)
    step_level_m: float | None = None      # level found by the step method (diagnostics)
    resolution_cm_per_px: float | None = None   # real precision at the water line (cm per image pixel)
    water_mask: np.ndarray | None = None   # work-crop coordinates
    object_mask: np.ndarray | None = None
    rect_view: np.ndarray | None = None    # rectified marker (debug)


def _prefix_linear_sse(y, x):
    """Linear-fit squared error of every prefix y[:k] (k = 1..n) of a multi-channel series."""
    n = len(y)
    k = np.arange(1, n + 1, dtype=np.float64)[:, None]
    Sx, Sxx = np.cumsum(x)[:, None], np.cumsum(x * x)[:, None]
    Sy, Syy, Sxy = np.cumsum(y, axis=0), np.cumsum(y * y, axis=0), np.cumsum(x[:, None] * y, axis=0)
    den = k * Sxx - Sx * Sx
    den = np.where(np.abs(den) < 1e-9, np.inf, den)
    b = (k * Sxy - Sx * Sy) / den
    a = (Sy - b * Sx) / k
    sse = Syy - 2 * a * Sy - 2 * b * Sxy + k * a * a + 2 * a * b * Sx + b * b * Sxx
    return np.maximum(sse, 0).sum(axis=1)


def waterline_step(cur_r, ref_r, usable, min_rows):
    """
    Illumination-invariant water line on the rectified marker.
    The per-row ratio  log(current / dry reference)  only changes smoothly with light,
    haze, smoke or exposure along a 1 m marker, but jumps where the water starts.
    The best break of a two-piece linear fit gives the water line (water is the
    lower piece - it always fills from the road up). Returns (level_m, strength).
    """
    n = cur_r.shape[0]
    cnt = np.maximum(usable.sum(axis=1), 1)[:, None]
    c = (cur_r * usable[..., None]).sum(axis=1) / cnt
    r = (ref_r * usable[..., None]).sum(axis=1) / cnt
    f = np.log((c + 4.0) / (r + 4.0))[::-1]                     # index 0 = road surface
    ok = (usable.sum(axis=1) > 0.3 * usable.shape[1])[::-1]
    x = np.arange(n, dtype=np.float64)
    f, x = f[ok], x[ok]
    m = len(f)
    if m < 2 * min_rows + 2:
        return 0.0, 0.0
    pre = _prefix_linear_sse(f, x)
    suf = _prefix_linear_sse(f[::-1], x[::-1])[::-1]
    ks = np.arange(min_rows, m - min_rows)
    total = pre[ks - 1] + suf[ks]
    best = int(ks[np.argmin(total)])
    w = max(4, min_rows)
    step = np.median(f[max(0, best - w):best], axis=0) - np.median(f[best:best + w], axis=0)
    resid = math.sqrt(float(total.min()) / max(m, 1) / f.shape[1]) + 1e-3
    return float((x[best] - 0.5) / RECT_PPM), float(np.linalg.norm(step) / resid)


class FloodDetector:
    HARD_FAILURES = ("no video signal or lens covered", "video is frozen (camera / network problem)",
                     "camera moved - re-check the ROIs", "image over-exposed (no detail)",
                     "dense smoke / fog (no detail)")
    """
    Water detection inside a work crop that contains both ROIs.

      1. Image quality is assessed (QualityMonitor) and the frame is stabilised.
      2. Noise: the crop is averaged over N frames (N grows with sensor noise).
      3. Water evidence per pixel, by comparison with the DRY reference for the
         current lighting (day / night / IR reference bank):
           * colour / brightness term: CIE-Lab distance after photometric mapping
             (handles exposure changes, dusk, uniform haze); threshold grows with noise
           * texture term: local normalised cross-correlation (NCC) with the
             reference - invariant to brightness and contrast, so it keeps working
             at night, in haze and in smoke; only used where the reference texture
             is visible above the noise
           * the mix between both terms follows the conditions (colour is trusted
             less at night, in IR, in haze/smoke, over-exposure, wrong reference)
           * pixels without information (blown-out glare, black) are ignored
         Fallback without any reference: murky / brown flood-water colour (HSV).
      4. Temporal smoothing of the evidence (stronger when the image is poor).
      5. Moving objects (MOG2): removed from the road-water %; obstructions are
         reported only inside the marker ROI.
      6. FLOOD ROI -> flood coverage %;  MARKER ROI -> rectified marker, scanned
         from the road surface upward, sub-row water line.
      7. Contingency: if the image is unusable or the marker is not visible, the
         reading is marked unreliable and the Kalman filter holds the last level.
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
        self.ref_adapt = args.ref_adapt
        self.step_min_strength = args.step_strength
        self.auto_reference = not args.no_auto_reference
        self.hsv_lo = np.array(args.water_hsv_low, np.uint8)
        self.hsv_hi = np.array(args.water_hsv_high, np.uint8)
        self.min_segment_m = max(0.0, float(args.min_segment_m))
        self.obstruction_row_cover = args.obstruction_row_cover
        self.step_min_rows_m = args.step_min_rows_m
        self.unknown_row_frac = args.unknown_row_frac
        self.ref_adapt_every = max(1, int(args.ref_adapt_every))
        self.capture_frames = args.capture_frames
        self.wet_level = args.wet_level
        self.wet_coverage = args.wet_coverage
        self.dry_coverage = args.dry_coverage
        self.trusted_dry_s = args.trusted_dry_s
        self.k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.k7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.k9 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self.qm = QualityMonitor(args)
        self.refs: dict[str, RefImage] = {}      # reference bank: lighting -> dry reference
        self.active: RefImage | None = None
        self.active_light: str | None = None
        self.mismatch = False                    # active reference is from other lighting
        self.capture = None                      # reference capture in progress
        self.on_reference_captured = None        # callback(lighting, bgr)
        self.last_dry_t = -1e9                   # last reliable "dry" reading (matching reference)
        self.last_wet_t = -1e9
        self.cal: Calibration | None = None
        self.geom = None
        self.mg: MarkerGeometry | None = None
        self.perspective: dict = {}
        self.quality = QualityReport()
        self._adapt_count = 0
        self._reset_state()

    def _reset_state(self):
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        self.prob = None
        self.avg = None
        self.frames = 0
        self._ref_rect = None
        self._cur_used = None
        self._filtered_level = None
        self.last_method = "scan"

    # ---- setup ---------------------------------------------------------
    def set_calibration(self, cal: Calibration, frame_shape, hfov_deg: float = 70.0):
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
        # static scene for camera-shake measurement = everything except the (changing) ROIs
        roi_full = np.zeros((H, W), np.uint8)
        cv2.fillPoly(roi_full, [flood, np.round(mg.quad).astype(np.int32)], 255)
        roi_full = cv2.dilate(roi_full, np.ones((15, 15), np.uint8))
        static_half = (cv2.resize(roi_full, (W // 2, H // 2), interpolation=cv2.INTER_NEAREST) == 0)
        static_small = cv2.resize(roi_full, SMALL, interpolation=cv2.INTER_NEAREST) == 0
        static_small = static_small if static_small.mean() > 0.15 else None
        static_half = static_half.astype(np.float32) if static_half.mean() > 0.15 else None
        self.geom = dict(crop=(x, y, w, h), flood_mask=flood_mask, marker_mask=marker_mask, static_half=static_half,
                         min_overlap_px=max(15, int(0.02 * cv2.countNonZero(marker_mask))), M=M, valid=valid,
                         static_small=static_small,
                         min_obj_px=max(30, int(self.min_obj_frac * W * H)))
        self.mg = mg
        self.perspective = mg.perspective_info(W, H, hfov_deg, cal.marker_width_m)
        acc = self.perspective["setup_accuracy_cm_per_px"].get("1.0m")
        if acc is not None and acc > 3.0:
            LOG.warning("Marker setup is sensitive: a 1-pixel corner error changes the 1.0 m reading by "
                        "%.1f cm. Make the marker ROI taller (higher reference point) or use a higher "
                        "camera resolution / zoom.", acc)
        for ref in self.refs.values():
            ref.crop = None
        if self.active is not None:
            self.active.prepare(self.geom["crop"])

    # ---- reference bank ------------------------------------------------
    def add_reference(self, bgr, lighting: str | None = None) -> str:
        """Add a dry reference; its lighting (day / night / ir) is detected from the image."""
        if self.cal is not None and self.cal.frame_size:
            W, H = self.cal.frame_size
            if bgr.shape[:2] != (H, W):
                bgr = cv2.resize(bgr, (W, H))
        if lighting is None:
            lighting = classify_lighting(cv2.resize(bgr, SMALL, interpolation=cv2.INTER_AREA),
                                         self.qm.night_brightness)
        self.refs[lighting] = RefImage(bgr, lighting)
        if self.active_light is None or lighting == self.active_light or self.active is None:
            self._activate(lighting if self.active_light is None else self.active_light)
        return lighting

    def _activate(self, lighting: str):
        self.active_light = lighting
        if lighting in self.refs:
            self.active, self.mismatch = self.refs[lighting], False
        elif self.refs:
            # no reference for this lighting yet: use the closest one. A colour reference still
            # works at night (brightness mapping); only colour <-> IR is a real mismatch.
            order = {"day": ["day", "night", "ir"], "night": ["night", "day", "ir"], "ir": ["ir", "night", "day"]}
            best = next(k for k in order[lighting] if k in self.refs)
            self.active = self.refs[best]
            self.mismatch = (lighting == "ir") != (best == "ir")
        else:
            self.active, self.mismatch = None, False
        if self.active is not None and self.geom is not None:
            self.active.prepare(self.geom["crop"])
        self.prob = None
        self.avg = None

    def start_capture(self, lighting: str, frames: int | None = None, reason: str = "manual",
                      replace_bank: bool = False):
        """Average `frames` frames into a new dry reference. replace_bank=True drops all other
        references (used when the scene itself changed, e.g. the physical marker was removed)."""
        frames = frames or self.capture_frames
        self.capture = dict(light=lighting, acc=None, n=0, N=frames, reason=reason, replace=replace_bank)
        LOG.info("Capturing dry reference for %s lighting (%s, %d frames)...", lighting, reason, frames)

    def _capture_step(self, frame):
        c = self.capture
        f = frame.astype(np.float32)
        c["acc"] = f if c["acc"] is None else c["acc"] + f
        c["n"] += 1
        if c["n"] >= c["N"]:
            bgr = np.clip(c["acc"] / c["n"], 0, 255).astype(np.uint8)       # averaged = low noise
            if c.get("replace"):
                self.refs = {}
            self.refs[c["light"]] = RefImage(bgr, c["light"])
            self.capture = None
            if c["light"] == self.active_light:
                self._activate(c["light"])
            LOG.info("Dry reference for %s lighting ready.", c["light"])
            if self.on_reference_captured:
                self.on_reference_captured(c["light"], bgr, bool(c.get("replace")))

    def trusted_dry(self, t: float) -> bool:
        """The road was confirmed dry recently (with a matching reference) and has not been wet since."""
        return (t - self.last_dry_t) < self.trusted_dry_s and self.last_wet_t < self.last_dry_t

    def note_result(self, t, meas, level, anomaly):
        """Called after the Kalman filter - remembers when the road was confirmed dry / wet."""
        if meas is None or not meas.reliable:
            return
        self._filtered_level = level
        if level >= self.wet_level or meas.flood_coverage >= self.wet_coverage:
            self.last_wet_t = t
        elif not anomaly and not self.mismatch and meas.flood_coverage < self.dry_coverage:
            self.last_dry_t = t
            # slowly follow daylight changes while dry, empty and well exposed
            if self.ref_adapt > 0 and self.active is not None and not meas.any_motion \
                    and self.quality.mode == "GOOD" and not self.quality.conditions:
                self._adapt_count += 1
                if self._adapt_count % self.ref_adapt_every == 0:
                    a = min(0.5, self.ref_adapt * self.ref_adapt_every)
                    # float accumulation: an 8-bit blend rounds small daylight changes away
                    acc = cv2.addWeighted(self.active.bgr_f, 1 - a, self._last_frame.astype(np.float32), a, 0)
                    self.refs[self.active.lighting] = RefImage(acc, self.active.lighting)
                    self.active = self.refs[self.active.lighting]
                    if self.geom is not None:
                        self.active.prepare(self.geom["crop"])

    @property
    def ready(self) -> bool:
        return self.geom is not None

    @property
    def uses_reference(self) -> bool:
        return self.method != "color" and self.active is not None

    # ---- per-frame -----------------------------------------------------
    def process(self, frame, t):
        """Assess quality, stabilise, adapt and measure. Returns (frame_used, Measurement|None, QualityReport)."""
        q = self.qm.assess(frame, t, self)
        if self.active_light != q.lighting:
            had = self.active_light
            self._activate(q.lighting)
            if had is not None:
                LOG.info("Using %s reference for %s lighting%s", self.active.lighting if self.active else "no",
                         q.lighting, " (texture-based until a matching reference is captured)" if self.mismatch else "")
        # auto-capture a matching reference when the road is known to be dry
        if self.auto_reference and self.active_light not in self.refs and self.capture is None                 and self.trusted_dry(t) \
                and not q.reason:
            self.start_capture(q.lighting, reason="automatic, road confirmed dry")
        self.quality = q
        if q.shift != (0.0, 0.0):
            M = np.float32([[1, 0, -q.shift[0]], [0, 1, -q.shift[1]]])
            frame = cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        self._last_frame = frame
        if self.capture is not None and not q.reason:
            self._capture_step(frame)
        if self.geom is None:
            return frame, None, q
        if q.reason in self.HARD_FAILURES[:3]:
            return frame, Measurement(0.0, 0.0, 0.0, 0.0, False, [], reliable=False,
                                      unreliable_reason=q.reason, trust=0.0), q
        meas = self.measure(frame, q)
        meas.method = self.last_method
        meas.resolution_cm_per_px = self.mg.cm_per_px_at(meas.raw_level_m)
        # A clear water-line step is self-validating (it averages whole marker rows), so a
        # dark / noisy picture is still usable. Hard failures are never used.
        if meas.method == "step":
            meas.trust = max(q.score, 0.6)
        else:
            meas.trust = q.score
        if meas.reliable and (q.reason in self.HARD_FAILURES or (q.mode == "POOR" and meas.method != "step")):
            meas.reliable, meas.unreliable_reason = False, q.reason
        return frame, meas, q

    def _evidence(self, crop_f, q: QualityReport):
        """Per-pixel water evidence (0..1) and a mask of pixels without usable information."""
        cu8 = np.clip(crop_f, 0, 255).astype(np.uint8)
        h, w = cu8.shape[:2]
        if not self.uses_reference:
            hsv = cv2.cvtColor(cv2.GaussianBlur(cu8, (3, 3), 0), cv2.COLOR_BGR2HSV)
            e = cv2.inRange(hsv, self.hsv_lo, self.hsv_hi).astype(np.float32) / 255.0
            return e, np.zeros((h, w), bool)
        ref = self.active
        ref.prepare(self.geom["crop"])
        x, y = self.geom["crop"][:2]
        self._cur_used = crop_f
        raw_gray = cv2.cvtColor(crop_f, cv2.COLOR_BGR2GRAY)
        ne = max(q.noise_eff, 0.7)
        ne_map = ne
        if q.t_small is not None:
            # haze / smoke: remove it with the reference-aided transmission map
            Wf, Hf = self.cal.frame_size
            tfull = cv2.resize(q.t_small, (Wf, Hf), interpolation=cv2.INTER_LINEAR)[y:y + h, x:x + w]
            A = np.asarray(q.airlight, np.float32)
            crop_f = np.clip((crop_f - A) / tfull[..., None] + A, 0, 255).astype(np.float32)
            cu8 = crop_f.astype(np.uint8)
            ne_map = ne / tfull                                   # dehazing amplifies noise
        self._cur_used = crop_f
        g = max(q.gain, 0.1)
        gray = cv2.cvtColor(crop_f, cv2.COLOR_BGR2GRAY)

        # colour / brightness term, measured in the reference's brightness units
        d_gray = (gray - (ref.gray * q.gain + q.offset)) / g
        if q.lighting == "ir" or ref.lighting == "ir":
            chroma2 = 0.0                                         # no colour in IR
        else:
            lab = cv2.cvtColor(cv2.GaussianBlur(cu8, (3, 3), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
            da = lab[..., 1] - ref.lab[..., 1]
            db = lab[..., 2] - ref.lab[..., 2]
            chroma2 = (da * da + db * db) * (1.0 if q.lighting == "day" else 0.5)
        dist = np.sqrt(0.25 * d_gray * d_gray + chroma2)
        thr = np.sqrt(self.diff_thresh ** 2 + (3.0 * ne_map / g) ** 2)
        ec = np.clip(0.5 + (dist - thr) / thr, 0, 1)             # 0.5 at the threshold, 1 at 1.5x

        # texture term: local normalised cross-correlation with the reference
        win = 7 if ne < 3 else (9 if ne < 6 else 11)
        mu_r, sd_r = ref.local_stats(win)
        mu_c = cv2.boxFilter(gray, -1, (win, win))
        var_c = cv2.boxFilter(gray * gray, -1, (win, win)) - mu_c * mu_c
        cov = cv2.boxFilter(gray * ref.gray, -1, (win, win)) - mu_c * mu_r
        ncc = cov / (np.sqrt(np.maximum(var_c, 0)) * sd_r + 1e-3)
        sig = sd_r * g                                           # texture expected now
        ncc_exp = sig / np.sqrt(sig * sig + ne_map * ne_map)     # expected NCC if unchanged
        s_ok = sig > 2.0 * ne_map
        change = 1.0 - ncc / np.maximum(ncc_exp, 0.2)            # 0 = same texture, 1 = new surface
        es = np.clip((change - 0.25) / 0.5, 0, 1)                # 0.5 when half the texture is gone

        cw = q.color_weight
        e = np.where(s_ok, cw * ec + (1 - cw) * es, ec * (0.8 if self.mismatch else 1.0)).astype(np.float32)

        # pixels without information: blown-out highlights, or black below the noise floor
        invalid = (raw_gray >= 250) | ref.sat | ((raw_gray < 3.0 * ne + 3) & ~s_ok)
        e[invalid] = 0.0
        return e, invalid

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

    def measure(self, frame_bgr, q: QualityReport | None = None) -> Measurement | None:
        """Road water coverage + water level on the marker for one (stabilised) frame."""
        g = self.geom
        if g is None:
            return None
        q = q or self.quality
        x, y, w, h = g["crop"]
        crop = frame_bgr[y:y + h, x:x + w]
        invalid = self._update_evidence(crop, q)
        obj, obj_marker, boxes = self._objects(crop)
        water_u8, coverage, flood_visible = self._road_coverage(invalid, obj)
        prof = self._marker_profiles(invalid, obj_marker)
        scan_level, last = self._scan_level(prof)
        level, last, use_step, step_level = self._step_level(prof, scan_level, last)
        self.last_method = "step" if use_step else "scan"
        motion_ratio, obstructed = self._obstruction(prof, level)
        if not obstructed:
            boxes, obj_marker = [], np.zeros_like(obj_marker)
        confidence = self._confidence(prof, last, level, coverage, flood_visible, q)
        flood_detected = coverage >= self.flood_min_cov or level >= 0.03
        reliable, why = self._marker_visible(prof, level)
        rect_view = (prof["r_prob"] * 255).astype(np.uint8)
        return Measurement(level, motion_ratio, confidence, coverage, flood_detected, boxes,
                           any_motion=bool(obj.any()), reliable=reliable, unreliable_reason=why,
                           scan_level_m=scan_level, step_level_m=step_level,
                           water_mask=water_u8, object_mask=obj_marker, rect_view=rect_view)

    # ---- measure() steps ------------------------------------------------
    def _update_evidence(self, crop, q: QualityReport):
        """Temporal averaging (sensor noise) -> per-pixel evidence -> smoothed water probability."""
        cf = crop.astype(np.float32)
        if self.avg is None or self.avg.shape != cf.shape:
            self.avg = cf
        else:
            a = 1.0 / q.avg_frames
            self.avg = cv2.addWeighted(self.avg, 1 - a, cf, a, 0)
        evidence, invalid = self._evidence(self.avg, q)
        smooth = float(np.clip(self.smooth + 0.35 * (1.0 - q.score), 0.0, 0.9))
        self.prob = evidence if self.prob is None or self.prob.shape != evidence.shape else cv2.addWeighted(
            self.prob, smooth, evidence, 1.0 - smooth, 0)
        return invalid

    def _road_coverage(self, invalid, obj):
        """Share of the flood ROI covered by water (moving objects and unusable pixels excluded)."""
        g = self.geom
        water_u8 = (self.prob >= 0.5).astype(np.uint8) * 255      # area measure: morphology is safe here
        water_u8 = cv2.morphologyEx(water_u8, cv2.MORPH_OPEN, self.k3)
        water_u8 = cv2.morphologyEx(water_u8, cv2.MORPH_CLOSE, self.k7)
        water = (water_u8 > 0) & ~invalid
        flood = g["flood_mask"] > 0
        free = flood & ~(obj > 0) & ~invalid
        n_free = int(free.sum())
        coverage = float((water & free).sum() / n_free) if n_free > 50 else 0.0
        flood_visible = n_free / max(1, int(flood.sum()))
        return water_u8, coverage, flood_visible

    def _marker_profiles(self, invalid, obj_marker) -> dict:
        """Row profiles of the rectified (perspective-free) marker, index 0 = road surface."""
        g, mg = self.geom, self.mg
        size = (mg.w, mg.rows)
        r_prob = cv2.warpPerspective(self.prob, g["M"], size, flags=cv2.INTER_LINEAR, borderValue=0)
        r_obj = cv2.warpPerspective(obj_marker, g["M"], size, flags=cv2.INTER_NEAREST, borderValue=0) > 0
        r_inv = cv2.warpPerspective(invalid.astype(np.uint8), g["M"], size, flags=cv2.INTER_NEAREST,
                                    borderValue=0) > 0
        valid = g["valid"]
        usable = valid & ~r_inv
        vcount = np.maximum(usable.sum(axis=1), 1)
        w_up = (((r_prob >= 0.5) & usable).sum(axis=1) / vcount)[::-1]            # water share per row
        o_up = ((r_obj & valid).sum(axis=1) / np.maximum(valid.sum(axis=1), 1))[::-1]   # object share
        unknown_up = (usable.sum(axis=1) < self.unknown_row_frac * np.maximum(valid.sum(axis=1), 1))[::-1]
        win = max(3, int(RECT_PPM * 0.02) | 1)                                    # 2 cm smoothing
        pad = win // 2
        w_s = np.convolve(np.pad(w_up, pad, mode="edge"), np.ones(win) / win, mode="valid")
        return dict(r_prob=r_prob, usable=usable, w_up=w_up, o_up=o_up, unknown_up=unknown_up, w_s=w_s)

    def _scan_level(self, prof):
        """Pixel scan from the road surface upward; tolerates short gaps; sub-row water line.
        With --min-segment-m, a thin water band above a gap (e.g. a gauge tick mark that
        differs from the reference) cannot pull the level up."""
        mg = self.mg
        w_s, unknown_up = prof["w_s"], prof["unknown_up"]
        gap = max(2, int(self.gap_m * RECT_PPM))
        last = -1
        segs = []                                   # [first_row, last_row] of each water band
        gap_seen = True
        for k in range(mg.rows):
            if unknown_up[k]:
                continue                            # glare / black: skip row
            if w_s[k] >= self.row_frac:
                if gap_seen:
                    segs.append([k, k])
                    gap_seen = False
                else:
                    segs[-1][1] = k
                last = k
            else:
                gap_seen = True
                if k - last > gap:
                    break
        min_rows = int(round(self.min_segment_m * RECT_PPM))
        if min_rows > 0 and len(segs) > 1:
            while len(segs) > 1 and segs[-1][1] - segs[-1][0] + 1 < min_rows:
                segs.pop()
            last = segs[-1][1]
        if last < 0:
            return 0.0, last
        frac = 0.5
        if last + 1 < mg.rows and not unknown_up[last + 1]:          # sub-row water line
            a, b = float(w_s[last]), float(w_s[last + 1])
            frac = float(np.clip((a - self.row_frac) / (a - b), 0, 1)) if a > b else 0.5
        return min((last + 0.5 + frac) / RECT_PPM, mg.max_m), last

    def _step_level(self, prof, scan_level, last):
        """Second method: illumination-invariant step along the marker (night, IR, dusk, smoke,
        haze ...). Used whenever the step is clear; otherwise the pixel scan."""
        if not self.uses_reference:
            return scan_level, last, False, None
        g, mg, ref = self.geom, self.mg, self.active
        if self._ref_rect is None or self._ref_rect[0] is not ref:
            x0, y0, w0, h0 = g["crop"]
            rc = ref.bgr[y0:y0 + h0, x0:x0 + w0].astype(np.float32)
            self._ref_rect = (ref, cv2.warpPerspective(rc, g["M"], (mg.w, mg.rows), flags=cv2.INTER_LINEAR))
        cur_r = cv2.warpPerspective(self._cur_used, g["M"], (mg.w, mg.rows), flags=cv2.INTER_LINEAR)
        step_level, step_strength = waterline_step(cur_r, self._ref_rect[1], prof["usable"],
                                                   int(self.step_min_rows_m * RECT_PPM))
        # tie-breaker: when the scan suddenly finds NO water although the filtered level
        # says there is water (e.g. textured wall behind a virtual marker), a moderately
        # clear step that agrees with the filtered level is used instead
        prev = self._filtered_level
        use_step = step_strength >= self.step_min_strength or (
            prev is not None and prev > 0.05 and scan_level < 0.02
            and step_strength >= 0.6 * self.step_min_strength and abs(step_level - prev) < 0.06)
        if use_step:
            level = min(step_level, mg.max_m)
            return level, (int(level * RECT_PPM) if level > 0 else -1), True, step_level
        return scan_level, last, False, step_level

    def _obstruction(self, prof, level):
        """Obstruction = a moving object covering a TALL part of the marker (car, person).
        The rising / rippling water line also "moves", but only as a thin horizontal band."""
        o_up = prof["o_up"]
        covered = np.concatenate([[0], (o_up > self.obstruction_row_cover).astype(np.int8), [0]])
        edges = np.flatnonzero(np.diff(covered))
        longest_m = float((edges[1::2] - edges[0::2]).max() / RECT_PPM) if edges.size else 0.0
        obstructed = longest_m >= self.min_obstruction_m
        zone = max(1, int(max(level, 0.5) * RECT_PPM))
        motion_ratio = float(o_up[:zone].mean()) if obstructed else 0.0
        return motion_ratio, obstructed

    def _confidence(self, prof, last, level, coverage, flood_visible, q):
        w_up = prof["w_up"]
        if last >= 0:
            confidence = float(w_up[:last + 1].mean())
        else:
            confidence = float(1.0 - w_up[: int(0.05 * RECT_PPM)].mean())
        if level >= 0.05 and coverage < 0.02 and flood_visible > 0.5:   # marker says water, road does not
            confidence *= 0.5
        return confidence * max(q.score, 0.2) ** 0.5

    def _marker_visible(self, prof, level):
        """Contingency: marker hidden by glare / darkness around the water line."""
        unknown_up = prof["unknown_up"]
        zone_hi = min(self.mg.rows, int((level + 0.15) * RECT_PPM))
        if unknown_up[:max(zone_hi, int(0.3 * RECT_PPM))].mean() > 0.5:
            return False, "marker not visible (glare or darkness)"
        return True, None
