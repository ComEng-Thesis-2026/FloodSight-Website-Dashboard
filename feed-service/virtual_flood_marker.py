"""
Virtual flood marker that measures from the FLOOD ROI, plus the road condition (DRY / WET / FLOODED).

Why this exists
---------------
floodmon's virtual marker reads the water line INSIDE the marker ROI: it compares what is behind
the (removed) marker with a dry reference taken without the physical marker. The demo clips cut
from a dry daytime scene, where the physical reference marker is composited in, to different
footage (a wet road at night or in rain, then the flood). After the cut nothing behind the marker
matches any reference, so that comparison reads the whole marker as water (about 1.0 m on a wet
road) or reads whatever happens to be behind it.

So once the marker is virtual, the level is measured from the flood ROI and the background inside
the marker ROI is not used:

  1. Flood water = pixels of the flood ROI that
       * no longer match the dry reference (floodmon's own per-pixel water evidence), and
       * have flood-water colour (CIE-Lab, the site's water is muddy brown or dark green):
           muddy brown       hue 15-70 deg,  chroma >= 3.5   (pale brown water included)
           olive / dk green  hue 70-170 deg, chroma >= 3.5 by day, >= 7 at night (at night a
                                                              weak yellow cast is lamp light on a
                                                              wet road, not water)
       * are not glare, black, or a moving vehicle / person,
     and they form a continuous sheet (connected regions of at least 2 % of the ROI).
     The share of the ROI under water is the upper quartile of the last 15 s: traffic, ripples and
     glare make the per-frame share jump (floodmon's Kalman filter expects ~1.5 cm of noise), and
     a vehicle or glare can only hide water, never add it.
  2. Road condition, with hysteresis and a short confirmation:
       FLOODED  the water sheet covers >= 35 % of the flood ROI (until it drops below 15 %)
       WET      not flooded, but the road no longer matches its dry reference
                (floodmon's wet / dry coverage thresholds, 5 % / 3 %)
       DRY      otherwise
  3. Depth = water share x the site's FULL-COVER DEPTH, and 0 m unless FLOODED.
     The full-cover depth (feeds.json `flood_full_depth_m`) is the water depth at which the whole
     flood ROI is under water: the height of the highest ground inside the ROI (curb, raised shoulder,
     road crown) above the road. A road is completely covered long before the water is deep, so the
     extent of the water says how far it has climbed that relief, not how far up a 1 m marker it is.
     Deeper water than that cannot be read from the extent alone: the depth stops at the full-cover
     depth. The remembered marker (marker_memory.py) shows the depth on its own scale and colours.

floodmon itself is not changed: like the stage timers in worker.py, this wraps the feed's own
detector instance (Monitor calls self.det.process / self.det.note_result and never replaces it).
In physical-marker mode the marker reading stays floodmon's; only the road condition is added.
"""

from __future__ import annotations

import collections
import dataclasses
import logging

import cv2
import numpy as np

LOG = logging.getLogger("feed-service")

ROAD_CONDITIONS = ("DRY", "WET", "FLOODED")

# flood-water colour (Lab: L* 0-100, a*/b* signed, hue in degrees)
BROWN_HUE = (15.0, 70.0)
BROWN_MIN_CHROMA = 3.5
GREEN_HUE = (70.0, 170.0)
GREEN_MIN_CHROMA_DAY = 3.5
GREEN_MIN_CHROMA_NIGHT = 7.0    # lamp light gives a wet road a weak yellow cast
FULL_DEPTH_DEFAULT_M = 0.20     # depth at which the flood ROI is all water (curb / shoulder height)
MAX_CHROMA = 60.0
MAX_L = 80.0
MIN_L = 8.0                     # darker = no information (black bars, night shadows)
GLARE_L = 85.0                  # near a highlight this bright, a colourless pixel is glare
GLARE_MAX_CHROMA = 6.0
MIN_SHEET_FRAC = 0.02           # a water region must cover at least this share of the ROI
MIN_KNOWN_FRAC = 0.25           # judged area needed for a reliable reading
SCENE_CHANGE_OBJECTS = 0.5      # "moving objects" over half the ROI = the whole scene changed

FLOOD_ON = 0.35                 # water sheet share that makes the road FLOODED ...
FLOOD_OFF = 0.15                # ... and the share it must drop below to leave FLOODED
CONFIRM_S = 1.0                 # a new road condition must hold this long
SMOOTH_S = 15.0                 # rolling window of the water share: traffic, ripples and glare make
SMOOTH_PCT = 75                 # the per-frame share jump by 0.1 - 0.3 for many seconds, far above the
                                # Kalman filter's 1.5 cm measurement noise. Vehicles and glare only
                                # ever HIDE water, so the upper quartile of the window is used
MIN_TRUST = 0.6                 # like floodmon's step method, a whole-ROI share is self-averaging
SCENE_CUT_DIFF = 20.0           # mean grey-level change (160x90) that means the clip cut to another scene


class VirtualFloodMarker:
    def __init__(self, mon, cal, full_depth_m: float | None = None):
        self.mon, self.cal, self.det = mon, cal, mon.det
        self.full_depth_m = float(full_depth_m or FULL_DEPTH_DEFAULT_M)
        args = mon.args
        self.wet_on = float(args.wet_coverage)
        self.wet_off = float(args.dry_coverage)
        self.condition: str | None = None       # None = undecided (start, new loop): no confirmation wait
        self._cand: str | None = None
        self._cand_since = 0.0
        self._regime_t = 0.0                    # when the evidence for the current condition began
        self.flood_pct = 0.0                    # water sheet share of the flood ROI (%), before gating
        self.change_pct = 0.0                   # share of the flood ROI that no longer matches the dry reference (%)
        self.active = False                     # this frame's level came from the flood ROI
        self._hist: collections.deque = collections.deque()     # (t, water share) of the last SMOOTH_S
        self._obj_all = None
        self._k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._k9 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self._install()

    # ---- wiring -----------------------------------------------------------
    def _install(self):
        det = self.det
        objects, process, note_result = det._objects, det.process, det.note_result

        def objects_kept(crop):
            out = objects(crop)
            self._obj_all = out[0]                  # every moving object (work-crop coordinates)
            return out

        def process_flood_roi(frame, t):
            self._guard_capture(frame)
            frame_used, meas, q = process(frame, t)
            if meas is not None:
                self._apply(meas, q, t)
            return frame_used, meas, q

        def note_result_on_change(t, meas, level, anomaly):
            # floodmon's dry / wet bookkeeping (automatic references, slow daylight adaptation of the
            # reference) must keep seeing how much of the road CHANGED: a wet road without flood
            # water is not dry, and must never be blended into the dry reference
            if meas is not None and hasattr(meas, "change_coverage"):
                meas = dataclasses.replace(meas, flood_coverage=meas.change_coverage)
            return note_result(t, meas, level, anomaly)

        det._objects, det.process, det.note_result = objects_kept, process_flood_roi, note_result_on_change

    def _guard_capture(self, frame):
        """A dry reference averages several frames. In the demo clips the dry scene is short (3.7 s
        at Masterson's), so a capture can run into the cut to the next scene and blend both into
        one reference. Such a capture is cancelled: pause on the dry scene and capture again."""
        cap = self.det.capture
        if cap is None:
            return
        small = cv2.cvtColor(cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA),
                             cv2.COLOR_BGR2GRAY).astype(np.float32)
        first = cap.setdefault("_scene", small)
        if float(np.abs(small - first).mean()) > SCENE_CUT_DIFF:
            self.det.capture = None
            if getattr(self.mon.vmark, "state", None) == "capturing":     # virtual marker step 2/2
                self.mon.vmark.state = "awaiting_removal"
            LOG.warning("[%s] dry reference capture CANCELLED: the video cut to another scene during the "
                        "capture - pause on the dry scene and capture again", self.cal.camera_id)

    def reset(self):
        """New loop of the clip (or setup change): decide the road condition afresh."""
        self.condition, self._cand = None, None
        self._hist.clear()

    def check_meas(self, meas):
        """The measurement as floodmon's reference-capture / conversion checks expect it:
        flood_coverage = share of the road that changed from the dry reference."""
        if meas is not None and hasattr(meas, "change_coverage"):
            return dataclasses.replace(meas, flood_coverage=meas.change_coverage)
        return meas

    # ---- per frame ----------------------------------------------------------
    def _apply(self, meas, q, t):
        det, cal = self.det, self.cal
        meas.change_coverage = float(meas.flood_coverage)
        self.change_pct = round(meas.flood_coverage * 100, 1)
        self.active = False
        g = det.geom
        if g is None or det.avg is None or meas.water_mask is None:     # hard failure: no image to judge
            return
        water, cov_now, known_frac = self._flood_water(g, q)
        self._hist.append((t, cov_now))
        while self._hist and t - self._hist[0][0] > SMOOTH_S:
            self._hist.popleft()
        cov = self._smoothed()
        before = self.condition
        condition = self._update_condition(cov, meas.change_coverage, t)
        if before is not None and condition != before and "FLOODED" in (before, condition):
            # a flood started or ended: the samples from before belong to the other state
            while len(self._hist) > 1 and self._hist[0][0] < self._regime_t:
                self._hist.popleft()
            cov = self._smoothed()
        self.flood_pct = round(cov * 100, 1)
        if cal.marker_mode != "virtual" or det.mg is None:
            return                                   # physical marker in view: floodmon reads it
        self.active = True
        flooded = condition == "FLOODED"
        # depth = how far the water has climbed the ROI's relief (see the module docstring)
        level = float(min(cov, 1.0) * self.full_depth_m) if flooded else 0.0
        meas.raw_level_m = level
        meas.scan_level_m = meas.step_level_m = None
        meas.method = "flood_roi"
        meas.flood_coverage = cov if flooded else 0.0
        meas.flood_detected = flooded
        meas.water_mask = water if flooded else np.zeros_like(water)
        # the marker ROI is not looked at, so nothing can obstruct it
        meas.objects, meas.motion_ratio = [], 0.0
        meas.object_mask = np.zeros_like(water)
        meas.resolution_cm_per_px = det.mg.cm_per_px_at(level)
        meas.trust = max(q.score, MIN_TRUST)
        meas.confidence = float(known_frac * max(q.score, 0.2) ** 0.5)
        # floodmon's "too dark / too noisy" verdict compares the picture with the dry reference's
        # texture, which says nothing once the clip shows other footage; the flood ROI's own
        # visibility (not black, not glare) is checked instead
        if q.reason in det.HARD_FAILURES:
            meas.reliable, meas.unreliable_reason = False, q.reason
        elif known_frac < MIN_KNOWN_FRAC:
            meas.reliable, meas.unreliable_reason = False, "flood ROI not visible (glare or darkness)"
        else:
            meas.reliable, meas.unreliable_reason = True, None

    def _flood_water(self, g, q):
        """Water sheet mask (work-crop coordinates, uint8 0/255), its share of the judged flood ROI,
        and the judged share of the ROI."""
        det = self.det
        bgr = np.clip(det.avg, 0, 255).astype(np.uint8)            # temporally averaged crop (low noise)
        lab = cv2.cvtColor(cv2.GaussianBlur(bgr, (5, 5), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
        L = lab[..., 0] * (100.0 / 255.0)
        a, b = lab[..., 1] - 128.0, lab[..., 2] - 128.0
        C = np.hypot(a, b)
        H = np.degrees(np.arctan2(b, a)) % 360.0
        roi = g["flood_mask"] > 0
        glare = (cv2.dilate(L, self._k9) >= GLARE_L) & (C < GLARE_MAX_CHROMA)
        known = roi & (L >= MIN_L) & ~glare
        obj = self._obj_all
        if obj is not None and obj.shape == roi.shape:
            moving = (obj > 0) & roi
            if moving.sum() < SCENE_CHANGE_OBJECTS * max(1, int(roi.sum())):   # vehicles, people
                known &= ~moving
        green_min = GREEN_MIN_CHROMA_DAY if q.lighting == "day" else GREEN_MIN_CHROMA_NIGHT
        colour = (((H >= BROWN_HUE[0]) & (H < BROWN_HUE[1]) & (C >= BROWN_MIN_CHROMA))
                  | ((H >= GREEN_HUE[0]) & (H <= GREEN_HUE[1]) & (C >= green_min)))
        colour &= (C <= MAX_CHROMA) & (L <= MAX_L)
        changed = det.prob >= 0.5 if (det.uses_reference and det.prob is not None) else True
        w = ((colour & changed & known).astype(np.uint8)) * 255
        w = cv2.morphologyEx(cv2.morphologyEx(w, cv2.MORPH_OPEN, self._k5), cv2.MORPH_CLOSE, self._k9)
        n_known = int(known.sum())
        n, labels, stats, _ = cv2.connectedComponentsWithStats(w, connectivity=8)
        sheets = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= MIN_SHEET_FRAC * max(n_known, 1)]
        water = np.isin(labels, sheets) & known
        cov = float(water.sum() / n_known) if n_known > 0 else 0.0
        known_frac = n_known / max(1, int(roi.sum()))
        return water.astype(np.uint8) * 255, cov, known_frac

    def _smoothed(self) -> float:
        return float(np.percentile([c for _, c in self._hist], SMOOTH_PCT))

    def _update_condition(self, cov, change, t) -> str:
        cur = self.condition
        if cov >= (FLOOD_OFF if cur == "FLOODED" else FLOOD_ON):
            cand = "FLOODED"
        else:
            cand = "WET" if change >= (self.wet_off if cur in ("WET", "FLOODED") else self.wet_on) else "DRY"
        if cur is None:
            self.condition, self._cand = cand, None
        elif cand == cur:
            self._cand = None
        elif cand != self._cand:
            self._cand, self._cand_since = cand, t
        elif t - self._cand_since >= CONFIRM_S:
            LOG.info("[%s] road condition %s -> %s (flood water %.0f%%, road changed %.0f%%)",
                     self.cal.camera_id, cur, cand, cov * 100, change * 100)
            self.condition, self._cand, self._regime_t = cand, None, self._cand_since
            if "FLOODED" in (cur, cand) and self.cal.marker_mode == "virtual":
                # outside FLOODED the level is 0 by classification, not a measured depth, so the
                # step into / out of a flood is not a spike: the Kalman filter starts afresh on it
                # (as on a new loop of the clip) instead of holding the old level for persist_s
                self.mon.kalman.reset()
        return self.condition

    # ---- output -------------------------------------------------------------
    def payload_fields(self) -> dict:
        return {
            "road_condition": self.condition,
            "level_source": "flood_roi" if self.active else "marker",
            "flood_water_pct": self.flood_pct,
            "road_change_pct": self.change_pct,
            "full_cover_depth_m": self.full_depth_m,
        }


def install(mon, cal) -> VirtualFloodMarker:
    return VirtualFloodMarker(mon, cal)
