"""
Flood marker levels - the ONE place where the bands are defined.

    GREEN   0.00 - 0.30 m   SAFE
    YELLOW  0.30 - 0.50 m   ALERT
    RED     0.50 - 1.00 m   DANGER

Everything else (classification, colours, gauge drawing, labels) is derived
from FLOOD_BANDS, so changing a threshold here changes it everywhere.
"""

from __future__ import annotations

from typing import NamedTuple


class Band(NamedTuple):
    from_m: float
    to_m: float
    status: str
    color_name: str
    bgr: tuple
    hex: str


MARKER_MAX_M = 1.0          # top of the gauge (m)

FLOOD_BANDS = (
    Band(0.00, 0.30, "SAFE", "GREEN", (0, 200, 0), "#22c55e"),
    Band(0.30, 0.50, "ALERT", "YELLOW", (0, 220, 255), "#facc15"),
    Band(0.50, MARKER_MAX_M, "DANGER", "RED", (0, 0, 255), "#ef4444"),
)

STATUS_ORDER = [b.status for b in FLOOD_BANDS]
STATUS_BGR = {b.status: b.bgr for b in FLOOD_BANDS}
STATUS_HEX = {b.status: b.hex for b in FLOOD_BANDS}
STATUS_COLOR_NAME = {b.status: b.color_name for b in FLOOD_BANDS}


def classify_level(level_m: float) -> str:
    """Status of a water level. Levels above the gauge stay in the highest band."""
    for band in FLOOD_BANDS[:-1]:
        if level_m < band.to_m:
            return band.status
    return FLOOD_BANDS[-1].status


def band_boundaries() -> list[float]:
    """Inner thresholds between bands, e.g. [0.30, 0.50]."""
    return [b.from_m for b in FLOOD_BANDS[1:]]


def is_above_range(level_m: float, margin_m: float = 0.005) -> bool:
    """True when the water reaches the top of the gauge: the real level may be higher."""
    return level_m >= MARKER_MAX_M - margin_m


class StatusTracker:
    """Flood status with a small hysteresis so it does not flicker at a boundary."""

    def __init__(self, hysteresis_m: float = 0.02):
        self.h = hysteresis_m
        self.status = STATUS_ORDER[0]

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
