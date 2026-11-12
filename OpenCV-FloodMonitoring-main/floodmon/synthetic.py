"""
Synthetic flood scenes with exact ground truth.

A pinhole camera looks down at a wall (vertical plane Z = 0) standing on a road
(horizontal plane Y = 0). World axes: X along the wall, Y up, Z out of the wall
towards the camera, all in meters. Every surface is a textured plane that is
projected into the image with its exact homography, so the true water level of
every frame is known.

Optional events: a painted staff gauge (physical marker), ArUco anchor tags, a
car driving past in front of the marker, camera shake, slow lighting drift and
sensor noise.

Used by tools/make_synthetic_video.py, the unit tests and the regression check
between program versions. Synthetic accuracy is NOT field accuracy: it only
shows that the measuring chain is correct and repeatable.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from .bands import classify_level

WALL_X = (-4.0, 4.0)        # wall texture extent (m)
WALL_TOP = 2.4
WALL_PPM = 220              # wall texture pixels per meter
ROAD_X = (-4.5, 4.5)
ROAD_Z = 3.4
ROAD_PPM = 120
ROAD_SLOPE = 0.02           # cross-fall: water spreads from the wall outwards (m per m)


@dataclass
class SceneConfig:
    width: int = 960
    height: int = 540
    fps: float = 10.0
    duration_s: float = 90.0
    dry_s: float = 12.0             # dry road at the start (reference + background model)
    rise_end_s: float = 62.0        # linear rise from dry_s to rise_end_s
    peak_m: float = 0.80
    hold_s: float = 8.0
    fall_to_m: float = 0.25         # linear fall after the hold, until the end
    gauge: bool = True              # painted staff gauge visible (physical marker)
    gauge_width_m: float = 0.15
    gauge_height_m: float = 1.0
    tags: bool = False              # ArUco anchor tags above the gauge
    tag_size_m: float = 0.12
    tag_dictionary: str = "DICT_4X4_50"
    car_at_s: float | None = 40.0   # a car passes in front of the marker
    car_duration_s: float = 3.0
    shake_at_s: float | None = None
    shake_px: tuple = (6.0, 3.0)
    shake_duration_s: float = 6.0
    light_drift: float = 0.0        # relative brightness change over the clip, e.g. -0.15
    noise_std: float = 2.0
    seed: int = 1
    cam_pos: tuple = (0.55, 2.3, 3.3)
    look_at: tuple = (0.1, 0.55, 0.0)
    focal_px: float = 760.0
    tag_positions: dict = field(default_factory=lambda: {
        # tag id -> centre (x along the wall from the gauge's left edge, height above the road), m
        0: (0.075, 1.20), 1: (-0.55, 1.20), 2: (0.75, 1.20),   # above the water: re-alignment
        3: (-0.35, 0.12), 4: (0.55, 0.12),                      # near the road: setup fit only
    })

    def level_at(self, t: float) -> float:
        if t < self.dry_s:
            return 0.0
        if t < self.rise_end_s:
            return self.peak_m * (t - self.dry_s) / max(self.rise_end_s - self.dry_s, 1e-6)
        t_fall = self.rise_end_s + self.hold_s
        if t < t_fall:
            return self.peak_m
        frac = min(1.0, (t - t_fall) / max(self.duration_s - t_fall, 1e-6))
        return self.peak_m + (self.fall_to_m - self.peak_m) * frac


def _look_at(cam, target):
    c, p = np.asarray(cam, float), np.asarray(target, float)
    f = p - c
    f /= np.linalg.norm(f)
    r = np.cross(f, [0.0, 1.0, 0.0])
    r /= np.linalg.norm(r)
    d = np.cross(f, r)                       # image "down"
    R = np.vstack([r, d, f])
    return R, -R @ c


class Scene:
    def __init__(self, cfg: SceneConfig):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        W, H = cfg.width, cfg.height
        self.K = np.array([[cfg.focal_px, 0, W / 2], [0, cfg.focal_px, H / 2], [0, 0, 1]], float)
        R, t = _look_at(cfg.cam_pos, cfg.look_at)
        self.R, self.t = R, t
        self.H_wall = self.K @ np.column_stack([R[:, 0], R[:, 1], t])     # (X, Y) on Z = 0
        self.H_road = self.K @ np.column_stack([R[:, 0], R[:, 2], t])     # (X, Z) on Y = 0
        self.T_wall = np.array([[1 / WALL_PPM, 0, WALL_X[0]], [0, -1 / WALL_PPM, WALL_TOP], [0, 0, 1]])
        self.T_road = np.array([[1 / ROAD_PPM, 0, ROAD_X[0]], [0, 1 / ROAD_PPM, 0], [0, 0, 1]])
        self.wall_tex = self._wall_texture()
        self.road_tex = self._road_texture()
        self.water_wall = self._water_texture(self.wall_tex.shape[:2], 0)
        self.water_road = self._water_texture(self.road_tex.shape[:2], 1)
        self.background = self._background()
        self._prepare_static()

    # ---- geometry --------------------------------------------------------
    def H_front(self, zc: float):
        """Plane parallel to the wall at distance zc (car side)."""
        R, t = self.R, self.t
        return self.K @ np.column_stack([R[:, 0], R[:, 1], R[:, 2] * zc + t])

    @staticmethod
    def project(H, a, b):
        v = H @ np.array([a, b, 1.0])
        return [float(v[0] / v[2]), float(v[1] / v[2])]

    def wall_point(self, x, y):
        return self.project(self.H_wall, x, y)

    def road_point(self, x, z):
        return self.project(self.H_road, x, z)

    # ---- textures ----------------------------------------------------------
    def _noise(self, shape, scale, amp, seed_off=0):
        rng = np.random.default_rng(self.cfg.seed + 100 + seed_off)
        small = rng.normal(0, 1, (max(2, shape[0] // scale), max(2, shape[1] // scale))).astype(np.float32)
        return cv2.resize(small, (shape[1], shape[0]), interpolation=cv2.INTER_CUBIC) * amp

    def _wall_texture(self):
        h, w = int(WALL_TOP * WALL_PPM), int((WALL_X[1] - WALL_X[0]) * WALL_PPM)
        tex = np.zeros((h, w, 3), np.float32)
        tex[:] = (150, 165, 178)
        tex += self._noise((h, w), 6, 6.0, 1)[..., None]
        tex += self._noise((h, w), 40, 8.0, 2)[..., None]
        # brick courses
        course, brick = int(0.075 * WALL_PPM), int(0.23 * WALL_PPM)
        for r, v in enumerate(range(0, h, course)):
            tex[v:v + 2] = (115, 125, 135)
            off = (r % 2) * brick // 2
            for u in range(off, w, brick):
                tex[v:v + course, u:u + 2] = (115, 125, 135)
        if self.cfg.gauge:
            self._draw_gauge(tex)
        if self.cfg.tags:
            self._draw_tags(tex)
        return np.clip(tex, 0, 255)

    def _wall_uv(self, x, y):
        return (x - WALL_X[0]) * WALL_PPM, (WALL_TOP - y) * WALL_PPM

    def _draw_gauge(self, tex):
        from .bands import FLOOD_BANDS
        gw, gh = self.cfg.gauge_width_m, self.cfg.gauge_height_m
        u0, _ = self._wall_uv(0.0, 0.0)
        u1, _ = self._wall_uv(gw, 0.0)
        u0, u1 = int(round(u0)), int(round(u1))
        colors = {"SAFE": (60, 170, 60), "ALERT": (40, 200, 230), "DANGER": (50, 50, 200)}
        for b in FLOOD_BANDS:
            _, vb = self._wall_uv(0, b.from_m)
            _, vt = self._wall_uv(0, min(b.to_m, gh))
            tex[int(round(vt)):int(round(vb)), u0:u1] = colors[b.status]
        # white E-style ticks every 5 cm, black major ticks every 10 cm
        for i in range(0, int(round(gh / 0.05)) + 1):
            _, v = self._wall_uv(0, i * 0.05)
            v = int(round(v))
            if i % 2 == 0:
                tex[max(0, v - 2):v + 1, u0:u1] = (20, 20, 20)
            else:
                tex[max(0, v - 2):v + 1, u0:u0 + (u1 - u0) // 2] = (240, 240, 240)
        _, vtop = self._wall_uv(0, gh)
        tex[int(round(vtop)) - 3:int(round(vtop)), u0:u1] = (20, 20, 20)

    def _draw_tags(self, tex):
        dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.cfg.tag_dictionary))
        s = self.cfg.tag_size_m
        side = int(round(s * WALL_PPM))
        quiet = int(round(0.025 * WALL_PPM))
        for tag_id, (cx, cy) in self.cfg.tag_positions.items():
            img = cv2.aruco.generateImageMarker(dic, int(tag_id), side)
            u, v = self._wall_uv(cx - s / 2, cy + s / 2)
            u, v = int(round(u)), int(round(v))
            tex[v - quiet:v + side + quiet, u - quiet:u + side + quiet] = (245, 245, 245)
            tex[v:v + side, u:u + side] = img[..., None].astype(np.float32)

    def _road_texture(self):
        h, w = int(ROAD_Z * ROAD_PPM), int((ROAD_X[1] - ROAD_X[0]) * ROAD_PPM)
        tex = np.zeros((h, w, 3), np.float32)
        tex[:] = (88, 90, 92)
        tex += self._noise((h, w), 2, 9.0, 3)[..., None]
        tex += self._noise((h, w), 50, 6.0, 4)[..., None]
        # kerb line along the wall + a lane marking
        tex[: int(0.12 * ROAD_PPM)] = (170, 170, 168)
        z = int(3.2 * ROAD_PPM)
        tex[z:z + 10, ::1] = np.where((np.arange(w) // 150) % 2 == 0, 225, 90)[None, :, None]
        return np.clip(tex, 0, 255)

    def _water_texture(self, shape, seed_off):
        h, w = shape
        base = np.zeros((h, w, 3), np.float32)
        base[:] = (78, 104, 122)                      # muddy brown-grey (BGR)
        base += self._noise((h, w), 30, 10.0, 10 + seed_off)[..., None]
        return base

    def _background(self):
        W, H = self.cfg.width, self.cfg.height
        g = np.linspace(205, 160, H, dtype=np.float32)[:, None, None]
        bg = np.repeat(np.repeat(g, W, axis=1), 3, axis=2)
        bg[..., 0] += 15
        return bg

    # ---- rendering ---------------------------------------------------------
    def _warp(self, tex, H_plane, T):
        W, Hh = self.cfg.width, self.cfg.height
        M = H_plane @ T
        img = cv2.warpPerspective(tex, M, (W, Hh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        m = cv2.warpPerspective(np.ones(tex.shape[:2], np.float32), M, (W, Hh), flags=cv2.INTER_LINEAR,
                                borderValue=0)
        return img, m[..., None]

    def _plane_maps(self, H_plane):
        """Plane coordinates (a, b) of every image pixel + local size of one pixel in b."""
        W, Hh = self.cfg.width, self.cfg.height
        Hi = np.linalg.inv(H_plane)
        u, v = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(Hh, dtype=np.float64))
        d = Hi[2, 0] * u + Hi[2, 1] * v + Hi[2, 2]
        a = (Hi[0, 0] * u + Hi[0, 1] * v + Hi[0, 2]) / d
        b = (Hi[1, 0] * u + Hi[1, 1] * v + Hi[1, 2]) / d
        db = np.abs(np.gradient(b, axis=0)) + 1e-9
        return a.astype(np.float32), b.astype(np.float32), db.astype(np.float32)

    def _prepare_static(self):
        self.wall_img, self.wall_m = self._warp(self.wall_tex, self.H_wall, self.T_wall)
        self.road_img, self.road_m = self._warp(self.road_tex, self.H_road, self.T_road)
        self.water_wall_img, _ = self._warp(self.water_wall, self.H_wall, self.T_wall)
        self.water_road_img, _ = self._warp(self.water_road, self.H_road, self.T_road)
        self.wall_x, self.wall_y, self.wall_dy = self._plane_maps(self.H_wall)
        self.road_x, self.road_z, self.road_dz = self._plane_maps(self.H_road)
        dry = self.background * (1 - self.wall_m) + self.wall_img * self.wall_m
        self.dry = dry * (1 - self.road_m) + self.road_img * self.road_m

    def _ripple(self, x, y, t, phase):
        return (6.0 * np.sin(x * 27.0 + 2.1 * t + phase) * np.sin(y * 37.0 - 1.4 * t)).astype(np.float32)

    def car_x(self, t):
        c = self.cfg
        if c.car_at_s is None or not (c.car_at_s <= t < c.car_at_s + c.car_duration_s):
            return None
        frac = (t - c.car_at_s) / c.car_duration_s
        return -6.0 + 12.0 * frac                     # left edge of the car (m)

    def shake_at(self, t):
        c = self.cfg
        if c.shake_at_s is not None and c.shake_at_s <= t < c.shake_at_s + c.shake_duration_s:
            return tuple(float(v) for v in c.shake_px)
        return (0.0, 0.0)

    def render(self, t: float, noise: bool = True, car: bool = True):
        c = self.cfg
        level = c.level_at(t)
        out = self.dry.copy()
        if level > 0:
            # water on the wall: everything below the water line (anti-aliased in plane units)
            a = np.clip((level - self.wall_y) / self.wall_dy + 0.5, 0, 1)[..., None] * self.wall_m
            water = self.water_wall_img + self._ripple(self.wall_x, self.wall_y, t, 0.0)[..., None]
            out = out * (1 - a) + water * a
            # water on the road spreads from the wall outwards (cross-fall)
            z_edge = min(ROAD_Z, level / ROAD_SLOPE)
            a = np.clip((z_edge - self.road_z) / self.road_dz + 0.5, 0, 1)[..., None] * self.road_m
            water = self.water_road_img + self._ripple(self.road_x, self.road_z, t, 1.3)[..., None]
            out = out * (1 - a) + water * a

        # car on a plane parallel to the wall
        cx = self.car_x(t) if car else None
        if cx is not None:
            ppm = 60
            body = np.zeros((int(1.2 * ppm), int(3.8 * ppm), 3), np.float32)
            body[:] = (120, 45, 35)
            body[int(0.15 * ppm):int(0.5 * ppm), int(0.6 * ppm):int(3.2 * ppm)] = (70, 60, 55)
            T = np.array([[1 / ppm, 0, cx], [0, -1 / ppm, 1.45], [0, 0, 1]])
            img, m = self._warp(body, self.H_front(1.0), T)
            out = out * (1 - m) + img * m

        gain = 1.0 + c.light_drift * (t / max(c.duration_s, 1e-6))
        out = out * gain
        if noise and c.noise_std > 0:
            out = out + self.rng.standard_normal(out.shape, dtype=np.float32) * c.noise_std
        out = np.clip(out, 0, 255).astype(np.uint8)
        dx, dy = self.shake_at(t)
        if dx or dy:
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            out = cv2.warpAffine(out, M, (c.width, c.height), borderMode=cv2.BORDER_REPLICATE)
        truth = dict(t_s=round(t, 3), level_m=round(level, 4), status=classify_level(level),
                     car=cx is not None, shake=bool(dx or dy))
        return out, truth

    def frames(self):
        n = int(round(self.cfg.duration_s * self.cfg.fps))
        for i in range(n):
            frame, truth = self.render(i / self.cfg.fps)
            truth["frame"] = i
            yield frame, truth

    def dry_reference(self, n: int = 15):
        acc = None
        for _ in range(n):
            f, _ = self.render(0.0, noise=True, car=False)
            acc = f.astype(np.float32) if acc is None else acc + f
        return np.clip(acc / n + 0.5, 0, 255).astype(np.uint8)

    # ---- setup that a user would make for this scene ---------------------
    def setup(self) -> dict:
        c = self.cfg
        gw, gh = c.gauge_width_m, c.gauge_height_m
        marker = [self.wall_point(0, 0), self.wall_point(gw, 0), self.wall_point(gw, gh), self.wall_point(0, gh)]
        flood = [self.road_point(x, z) for x, z in ((-1.6, 0.15), (2.0, 0.15), (2.0, 2.4), (-1.6, 2.4))]
        frame = np.float32([[2, 2], [c.width - 3, 2], [c.width - 3, c.height - 3], [2, c.height - 3]])
        _, clipped = cv2.intersectConvexConvex(np.float32(flood), frame)
        if clipped is not None and len(clipped) >= 3:
            flood = clipped.reshape(-1, 2).tolist()
        return dict(
            camera_id="SYNTH",
            frame_size=[c.width, c.height],
            flood_roi=[[int(round(x)), int(round(y))] for x, y in flood],
            marker_roi=[[round(x, 2), round(y, 2)] for x, y in marker],
            ref_height_m=gh,
            marker_width_m=gw,
        )

    def tag_layout(self) -> dict:
        c = self.cfg
        return dict(dictionary=c.tag_dictionary, size_m=c.tag_size_m,
                    tags={str(k): {"x_m": v[0], "h_m": v[1]} for k, v in c.tag_positions.items()})


def write_scene(cfg: SceneConfig, out_dir: str, video_name: str = "scene.mp4") -> dict:
    """Render a scene to out_dir: video, ground truth CSV, dry reference, setup file."""
    os.makedirs(out_dir, exist_ok=True)
    scene = Scene(cfg)
    setup = scene.setup()
    setup["reference_images"] = {"day": "reference_dry_day.png"}
    if cfg.tags:
        setup["tag_layout"] = scene.tag_layout()
    with open(os.path.join(out_dir, "flood_config.json"), "w", encoding="utf-8") as f:
        json.dump(setup, f, indent=2)
    video = os.path.join(out_dir, video_name)
    vw = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"mp4v"), cfg.fps, (cfg.width, cfg.height))
    if not vw.isOpened():
        raise RuntimeError("Cannot write the video file (no mp4v encoder?)")
    gt_path = os.path.join(out_dir, "ground_truth.csv")
    with open(gt_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["frame", "t_s", "level_m", "status", "car", "shake"])
        wr.writeheader()
        for frame, truth in scene.frames():
            vw.write(frame)
            wr.writerow({k: (int(v) if isinstance(v, bool) else v) for k, v in truth.items()})
    vw.release()
    # dry reference = average of the first dry frames AS ENCODED in the video, exactly like
    # the program's own 'b' capture from a compressed CCTV stream
    cap = cv2.VideoCapture(video)
    acc, n = None, 0
    while n < min(15, int(cfg.dry_s * cfg.fps) - 1):
        ok, f = cap.read()
        if not ok:
            break
        acc = f.astype(np.float32) if acc is None else acc + f
        n += 1
    cap.release()
    ref = np.clip(acc / max(n, 1) + 0.5, 0, 255).astype(np.uint8) if acc is not None else scene.dry_reference()
    cv2.imwrite(os.path.join(out_dir, "reference_dry_day.png"), ref)
    meta = dict(asdict(cfg), video=video_name, ground_truth="ground_truth.csv",
                start_time="2026-01-01T08:00:00+08:00")
    meta["tag_positions"] = {str(k): v for k, v in cfg.tag_positions.items()}
    with open(os.path.join(out_dir, "scene.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return dict(video=video, ground_truth=gt_path, config=os.path.join(out_dir, "flood_config.json"))


PRESETS = {
    # physical marker in view, car passes, slight lighting drift
    "physical": dict(gauge=True, car_at_s=40.0, light_drift=-0.06),
    # no physical marker: the marker ROI sits on the plain wall behind it
    "virtual": dict(gauge=False, car_at_s=40.0, light_drift=-0.06),
    # physical marker with ArUco anchors and a camera bump
    "tags": dict(gauge=True, tags=True, car_at_s=None, shake_at_s=30.0, shake_px=(14.0, 8.0),
                 shake_duration_s=20.0),
    # the camera is knocked out of position for good (larger than stabilisation can follow)
    "bump": dict(gauge=True, tags=True, car_at_s=None, shake_at_s=30.0, shake_px=(70.0, 25.0),
                 shake_duration_s=1000.0),
    # short clip for unit tests
    "quick": dict(gauge=True, duration_s=30.0, dry_s=6.0, rise_end_s=26.0, peak_m=0.6, hold_s=2.0,
                  fall_to_m=0.6, car_at_s=None, fps=8.0, width=640, height=360, focal_px=507.0),
}


def preset(name: str, **overrides) -> SceneConfig:
    kw = dict(PRESETS[name])
    kw.update(overrides)
    return SceneConfig(**kw)
