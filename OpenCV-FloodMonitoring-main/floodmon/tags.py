"""
ArUco anchor tags on the marker surface.

Small printed tags (tools/make_aruco_tags.py) are fixed on the SAME flat surface
as the flood marker. Put at least two ABOVE the highest expected water (they keep
the camera aligned during a flood) and one or two near the road beside the
marker (they make the setup fit accurate; they are only needed while fitting,
when the road is dry). A layout file says where each tag's centre is, in meters:

    {"dictionary": "DICT_4X4_50", "size_m": 0.12,
     "tags": {"0": {"x_m": 0.075, "h_m": 1.20}, "1": {"x_m": -0.55, "h_m": 1.20},
              "3": {"x_m": -0.35, "h_m": 0.12}}}

    x_m : across the surface, from the marker's LEFT edge (to the right = positive)
    h_m : height of the tag centre above the road surface (0 m)
    size_m : side of the black square (without the white margin)

They give two things:
  1. Automatic marker ROI: the 4 marker corners are computed from the tags with
     sub-pixel accuracy instead of being clicked ('g' key).
  2. Re-alignment: if the camera is bumped or slowly creeps, each frame is warped
     back onto the setup view, so the ROIs and dry references stay valid. Without
     tags, a large camera move can only be reported (CAMERA-MOVED).
"""

from __future__ import annotations

import json
import logging

import cv2
import numpy as np

LOG = logging.getLogger("FloodMonitoring")


def load_layout(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        layout = json.load(f)
    if not isinstance(layout, dict) or "tags" not in layout or "size_m" not in layout:
        raise ValueError(f"{path}: tag layout needs 'size_m' and 'tags'")
    layout.setdefault("dictionary", "DICT_4X4_50")
    return layout


class TagDetector:
    def __init__(self, dictionary: str = "DICT_4X4_50"):
        if not hasattr(cv2.aruco, dictionary):
            raise ValueError(f"unknown ArUco dictionary {dictionary}")
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
        prm = cv2.aruco.DetectorParameters()
        prm.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        prm.cornerRefinementWinSize = 4
        self.detector = cv2.aruco.ArucoDetector(d, prm)

    def detect(self, frame) -> dict[int, np.ndarray]:
        """tag id -> 4 corners (TL, TR, BR, BL of the printed tag), float px."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _ = self.detector.detectMarkers(gray)
        if ids is None:
            return {}
        return {int(i): c.reshape(4, 2).astype(np.float64) for c, i in zip(corners, ids.ravel())}


def tag_plane_corners(layout: dict, tag_id) -> np.ndarray:
    """Corners of one tag on the marker plane (x_m, h_m), in ArUco order TL, TR, BR, BL."""
    t = layout["tags"][str(tag_id)]
    s = float(layout["size_m"]) / 2
    x, h = float(t["x_m"]), float(t["h_m"])
    return np.array([[x - s, h + s], [x + s, h + s], [x + s, h - s], [x - s, h - s]], np.float64)


def fit_marker_from_tags(layout: dict, detections: dict, marker_width_m: float, ref_height_m: float):
    """
    Marker ROI corners from the visible tags (plane -> image homography, least squares).
    Returns dict(quad=[bl, br, tr, tl], rms_px, tags=[ids], H) or None.
    With one tag the 4 tag corners define the plane; more tags spread over the surface
    are much better (less extrapolation down to the road).
    """
    ids = [i for i in detections if str(i) in layout["tags"]]
    if not ids:
        return None
    plane = np.vstack([tag_plane_corners(layout, i) for i in ids])
    img = np.vstack([detections[i] for i in ids])
    H, _ = cv2.findHomography(plane, img, 0)
    if H is None:
        return None
    proj = cv2.perspectiveTransform(plane.reshape(-1, 1, 2), H).reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((proj - img) ** 2, axis=1))))
    heights = [float(layout["tags"][str(i)]["h_m"]) for i in ids]
    if max(heights) - min(heights) < 0.3 * ref_height_m:
        LOG.warning("All visible tags are at about the same height: the marker fit has to extrapolate "
                    "down to the road and can be off by several pixels. Add a tag near the road "
                    "(it is only needed while fitting, when the road is dry).")
    corners = np.float64([[0, 0], [marker_width_m, 0], [marker_width_m, ref_height_m], [0, ref_height_m]])
    quad = cv2.perspectiveTransform(corners.reshape(-1, 1, 2), H).reshape(-1, 2)
    return dict(quad=[[round(float(x), 2), round(float(y), 2)] for x, y in quad], rms_px=rms,
                tags=sorted(ids), H=H)


class TagAligner:
    """
    Keeps every frame aligned with the setup view.

    The anchors are the tag corners in the setup view (saved in the setup file).
    For each frame the visible tags give a transform current -> setup view. A bumped
    or creeping CCTV camera mostly shifts, rotates and zooms the picture slightly, so a
    SIMILARITY transform (4 parameters) is used: it stays accurate far away from the
    tags, where a full homography fitted to a few tags in one row would extrapolate
    badly (tags are mounted high, the water line is low). The transform is smoothed
    with the median of the last `smooth` frames, and moves below `min_move_px` at the
    marker are ignored (no resampling noise when nothing moved).
    """

    def __init__(self, anchors: dict, detector: TagDetector, max_err_px: float = 1.5,
                 min_move_px: float = 0.5, smooth: int = 5):
        self.anchors = {int(k): np.asarray(v, np.float64) for k, v in anchors.items()}
        self.detector = detector
        self.max_err = max_err_px
        self.min_move = min_move_px
        self.hist = []
        self.smooth = max(1, int(smooth))
        self.last = dict(visible=0, move_px=0.0, err_px=0.0, corrected=False, status="not started")

    @staticmethod
    def _similarity(src, dst):
        """Least-squares similarity src -> dst: [a -b tx; b a ty]."""
        n = len(src)
        A = np.zeros((2 * n, 4))
        A[0::2] = np.column_stack([src[:, 0], -src[:, 1], np.ones(n), np.zeros(n)])
        A[1::2] = np.column_stack([src[:, 1], src[:, 0], np.zeros(n), np.ones(n)])
        (a, b, tx, ty), *_ = np.linalg.lstsq(A, dst.reshape(-1), rcond=None)
        return np.array([a, b, tx, ty])

    @staticmethod
    def _matrix(prm):
        a, b, tx, ty = prm
        return np.array([[a, -b, tx], [b, a, ty], [0.0, 0.0, 1.0]])

    def update(self, frame):
        det = self.detector.detect(frame)
        common = sorted(i for i in det if i in self.anchors)
        info = dict(visible=len(common), move_px=0.0, err_px=0.0, corrected=False)
        if not common:
            info["status"] = "tags not visible"
            self.last = info
            return frame, info
        src = np.vstack([det[i] for i in common])
        dst = np.vstack([self.anchors[i] for i in common])
        prm = self._similarity(src, dst)
        M = self._matrix(prm)
        proj = (M[:2, :2] @ src.T).T + M[:2, 2]
        info["err_px"] = float(np.sqrt(np.mean(np.sum((proj - dst) ** 2, axis=1))))
        if info["err_px"] > self.max_err:
            info["status"] = f"tag fit poor ({info['err_px']:.1f}px)"
            self.last = info
            return frame, info
        self.hist.append(prm)
        self.hist = self.hist[-self.smooth:]
        M = self._matrix(np.median(np.array(self.hist), axis=0))
        h, w = frame.shape[:2]
        grid = np.float64([[x, y] for x in (0, w / 2, w - 1) for y in (0, h / 2, h - 1)])
        info["move_px"] = float(np.max(np.linalg.norm((M[:2, :2] @ grid.T).T + M[:2, 2] - grid, axis=1)))
        if info["move_px"] < self.min_move:
            info["status"] = "aligned"
            self.last = info
            return frame, info
        out = cv2.warpAffine(frame, M[:2], (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        info.update(corrected=True, status=f"re-aligned {info['move_px']:.1f}px")
        self.last = info
        return out, info
