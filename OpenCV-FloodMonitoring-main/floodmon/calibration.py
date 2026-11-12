"""Setup of one camera: flood ROI polygon, marker ROI (4 corners), reference height, references."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

import numpy as np

LOG = logging.getLogger("FloodMonitoring")


class SetupError(ValueError):
    """The setup file cannot be used (clear message for the operator)."""


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
    reference_image: str | None = None  # dry reference frame (png) - older single-reference setups
    reference_images: dict | None = None  # dry reference per lighting {"day": png, "night": png, "ir": png}
    marker_locked: bool = False         # True after 'x': marker geometry is fixed (virtual marker)
    virtual_marker: bool = False        # True once the physical marker was removed and re-referenced
    physical_marker_references: dict | None = None   # dry references taken WITH the physical marker
    lens_file: str | None = None        # lens calibration (tools/calibrate_lens.py) used for this setup
    lens_applied: bool = False          # True: the ROIs / references were made on undistorted frames
    tag_layout: dict | None = None      # ArUco anchor tags on the marker surface (see floodmon/tags.py)
    tag_anchors: dict | None = None     # tag id -> 4 corners (px) in the reference view
    is_default: bool = field(default=False, compare=False)   # auto-created, not saved yet

    def __post_init__(self):
        self.flood_roi = normalize_polygon(self.flood_roi)
        if self.marker_roi is not None:     # sub-pixel corners keep the homography precise
            self.marker_roi = normalize_polygon(self.marker_roi, as_int=False)

    @property
    def ready(self) -> bool:
        return bool(self.flood_roi and len(self.flood_roi) >= 3
                    and self.marker_roi and len(self.marker_roi) == 4)

    @property
    def marker_mode(self) -> str:
        """'physical' (marker in view), 'converting' (locked, marker being removed) or 'virtual'."""
        if self.virtual_marker:
            return "virtual"
        return "converting" if self.marker_locked else "physical"

    @classmethod
    def load(cls, path: str) -> "Calibration":
        """Load a setup file. A broken file raises SetupError with a readable explanation."""
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise SetupError(f"Setup file {path} is not valid JSON (line {e.lineno}, column {e.colno}: "
                             f"{e.msg}). Fix it, or rename it to start a new setup.") from None
        except OSError as e:
            raise SetupError(f"Cannot read setup file {path}: {e}") from None
        if not isinstance(data, dict):
            raise SetupError(f"Setup file {path} must contain a JSON object {{...}}.")
        # older setup files: "roi" + 2-point marker
        if "flood_roi" not in data and data.get("roi"):
            data["flood_roi"] = data["roi"]
        if "marker_roi" not in data and data.get("marker_base") and data.get("marker_top"):
            data["marker_roi"] = marker_quad_from_line(data["marker_base"], data["marker_top"])
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "is_default"}
        problems = validate_setup(known)
        if problems:
            bullet = "\n  - "
            raise SetupError(f"Setup file {path} has problems:{bullet}" + bullet.join(problems)
                             + "\nFix these values, or rename the file to start a new setup.")
        unknown = sorted(k for k in data if k not in cls.__dataclass_fields__
                         and k not in ("roi", "marker_base", "marker_top"))
        if unknown:
            LOG.warning("Setup file %s: ignoring unknown keys %s", path, ", ".join(unknown))
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
        if c.tag_anchors:
            c.tag_anchors = {k: [[round(px * sx, 3), round(py * sy, 3)] for px, py in v]
                             for k, v in c.tag_anchors.items()}
        c.frame_size = [w, h]
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
# Setup file validation
# --------------------------------------------------------------------------
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _points(v, name, n_min, n_exact=None) -> list[str]:
    try:
        pts = normalize_polygon(v, as_int=False)
    except (ValueError, TypeError, IndexError):
        return [f"{name}: not a list of [x, y] points"]
    if pts is None:
        return []
    if any(not (_is_num(p[0]) and _is_num(p[1])) for p in pts):
        return [f"{name}: every point needs two numbers [x, y]"]
    if n_exact is not None and len(pts) != n_exact:
        return [f"{name}: needs exactly {n_exact} corners (found {len(pts)})"]
    if len(pts) < n_min:
        return [f"{name}: needs at least {n_min} points (found {len(pts)})"]
    return []


def validate_setup(d: dict) -> list[str]:
    """Return a list of human-readable problems (empty = OK)."""
    out = []
    if "camera_id" in d and not isinstance(d["camera_id"], str):
        out.append("camera_id: must be text")
    fs = d.get("frame_size")
    if fs is not None and not (isinstance(fs, (list, tuple)) and len(fs) == 2
                               and all(_is_num(v) and v > 0 for v in fs)):
        out.append("frame_size: must be [width, height] in pixels")
    if d.get("flood_roi") is not None:
        out += _points(d["flood_roi"], "flood_roi", 3)
    if d.get("marker_roi") is not None:
        out += _points(d["marker_roi"], "marker_roi", 4, n_exact=4)
    rh = d.get("ref_height_m", 1.0)
    if not (_is_num(rh) and 0.05 <= rh <= 5.0):
        out.append(f"ref_height_m: must be a number between 0.05 and 5 m (found {rh!r})")
    mw = d.get("marker_width_m")
    if mw is not None and not (_is_num(mw) and mw > 0):
        out.append("marker_width_m: must be a positive number (m) or null")
    for key in ("reference_images", "physical_marker_references"):
        v = d.get(key)
        if v is not None and not (isinstance(v, dict) and all(isinstance(x, str) for x in v.values())):
            out.append(f"{key}: must map a lighting name to an image file name")
    for key in ("reference_image", "lens_file"):
        v = d.get(key)
        if v is not None and not isinstance(v, str):
            out.append(f"{key}: must be a file name or null")
    for key in ("marker_locked", "virtual_marker", "lens_applied"):
        if key in d and not isinstance(d[key], bool):
            out.append(f"{key}: must be true or false")
    tl = d.get("tag_layout")
    if tl is not None:
        bad = (not isinstance(tl, dict) or not _is_num(tl.get("size_m")) or tl.get("size_m", 0) <= 0
               or not isinstance(tl.get("tags"), dict) or not tl["tags"])
        if bad:
            out.append("tag_layout: needs size_m (> 0) and tags {id: {x_m, h_m}}")
        else:
            for k, v in tl["tags"].items():
                if not (str(k).isdigit() and isinstance(v, dict) and _is_num(v.get("x_m")) and _is_num(v.get("h_m"))):
                    out.append(f"tag_layout.tags[{k}]: needs a numeric id and numbers x_m, h_m")
    ta = d.get("tag_anchors")
    if ta is not None and not (isinstance(ta, dict) and all(
            isinstance(v, list) and len(v) == 4 and all(isinstance(p, list) and len(p) == 2 for p in v)
            for v in ta.values())):
        out.append("tag_anchors: must map a tag id to 4 corners [[x, y], ...]")
    return out
