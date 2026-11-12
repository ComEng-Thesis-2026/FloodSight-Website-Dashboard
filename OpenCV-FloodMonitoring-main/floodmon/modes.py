"""Marker modes: physical marker, and conversion to a locked virtual marker."""

from __future__ import annotations

import logging

from .calibration import Calibration

LOG = logging.getLogger("FloodMonitoring")


class VirtualMarker:
    """
    Turns the physical flood marker into a locked VIRTUAL marker ('x' key).

      step 1  physical marker in place -> x : the marker geometry (homography,
              scale, perspective compensation) is LOCKED and saved.
      step 2  physical marker removed, road dry and still -> x : a new dry
              reference WITHOUT the physical marker is captured (the old ones are
              kept as a record) and the setup is saved. Measuring resumes.

    While the conversion is in progress (between the two presses) readings are
    held, so removing the marker is never mistaken for water. Shift+X unlocks.
    """

    def __init__(self, cal: Calibration, det, kalman, save):
        self.cal, self.det, self.kalman, self.save = cal, det, kalman, save
        self.state = "awaiting_removal" if cal.marker_locked and not cal.virtual_marker else None
        self.last_motion_t = -1e9

    @property
    def blocking(self) -> bool:
        return self.state is not None

    def status_text(self) -> str | None:
        if self.state == "awaiting_removal":
            return "MARKER LOCKED - remove the physical marker, keep the road dry & empty, then press x"
        if self.state == "capturing":
            return "Capturing the scene without the physical marker..."
        return None

    def observe(self, t, meas):
        if meas is not None and meas.any_motion:
            self.last_motion_t = t

    def press(self, t, meas, q) -> str:
        cal = self.cal
        if not cal.ready or self.det.mg is None:
            return "Fit the marker ROI to the physical marker first."
        if not cal.marker_locked:
            cal.marker_locked, cal.virtual_marker = True, False
            self.state = "awaiting_removal"
            self.kalman.reset()
            self.save()
            return ("STEP 1/2 done - virtual marker geometry LOCKED. Now remove the physical marker, "
                    "keep the road dry and empty, then press x again.")
        if self.state == "awaiting_removal":
            if q is not None and q.reason:
                return f"Cannot capture yet: {q.reason}."
            if meas is not None and meas.flood_coverage >= 0.05:
                return "Cannot capture yet: the road is not dry (wait until the water is gone)."
            if t - self.last_motion_t < 2.0:
                return "Cannot capture yet: something is moving in view - wait until the scene is still."
            self.state = "capturing"
            self.det.start_capture(q.lighting if q is not None else "day", frames=20,
                                   reason="virtual marker - physical marker removed", replace_bank=True)
            return "STEP 2/2 - capturing the scene without the physical marker (keep still)..."
        if self.state == "capturing":
            return "Capture in progress..."
        return "The virtual marker is already active and locked (Shift+X unlocks it)."

    def finish(self, light: str, name: str):
        """Called when the reference without the physical marker is ready."""
        cal = self.cal
        old = dict(cal.reference_images or {})
        if cal.reference_image:
            old.setdefault("legacy", cal.reference_image)
        if old and not cal.physical_marker_references:
            cal.physical_marker_references = old
        cal.reference_images, cal.reference_image = {light: name}, None
        cal.virtual_marker = True
        self.state = None
        self.kalman.reset()
        self.save()
        LOG.info("VIRTUAL MARKER ACTIVE - locked geometry, reference without the physical marker (%s). "
                 "References for other lighting will be added automatically when the road is dry.", name)

    def unlock(self) -> str:
        if not self.cal.marker_locked:
            return "The marker is not locked."
        self.cal.marker_locked = False
        self.state = None
        self.save()
        return ("Marker UNLOCKED - it can be adjusted again"
                + (" (it stays virtual: no physical marker needed)." if self.cal.virtual_marker else "."))


class ReferenceGuard:
    """
    'b' captures a dry reference. A reference taken while the road is wet or something
    is moving would make every later reading wrong, so the scene is checked first.
    If it does not look dry and still, nothing is captured and the operator is told
    why; pressing 'b' again within `confirm_s` seconds captures anyway.
    """

    def __init__(self, confirm_s: float = 5.0, wet_coverage: float = 0.05, wet_level: float = 0.02):
        self.confirm_s = confirm_s
        self.wet_coverage = wet_coverage
        self.wet_level = wet_level
        self.pending_since = None

    def problems(self, t, meas, q, last_motion_t) -> list[str]:
        out = []
        if q is not None and q.reason:
            out.append(q.reason)
        if meas is not None and meas.reliable:
            if meas.flood_coverage >= self.wet_coverage:
                out.append(f"the road shows water ({meas.flood_coverage * 100:.0f}% of the flood ROI)")
            if meas.raw_level_m >= self.wet_level:
                out.append(f"the marker reads {meas.raw_level_m:.2f} m")
        if t - last_motion_t < 2.0:
            out.append("something is moving in view")
        return out

    def press(self, now: float, t, meas, q, last_motion_t) -> tuple[bool, str]:
        """now = wall clock (for the confirmation window), t = video time."""
        found = self.problems(t, meas, q, last_motion_t)
        if not found:
            self.pending_since = None
            return True, "Capturing the dry reference..."
        if self.pending_since is not None and now - self.pending_since <= self.confirm_s:
            self.pending_since = None
            return True, "Capturing the dry reference anyway (confirmed)."
        self.pending_since = now
        return False, ("Dry reference NOT captured: " + "; ".join(found)
                       + f". Press b again within {self.confirm_s:.0f} s to capture anyway.")
