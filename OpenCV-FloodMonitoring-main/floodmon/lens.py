"""
Lens calibration and undistortion.

Wide-angle CCTV lenses (e.g. Dahua 2.8 mm, about 100 deg) bend straight lines,
most strongly near the image edges. The marker homography assumes straight
lines, so an uncorrected marker away from the image centre is measured with a
systematic error of several centimetres. A one-time checkerboard calibration
(tools/calibrate_lens.py) gives the camera matrix and distortion coefficients;
every frame is then undistorted before anything is measured.

Important: ROIs and dry references must be made on UNDISTORTED frames. Turning
the correction on or off after the setup means the ROIs must be re-fitted and
the references re-captured (the program warns about this).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

import cv2
import numpy as np


@dataclass
class LensModel:
    camera_matrix: list                     # 3x3
    dist_coeffs: list                       # k1 k2 p1 p2 k3 (...)
    image_size: list                        # [w, h] of the calibration images
    rms_px: float = 0.0                     # reprojection error of the calibration
    n_images: int = 0
    camera: str = ""
    created: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))

    @property
    def K(self) -> np.ndarray:
        return np.array(self.camera_matrix, np.float64)

    @property
    def D(self) -> np.ndarray:
        return np.array(self.dist_coeffs, np.float64).ravel()

    @classmethod
    def load(cls, path: str) -> "LensModel":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        K = np.array(d.get("camera_matrix"), float)
        if K.shape != (3, 3) or len(d.get("image_size", [])) != 2:
            raise ValueError(f"{path}: not a lens calibration file (camera_matrix 3x3 + image_size needed)")
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    def scaled_K(self, w: int, h: int) -> np.ndarray:
        """Camera matrix for frames of size w x h (the calibration may be at another resolution)."""
        sx, sy = w / self.image_size[0], h / self.image_size[1]
        K = self.K.copy()
        K[0, 0] *= sx
        K[0, 2] *= sx
        K[1, 1] *= sy
        K[1, 2] *= sy
        return K

    def undistorter(self, w: int, h: int, alpha: float = 0.0) -> "Undistorter":
        return Undistorter(self, w, h, alpha)


class Undistorter:
    """Pre-computed remap tables: undistorting a frame costs one cv2.remap."""

    def __init__(self, lens: LensModel, w: int, h: int, alpha: float = 0.0):
        K = lens.scaled_K(w, h)
        D = lens.D
        # alpha 0: keep only valid pixels (no black corners) at the same output size
        self.new_K, _ = cv2.getOptimalNewCameraMatrix(K, D, (w, h), alpha, (w, h))
        self.map1, self.map2 = cv2.initUndistortRectifyMap(K, D, None, self.new_K, (w, h), cv2.CV_16SC2)
        self.size = (w, h)

    def __call__(self, frame):
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError("frame size changed after the undistortion maps were made")
        return cv2.remap(frame, self.map1, self.map2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------
# Calibration from checkerboard views
# --------------------------------------------------------------------------
def board_object_points(pattern: tuple[int, int], square_m: float) -> np.ndarray:
    """3D corner positions of a checkerboard with `pattern` = inner corners (cols, rows)."""
    cols, rows = pattern
    obj = np.zeros((cols * rows, 3), np.float32)
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square_m
    return obj


def find_board(gray, pattern: tuple[int, int]):
    """Sub-pixel checkerboard corners, or None."""
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    ok, corners = cv2.findChessboardCornersSB(gray, pattern, flags=flags)
    return corners.reshape(-1, 1, 2).astype(np.float32) if ok else None


def calibrate(obj_points: list, img_points: list, image_size: tuple[int, int], camera: str = "") -> tuple:
    """Returns (LensModel, per-view reprojection errors in px)."""
    if len(obj_points) < 5:
        raise ValueError("at least 5 checkerboard views are needed (15-25 recommended)")
    rms, K, D, rvecs, tvecs = cv2.calibrateCamera(obj_points, img_points, image_size, None, None)
    per_view = []
    for o, i, r, t in zip(obj_points, img_points, rvecs, tvecs):
        proj, _ = cv2.projectPoints(o, r, t, K, D)
        per_view.append(float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - i.reshape(-1, 2)) ** 2, axis=1)))))
    lens = LensModel(camera_matrix=K.tolist(), dist_coeffs=D.ravel().tolist(), image_size=list(image_size),
                     rms_px=float(rms), n_images=len(obj_points), camera=camera)
    return lens, per_view


def max_edge_shift_px(lens: LensModel, w: int, h: int) -> float:
    """How far (px) the lens moves points near the image border - shows how much correction matters."""
    K, D = lens.scaled_K(w, h), lens.D
    pts = np.float64([[x, y] for x in np.linspace(0, w - 1, 9) for y in (0, h - 1)]
                     + [[x, y] for x in (0, w - 1) for y in np.linspace(0, h - 1, 9)]).reshape(-1, 1, 2)
    und = cv2.undistortPoints(pts, K, D, P=K)
    return float(np.max(np.linalg.norm(und.reshape(-1, 2) - pts.reshape(-1, 2), axis=1)))
