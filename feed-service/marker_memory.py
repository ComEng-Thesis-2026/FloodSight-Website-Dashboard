"""
The remembered physical marker: what the virtual marker keeps from the physical one.

While the physical reference marker is in view (in the demo clips: only in the first, dry
sequence), the marker ROI is read through floodmon's marker homography (perspective-free,
400 rows per meter, bottom edge = road = 0 m, top edge = reference height):

  * its colour bands, GREEN / YELLOW / RED, and the height of every band boundary,
  * the real (mean) colour of each band, and a photo of the marker itself,
  * the scale it was calibrated with (px per m, cm per px, reference height, ROI corners),
  * the reading of the physical marker at that moment.

This is saved next to the setup (marker_memory.json + marker_memory.png; floodmon's own setup file
keeps only its own keys). Once the marker is virtual, the flood is measured against it:

  * the water level from the flood ROI is placed on the remembered marker,
  * the status is the colour of the remembered band the water reaches (GREEN = SAFE,
    YELLOW = ALERT, RED = DANGER),
  * the overlay draws the remembered marker opaque at its calibrated place, the submerged part
    shaded, plus an enlarged copy with the remembered photo, so the flood behind the marker's place
    does not get in the way of reading it.

A marker composited upside down (red at the road) is remembered as seen, but measured the right
way up (green at the road), and that is reported (FLIP_UPSIDE_DOWN).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import cv2
import numpy as np

from floodmon.bands import FLOOD_BANDS, STATUS_ORDER
from floodmon.geometry import RECT_PPM, MarkerGeometry

LOG = logging.getLogger("feed-service")

MEMORY_JSON = "marker_memory.json"
MEMORY_PNG = "marker_memory.png"
FLIP_UPSIDE_DOWN = True          # measure an upside-down marker the right way up

COLOR_STATUS = {"GREEN": "SAFE", "YELLOW": "ALERT", "RED": "DANGER"}
STATUS_COLOR = {v: k for k, v in COLOR_STATUS.items()}
DRAW_BGR = {b.color_name: b.bgr for b in FLOOD_BANDS}             # the dashboard's band colours
MIN_BAND_M = 0.03                # thinner colour runs are noise / edges
SMOOTH_M = 0.02                  # majority filter along the marker
MIN_COVER = 0.6                  # the bands must cover this share of the marker height
ROI_TOLERANCE_PX = 0.5           # a memory belongs to these exact marker ROI corners


def _classify(hsv_row) -> str | None:
    """Colour of one rectified marker row (OpenCV HSV, median of the central columns)."""
    h, s, v = (float(x) for x in hsv_row)
    if (h <= 10 or h >= 160) and s >= 70 and v >= 30:
        return "RED"
    if 11 <= h <= 38 and s >= 70 and v >= 40:
        return "YELLOW"
    if 38 < h <= 95 and s >= 60 and v >= 20:
        return "GREEN"
    return None


@dataclass
class MarkerMemory:
    bands: list                       # [{color, from_m, to_m, bgr}] bottom -> top, as MEASURED (may be flipped)
    seen_bands: list                  # the same, exactly as seen in the image
    upside_down: bool
    marker_roi: list
    ref_height_m: float
    px_per_m: float
    cm_per_px: float
    reading_m: float | None           # the physical marker's reading when it was remembered
    source: str                       # "lock" or "dry reference <file>"
    learned_at: str
    photo: np.ndarray | None = field(default=None, repr=False)    # rectified marker, road at the bottom

    # ---- status ------------------------------------------------------------
    def band_at(self, level_m: float) -> dict:
        lv = max(0.0, float(level_m))
        for b in self.bands:
            if lv < b["to_m"]:
                return b
        return self.bands[-1]                     # above the marker: the top band

    def color_at(self, level_m: float) -> str:
        return self.band_at(level_m)["color"]

    def status_at(self, level_m: float) -> str:
        return COLOR_STATUS[self.color_at(level_m)]

    def summary(self) -> dict:
        return {
            "bands": [{k: b[k] for k in ("color", "from_m", "to_m")} for b in self.bands],
            "upside_down": self.upside_down,
            "source": self.source,
            "learned_at": self.learned_at,
            "px_per_m": self.px_per_m,
            "cm_per_px": self.cm_per_px,
        }

    def matches(self, cal) -> bool:
        if not cal.marker_roi or abs(float(cal.ref_height_m) - self.ref_height_m) > 1e-6:
            return False
        a, b = np.array(cal.marker_roi, float), np.array(self.marker_roi, float)
        return a.shape == b.shape and float(np.abs(a - b).max()) <= ROI_TOLERANCE_PX

    # ---- files ---------------------------------------------------------------
    def save(self, setup_dir: str):
        data = asdict(self)
        data.pop("photo", None)
        with open(os.path.join(setup_dir, MEMORY_JSON), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if self.photo is not None:
            cv2.imwrite(os.path.join(setup_dir, MEMORY_PNG), self.photo)

    @classmethod
    def load(cls, setup_dir: str) -> "MarkerMemory | None":
        path = os.path.join(setup_dir, MEMORY_JSON)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.pop("photo", None)
            mem = cls(**data)
        except (OSError, ValueError, TypeError) as e:
            LOG.warning("Remembered marker %s is unreadable (%s) - ignored", path, e)
            return None
        mem.photo = cv2.imread(os.path.join(setup_dir, MEMORY_PNG))
        return mem


def delete(setup_dir: str):
    for name in (MEMORY_JSON, MEMORY_PNG):
        try:
            os.remove(os.path.join(setup_dir, name))
        except FileNotFoundError:
            pass


def _runs(labels):
    """Run-length encode a label list: [(label, first, last)]."""
    out = []
    for i, lab in enumerate(labels):
        if out and out[-1][0] == lab:
            out[-1][2] = i
        else:
            out.append([lab, i, i])
    return out


def learn(frame_bgr, cal, source: str, reading_m: float | None = None) -> tuple[MarkerMemory | None, str]:
    """Read the physical marker inside the marker ROI. Returns (memory, message)."""
    if not cal.marker_roi or len(cal.marker_roi) != 4:
        return None, "no marker ROI"
    try:
        mg = MarkerGeometry(cal.marker_roi, cal.ref_height_m)
    except (cv2.error, np.linalg.LinAlgError):
        return None, "the marker ROI is degenerate"
    ref_rows = int(round(cal.ref_height_m * RECT_PPM))
    rect = cv2.warpPerspective(frame_bgr, mg.M_img2rect, (mg.w, mg.rows), flags=cv2.INTER_LINEAR)
    rect = rect[mg.rows - ref_rows:mg.rows]                    # top (ref height) .. road (0 m)
    c0, c1 = int(mg.w * 0.25), int(mg.w * 0.75)                # central columns: no edge blending
    hsv = cv2.cvtColor(rect, cv2.COLOR_BGR2HSV)[:, c0:c1]
    # hue is circular around red: take the median of each channel on the central columns
    up = hsv[::-1]                                             # index 0 = road
    labels = [_classify(np.median(r, axis=0)) for r in up]
    # majority filter along the marker
    win = max(3, int(SMOOTH_M * RECT_PPM) | 1)
    half = win // 2
    smoothed = []
    for i in range(len(labels)):
        seg = [x for x in labels[max(0, i - half):i + half + 1] if x]
        smoothed.append(max(set(seg), key=seg.count) if seg else None)
    runs = [r for r in _runs(smoothed) if r[0] and (r[2] - r[1] + 1) >= MIN_BAND_M * RECT_PPM]
    if not runs:
        return None, "no green / yellow / red bands found in the marker ROI"
    # merge neighbours of the same colour (a short unknown gap between them)
    merged = []
    for lab, a, b in runs:
        if merged and merged[-1][0] == lab:
            merged[-1][2] = b
        else:
            merged.append([lab, a, b])
    covered = sum(b - a + 1 for _, a, b in merged) / len(labels)
    colours = {lab for lab, _, _ in merged}
    if len(colours) < 2 or covered < MIN_COVER:
        return None, (f"the marker ROI shows {len(colours)} colour band(s) over {covered * 100:.0f}% of its height - "
                      "is the physical marker in view and the ROI fitted to it?")
    # boundaries halfway through unknown gaps; the first band starts at the road, the last ends at the top
    n = len(labels)
    edges = [0.0] + [((merged[i][2] + 1 + merged[i + 1][1]) / 2) for i in range(len(merged) - 1)] + [float(n)]
    rect_up = rect[::-1]
    seen = []
    for (lab, a, b), lo, hi in zip(merged, edges[:-1], edges[1:]):
        bgr = np.median(rect_up[a:b + 1, c0:c1].reshape(-1, 3), axis=0)
        seen.append({"color": lab, "from_m": round(lo / RECT_PPM, 3), "to_m": round(hi / RECT_PPM, 3),
                     "bgr": [int(round(x)) for x in bgr]})
    order = [b["color"] for b in seen]
    upside_down = order[0] == "RED" and order[-1] == "GREEN"
    bands = seen
    if upside_down and FLIP_UPSIDE_DOWN:
        top = cal.ref_height_m
        bands = [{**b, "from_m": round(top - b["to_m"], 3), "to_m": round(top - b["from_m"], 3)}
                 for b in reversed(seen)]
    photo = cv2.resize(rect, (mg.w, ref_rows), interpolation=cv2.INTER_AREA)
    if upside_down and FLIP_UPSIDE_DOWN:
        photo = photo[::-1].copy()
    mem = MarkerMemory(
        bands=bands, seen_bands=seen, upside_down=upside_down,
        marker_roi=[[float(x), float(y)] for x, y in cal.marker_roi], ref_height_m=float(cal.ref_height_m),
        px_per_m=round(mg.px_per_m(0.0, min(0.5, cal.ref_height_m)), 2), cm_per_px=mg.cm_per_px_at(0.3),
        reading_m=None if reading_m is None else round(float(reading_m), 3),
        source=source, learned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), photo=photo)
    desc = " / ".join(f"{b['color']} {b['from_m']:.2f}-{b['to_m']:.2f} m" for b in bands)
    msg = f"Physical marker remembered: {desc}"
    if upside_down:
        msg += (" (the marker is UPSIDE DOWN in the video, red at the road - "
                + ("measured the right way up)" if FLIP_UPSIDE_DOWN else "measured as seen)"))
    return mem, msg


class RememberedStatus:
    """Drop-in for floodmon's StatusTracker: the status is the colour of the remembered band the
    water reaches, with the same small hysteresis so it does not flicker at a boundary."""

    def __init__(self, memory: MarkerMemory, hysteresis_m: float = 0.02):
        self.mem, self.h = memory, hysteresis_m
        self.status = STATUS_ORDER[0]

    def update(self, level_m: float) -> str:
        cur = STATUS_ORDER.index(self.status)
        up = STATUS_ORDER.index(self.mem.status_at(level_m))
        if up > cur:
            self.status = STATUS_ORDER[up]
        else:
            down = STATUS_ORDER.index(self.mem.status_at(level_m + self.h))
            if down < cur:
                self.status = STATUS_ORDER[down]
        return self.status


# ---- drawing ----------------------------------------------------------------
def _pt(p):
    return int(round(p[0])), int(round(p[1]))


def _text(img, text, org, scale, color, thick=1):
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    x, y = org
    cv2.rectangle(img, (x - 2, y - th - 3), (x + tw + 2, y + base), (0, 0, 0), -1)
    cv2.putText(img, text, org, font, scale, color, thick, cv2.LINE_AA)


WATER_BGR = (150, 110, 40)


def draw(out, mg: MarkerGeometry, level: float | None, raw: float | None, anomaly: bool, mem: MarkerMemory):
    """The remembered marker, opaque at its calibrated place, plus an enlarged copy."""
    def poly(a, b, s0=0.0, s1=1.0):
        return np.array([_pt(mg.to_img(s0, a)), _pt(mg.to_img(s1, a)), _pt(mg.to_img(s1, b)),
                         _pt(mg.to_img(s0, b))], np.int32)

    lv = None if level is None else float(min(max(level, 0.0), mg.max_m))
    # 1. in place: the remembered bands, opaque; the submerged part shaded with water
    for b in mem.bands:
        cv2.fillPoly(out, [poly(b["from_m"], b["to_m"])], DRAW_BGR[b["color"]])
    if lv and lv > 0:
        layer = out.copy()
        cv2.fillPoly(layer, [poly(0.0, min(lv, mem.ref_height_m))], WATER_BGR)
        cv2.addWeighted(layer, 0.55, out, 0.45, 0, out)
    cv2.polylines(out, [poly(0.0, mem.ref_height_m)], True, (255, 255, 255), 1, cv2.LINE_AA)
    if lv is not None:
        a, b = mg.to_img(-0.8, lv), mg.to_img(1.8, lv)
        cv2.line(out, _pt(a), _pt(b), (0, 0, 0), 4, cv2.LINE_AA)
        cv2.line(out, _pt(a), _pt(b), (255, 255, 0), 2, cv2.LINE_AA)
        _text(out, f"{lv:.2f} m", _pt(mg.to_img(1.8, lv) + np.array([4, 5])), 0.5, (255, 255, 0))

    # 2. enlarged copy (the marker is only a few dozen pixels tall in the picture)
    H, W = out.shape[:2]
    gh = int(min(300, H * 0.55))                                 # gauge height in px
    top, x_photo = 34, W - 150
    x_g, gw = x_photo + 30, 26
    layer = out.copy()
    cv2.rectangle(layer, (x_photo - 10, 4), (W - 4, top + gh + 46), (0, 0, 0), -1)
    cv2.addWeighted(layer, 0.65, out, 0.35, 0, out)
    _text(out, "REMEMBERED MARKER", (x_photo - 6, 20), 0.4, (255, 0, 255))
    to_y = lambda h: int(round(top + gh * (1 - h / mem.ref_height_m)))      # noqa: E731
    if mem.photo is not None:                                   # the real marker as it was seen
        ph = cv2.resize(mem.photo, (22, gh), interpolation=cv2.INTER_NEAREST)
        out[top:top + gh, x_photo:x_photo + 22] = ph
        cv2.rectangle(out, (x_photo, top), (x_photo + 21, top + gh - 1), (200, 200, 200), 1)
    for b in mem.bands:
        cv2.rectangle(out, (x_g, to_y(b["to_m"])), (x_g + gw, to_y(b["from_m"])), DRAW_BGR[b["color"]], -1)
    if lv and lv > 0:
        layer = out.copy()
        cv2.rectangle(layer, (x_g, to_y(min(lv, mem.ref_height_m))), (x_g + gw, to_y(0)), WATER_BGR, -1)
        cv2.addWeighted(layer, 0.55, out, 0.45, 0, out)
    cv2.rectangle(out, (x_g, to_y(mem.ref_height_m)), (x_g + gw, to_y(0)), (255, 255, 255), 1)
    marks = sorted({0.0, mem.ref_height_m, *(b["from_m"] for b in mem.bands[1:])})
    for h in marks:
        y = to_y(h)
        cv2.line(out, (x_g + gw, y), (x_g + gw + 5, y), (255, 255, 255), 1)
        cv2.putText(out, f"{h:.2f}", (x_g + gw + 8, y + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (230, 230, 230), 1,
                    cv2.LINE_AA)
    if lv is not None:
        y = to_y(min(lv, mem.ref_height_m))
        cv2.line(out, (x_photo - 4, y), (x_g + gw + 4, y), (0, 0, 0), 3)
        cv2.line(out, (x_photo - 4, y), (x_g + gw + 4, y), (255, 255, 0), 1)
        color = mem.color_at(lv)
        _text(out, f"{lv:.2f} m {color}", (x_photo - 6, top + gh + 20), 0.45, DRAW_BGR[color])
    if mem.upside_down:
        _text(out, "flipped: upside down in video", (x_photo - 6, top + gh + 38), 0.32, (0, 200, 255))
    if raw is not None and anomaly:
        cv2.circle(out, _pt(mg.to_img(-0.4, min(raw, mg.max_m))), 4, (0, 140, 255), -1)
