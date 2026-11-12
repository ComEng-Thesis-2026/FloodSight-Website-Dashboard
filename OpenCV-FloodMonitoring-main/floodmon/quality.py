"""Image quality assessment + adaptation (night, IR, glare, haze, smoke, noise, shake)."""

from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

LOG = logging.getLogger("FloodMonitoring")


SMALL = (160, 120)          # thumbnail used for global statistics


def dark_channel_index(bgr_small) -> float:
    """Mean dark channel (He et al.). Haze / smoke adds grey 'airlight' and raises it."""
    dc = cv2.erode(bgr_small.min(axis=2), np.ones((5, 5), np.uint8))
    return float(dc.mean() / 255.0)


def edge_map(gray_f32) -> np.ndarray:
    gx = cv2.Sobel(gray_f32, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_f32, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return mag / (float(mag.mean()) + 1e-6)          # normalised -> brightness independent


def classify_lighting(bgr_small, night_brightness: float) -> str:
    """'ir' (camera in black & white night mode), 'night' or 'day'."""
    b = bgr_small.astype(np.int16)
    chroma = float(np.abs(b[..., 0] - b[..., 1]).mean() + np.abs(b[..., 1] - b[..., 2]).mean())
    if chroma < 2.0:
        return "ir"
    gray = float(cv2.cvtColor(bgr_small, cv2.COLOR_BGR2GRAY).mean())
    return "night" if gray < night_brightness else "day"


def robust_photometric(cur, ref, tol: float = 6.0, max_points: int = 6000):
    """
    Brightness mapping  cur ~ gain * ref + offset  from the pixels that did NOT change.

    The 'static' scene (everything outside the ROIs) is not static during a flood:
    water also covers road and walls outside the ROIs, and cars park or pass. Those
    pixels do not follow one common line, so the line with the MOST pixels within
    +/- tol grey levels is the unchanged background (a Hough-style search over the
    gain, then least squares on its inliers). Returns (gain, offset, inlier share,
    contrast ratio of the inliers), or None if no line explains at least 20 %.
    """
    c = np.asarray(cur, np.float32).ravel()
    r = np.asarray(ref, np.float32).ravel()
    if c.size < 50:
        return None
    step = max(1, c.size // max_points)
    c, r = c[::step], r[::step]
    best = (-1, 1.0, 0.0)
    for g in np.exp(np.linspace(np.log(0.25), np.log(4.0), 57)):
        o = c - g * r
        lo = float(o.min())
        counts = np.bincount(((o - lo) / tol).astype(np.int64))
        win = counts[:-1] + counts[1:] if counts.size > 1 else counts
        k = int(win.argmax())
        if win[k] > best[0]:
            best = (int(win[k]), float(g), lo + (k + 1) * tol)
    _, g, off = best
    for _ in range(3):                                   # refine on the inliers
        inl = np.abs(c - g * r - off) < 2 * tol
        if inl.sum() < 20:
            break
        if float(r[inl].std()) > 3.0:
            A = np.vstack([r[inl], np.ones(int(inl.sum()), np.float32)]).T
            (g, off), *_ = np.linalg.lstsq(A, c[inl], rcond=None)
            g = float(np.clip(g, 0.05, 4.0))
        else:                                            # flat background: offset only
            off = float(np.median(c[inl] - g * r[inl]))
    inl = np.abs(c - g * r - off) < 2 * tol
    share = float(inl.mean())
    if share < 0.2:
        return None
    contrast = float(c[inl].std() / max(float(r[inl].std()), 1e-3))
    return float(g), float(off), share, contrast


class RefImage:
    """A dry reference frame with everything pre-computed for fast comparisons."""

    def __init__(self, bgr, lighting: str):
        # bgr_f keeps the exact (float) reference so slow adaptation is not lost to 8-bit
        # rounding; bgr (uint8) is what all comparisons use
        self.bgr_f = np.asarray(bgr, np.float32).copy()
        self.bgr = bgr.copy() if bgr.dtype == np.uint8 else np.clip(self.bgr_f + 0.5, 0, 255).astype(np.uint8)
        self.lighting = lighting
        self.crop = None
        self._win = {}
        g = cv2.cvtColor(self.bgr, cv2.COLOR_BGR2GRAY)
        self.small_bgr = cv2.resize(self.bgr, SMALL, interpolation=cv2.INTER_AREA)
        self.small_gray = cv2.resize(g, SMALL, interpolation=cv2.INTER_AREA).astype(np.float32)
        self.dc = dark_channel_index(self.small_bgr)
        half = cv2.resize(g, (g.shape[1] // 2, g.shape[0] // 2), interpolation=cv2.INTER_AREA).astype(np.float32)
        self.edges_half = edge_map(cv2.GaussianBlur(half, (3, 3), 0))
        self.pct = np.percentile(self.small_gray, [10, 50, 90])

    def prepare(self, crop):
        if self.crop == crop:
            return
        x, y, w, h = crop
        c = self.bgr[y:y + h, x:x + w]
        self.lab = cv2.cvtColor(cv2.GaussianBlur(c, (3, 3), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
        self.gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY).astype(np.float32)
        self.sat = self.gray >= 250
        self._win = {}
        self.crop = crop
        self.texture = float(np.median(self.local_stats(7)[1]))
        self.sharp = float(cv2.Laplacian(self.gray, cv2.CV_32F).var())

    def local_stats(self, win: int):
        if win not in self._win:
            mu = cv2.boxFilter(self.gray, -1, (win, win))
            var = cv2.boxFilter(self.gray * self.gray, -1, (win, win)) - mu * mu
            self._win[win] = (mu, np.sqrt(np.maximum(var, 0)))
        return self._win[win]


@dataclass
class QualityReport:
    score: float = 1.0               # 0 (unusable) .. 1 (perfect)
    mode: str = "GOOD"               # GOOD / FAIR / POOR
    lighting: str = "day"            # day / night / ir
    conditions: list = field(default_factory=list)
    reason: str | None = None        # why measurements cannot be trusted (POOR)
    brightness: float = 0.0          # mean grey level of the work area
    dark_frac: float = 0.0
    sat_frac: float = 0.0
    contrast_ratio: float = 1.0      # global contrast compared with the dry reference
    haze: float = 0.0                # dark-channel increase compared with the dry reference
    noise: float = 0.0               # sensor noise (grey levels) per frame
    noise_eff: float = 0.0           # after temporal averaging
    avg_frames: int = 1
    snr: float = 99.0
    blur_ratio: float = 1.0
    gain: float = 1.0                # photometric mapping  current ~ gain * reference + offset
    offset: float = 0.0
    static_inliers: float = 1.0      # share of the static scene that follows the brightness mapping
    shift: tuple = (0.0, 0.0)        # camera shake compensation (px)
    color_weight: float = 0.6        # how much colour is trusted vs texture
    airlight: np.ndarray | None = None   # haze / smoke colour (when present)
    t_small: np.ndarray | None = None    # haze transmission map (thumbnail size), None = clear
    t_mean: float = 1.0                  # mean transmission over the work area (1 = clear air)

    def summary(self) -> str:
        return f"{self.mode} {self.score:.2f} {self.lighting}" + (f" {','.join(self.conditions)}" if self.conditions else "")


class QualityMonitor:
    """
    Measures how usable each frame is and chooses the adaptation:
      * lighting (day / night / IR) with hysteresis -> reference image to use
      * sensor noise (temporal) -> number of frames to average, wider texture windows
      * global brightness / contrast change vs the dry reference -> photometric mapping
      * haze / smoke (dark channel + contrast loss) -> trust texture more than colour
      * over-exposure / glare and darkness -> pixels without information are ignored
      * camera shake (phase correlation) -> frames are stabilised
      * frozen video, lost signal, covered lens, moved camera -> data marked unreliable
    """

    def __init__(self, args):
        self.night_brightness = args.night_brightness
        self.stabilize = not args.no_stabilize
        self.frozen_s = args.frozen_s
        self.moved_frac = args.moved_frac
        self.moved_s = args.moved_s
        self.shake_px = args.shake_px
        self.robust_mapping = not getattr(args, "percentile_mapping", False)
        self.prev_small = None
        self.prev_crop = None
        self.frozen_since = None
        self.noise_hist = collections.deque(maxlen=45)
        self.lighting = None
        self.light_cand = None
        self.light_cand_since = None
        self.shift = np.zeros(2)
        self.moved_since = None
        self._hann = None

    def _lighting(self, small, t) -> str:
        cand = classify_lighting(small, self.night_brightness)
        if self.lighting is None:
            self.lighting = cand
        elif cand != self.lighting:
            # hysteresis on brightness so dusk does not flip back and forth
            g = float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean())
            if self.lighting == "night" and cand == "day" and g < self.night_brightness * 1.25:
                cand = "night"
            if cand != self.lighting:
                if self.light_cand != cand:
                    self.light_cand, self.light_cand_since = cand, t
                elif t - self.light_cand_since >= 3.0:
                    LOG.info("Lighting changed: %s -> %s", self.lighting, cand)
                    self.lighting = cand
            else:
                self.light_cand = None
        else:
            self.light_cand = None
        return self.lighting

    def _shift(self, frame, ref: RefImage, static_half):
        """Camera shift vs the reference, measured only on the static scene (ROIs masked out,
        because rising water changes them)."""
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        half = cv2.resize(g, (g.shape[1] // 2, g.shape[0] // 2), interpolation=cv2.INTER_AREA).astype(np.float32)
        cur = edge_map(cv2.GaussianBlur(half, (3, 3), 0))
        if cur.shape != ref.edges_half.shape:
            return None, 0.0
        if self._hann is None or self._hann.shape != cur.shape:
            self._hann = cv2.createHanningWindow((cur.shape[1], cur.shape[0]), cv2.CV_32F)
        win = self._hann if static_half is None else self._hann * static_half
        (dx, dy), resp = cv2.phaseCorrelate(ref.edges_half * win, cur * win)
        return np.array([dx * 2.0, dy * 2.0]), float(resp)

    def _transmission(self, small, ref: RefImage):
        """
        Reference-aided haze / smoke model  I = J*t + A*(1-t).
        The dark channel of the clear reference tells how dark each spot really is,
        so t can be estimated per pixel even on bright walls / posts:
            t = (A - dc_now) / (A - dc_ref)
        The map is smoothed strongly so local water changes are not absorbed.
        """
        dcur = cv2.erode(small.min(axis=2), np.ones((5, 5), np.uint8)).astype(np.float32) / 255.0
        dref = cv2.erode(ref.small_bgr.min(axis=2), np.ones((5, 5), np.uint8)).astype(np.float32) / 255.0
        flat = dcur.ravel()
        idx = np.argsort(flat)[-max(10, flat.size // 200):]
        A = small.reshape(-1, 3)[idx].astype(np.float32).mean(axis=0)      # airlight colour
        a_min = float(A.min()) / 255.0
        den = a_min - dref
        tmap = np.where(den > 0.05, (a_min - dcur) / np.maximum(den, 1e-3), np.nan).astype(np.float32)
        fill = float(np.nanmedian(tmap)) if np.isfinite(tmap).any() else 1.0
        tmap = np.where(np.isfinite(tmap), tmap, fill).astype(np.float32)
        tmap = np.clip(cv2.GaussianBlur(np.clip(tmap, 0.05, 1.0), (0, 0), 6), 0.08, 1.0)
        return A, tmap

    def assess(self, frame, t, det) -> QualityReport:
        q = QualityReport()
        H, W = frame.shape[:2]
        small = cv2.resize(frame, SMALL, interpolation=cv2.INTER_AREA)
        gs = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        q.lighting = self._lighting(small, t)
        if q.lighting == "night":
            q.conditions.append("NIGHT")
        elif q.lighting == "ir":
            q.conditions.append("IR-NIGHT")

        # ---- contingencies: no signal / covered lens / frozen picture ----
        if gs.std() < 1.2:
            q.conditions.append("NO-SIGNAL")
            q.reason = "no video signal or lens covered"
        if self.prev_small is not None and float(np.abs(gs - self.prev_small).mean()) < 0.05:
            self.frozen_since = self.frozen_since if self.frozen_since is not None else t
            if t - self.frozen_since >= self.frozen_s:
                q.conditions.append("FROZEN")
                q.reason = q.reason or "video is frozen (camera / network problem)"
        else:
            self.frozen_since = None
        self.prev_small = gs

        ref = det.active
        geom = det.geom
        sm = geom.get("static_small") if geom is not None else None

        # ---- camera shake / moved camera ----
        static = geom.get("static_half") if geom is not None else None
        if self.stabilize and ref is not None and not q.reason and not det.mismatch \
                and (geom is None or static is not None):
            s, resp = self._shift(frame, ref, static)
            if s is not None and resp > 0.1:
                mag = float(np.hypot(*s))
                if mag > self.moved_frac * W:
                    self.moved_since = self.moved_since if self.moved_since is not None else t
                    if t - self.moved_since > self.moved_s:
                        q.conditions.append("CAMERA-MOVED")
                        q.reason = "camera moved - re-check the ROIs"
                else:
                    self.moved_since = None
                    self.shift = s if mag >= 1.0 else np.zeros(2)     # ignore sub-pixel estimates
                    if mag >= self.shake_px:
                        q.conditions.append("CAMERA-SHAKE")
        q.shift = (float(self.shift[0]), float(self.shift[1]))
        if q.shift != (0.0, 0.0):
            # pixel-by-pixel comparisons with the reference below need the stabilised thumbnail
            sx, sy = SMALL[0] / W, SMALL[1] / H
            Ms = np.float32([[1, 0, -q.shift[0] * sx], [0, 1, -q.shift[1] * sy]])
            small = cv2.warpAffine(small, Ms, SMALL, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            gs = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)

        # ---- work-area statistics + sensor noise ----
        if geom is not None:
            x, y, w, h = geom["crop"]
            crop = frame[y:y + h, x:x + w]
        else:
            x, y, w, h = 0, 0, W, H
            crop = frame
        cg = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
        q.brightness = float(cg.mean())
        q.dark_frac = float((cg < 25).mean())
        q.sat_frac = float((cg >= 250).mean())
        sat_full = float((gs >= 250).mean())
        if self.prev_crop is not None and self.prev_crop.shape == cg.shape and not q.reason:
            d = np.abs(cg[::2, ::2] - self.prev_crop[::2, ::2])
            self.noise_hist.append(1.4826 * float(np.median(d)) / math.sqrt(2.0))   # temporal noise
        self.prev_crop = cg
        q.noise = float(np.median(self.noise_hist)) if self.noise_hist else 1.0
        q.avg_frames = int(np.clip(round((q.noise / 1.5) ** 2), 1, 12))
        q.noise_eff = q.noise / math.sqrt(q.avg_frames)
        if q.noise > 4:
            q.conditions.append("NOISY")
        overexposed = sat_full > 0.15 or q.sat_frac > 0.25

        # ---- comparison with the dry reference (static scene only, so the flood does not bias it) ----
        if ref is not None:
            dcur = cv2.erode(small.min(axis=2), np.ones((5, 5), np.uint8)).mean() / 255.0
            q.haze = float(dcur - ref.dc)
            # haze / smoke LOWERS contrast; over-exposure RAISES brightness and contrast
            raw_px, rref_px = (gs[sm], ref.small_gray[sm]) if sm is not None else (gs, ref.small_gray)
            pr0, pc0 = np.percentile(rref_px, [10, 50, 90]), np.percentile(raw_px, [10, 50, 90])
            # over-exposure pushes the highlights to white; smoke / haze greys them instead
            if pc0[2] >= 235 and pc0[1] > pr0[1] + 30:
                overexposed = True
            if q.lighting == "day" and not overexposed and not det.mismatch and q.haze > 0.02:
                A, tmap = self._transmission(small, ref)
                # uniform haze lowers the whole map, smoke plumes only part of it
                if float(tmap.mean()) < 0.92 or float(np.percentile(tmap, 20)) < 0.8:
                    q.airlight, q.t_small = A, tmap
                    dehazed = np.clip((small.astype(np.float32) - A) / tmap[..., None] + A, 0, 255)
                    gs = cv2.cvtColor(dehazed.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
                    sx, sy = SMALL[0] / W, SMALL[1] / H
                    tc = tmap[int(y * sy):max(int((y + h) * sy), int(y * sy) + 1),
                              int(x * sx):max(int((x + w) * sx), int(x * sx) + 1)]
                    q.t_mean = float(tc.mean()) if tc.size else float(tmap.mean())
            cur_px, ref_px = (gs[sm], ref.small_gray[sm]) if sm is not None else (gs, ref.small_gray)
            fit = robust_photometric(cur_px, ref_px, tol=max(4.0, 2.5 * q.noise)) if self.robust_mapping else None
            if fit is not None:
                q.gain, q.offset, q.static_inliers, q.contrast_ratio = fit
            else:                         # no common line (or --percentile-mapping): percentile mapping
                p, pr = np.percentile(cur_px, [10, 50, 90]), np.percentile(ref_px, [10, 50, 90])
                q.gain = float(np.clip((p[2] - p[0]) / max(pr[2] - pr[0], 1.0), 0.05, 4.0))
                q.offset = float(p[1] - q.gain * pr[1])
                q.contrast_ratio = float(cur_px.std() / max(ref_px.std(), 1e-3))
            if geom is not None and ref.crop == geom["crop"]:
                q.snr = ref.texture * q.gain * q.t_mean / max(q.noise_eff, 0.5)

        if q.t_small is not None:
            q.conditions.append("HAZE/SMOKE")
        elif q.contrast_ratio < 0.5:
            q.conditions.append("LOW-CONTRAST")
        if q.brightness < 40 or q.dark_frac > 0.5:
            q.conditions.append("DARK")
        if overexposed:
            q.conditions.append("OVEREXPOSED")
        elif q.sat_frac > 0.02:
            q.conditions.append("GLARE")
        if det.mismatch:
            q.conditions.append("REF-MISMATCH")

        # ---- how much to trust colour (vs texture) ----
        cw = {"day": 0.6, "night": 0.45, "ir": 0.25}[q.lighting]
        if q.t_small is not None:
            cw = min(cw, 0.45)
        if overexposed:
            cw = min(cw, 0.35)
        if det.mismatch:
            cw = min(cw, 0.2)
        q.color_weight = cw

        # ---- overall score + unusable situations ----
        if q.reason is None:
            if q.sat_frac > 0.6:
                q.reason = "image over-exposed (no detail)"
            elif q.snr < 1.0 and ref is not None:
                q.reason = "too dark / too noisy (no detail)"
            elif q.t_mean < 0.15:
                q.reason = "dense smoke / fog (no detail)"
        f_snr = float(np.clip((q.snr - 1.0) / 4.0, 0, 1)) ** 0.5
        f_sat = float(np.clip(1 - q.sat_frac / 0.6, 0, 1))
        f_t = float(np.clip((q.t_mean - 0.15) / 0.45, 0, 1)) ** 0.5
        q.score = 0.0 if q.reason else round(f_snr * f_sat * f_t * (0.85 if det.mismatch else 1.0), 3)
        q.mode = "POOR" if q.reason or q.score < 0.2 else ("FAIR" if q.score < 0.6 else "GOOD")
        if q.mode == "POOR" and q.reason is None:
            q.reason = "image quality too low"
        return q


def enhance_for_display(frame, q: QualityReport):
    """Makes the picture readable for the operator (does not change the measurement)."""
    out = frame
    if "HAZE/SMOKE" in q.conditions or "LOW-CONTRAST" in q.conditions:
        small = cv2.resize(out, (out.shape[1] // 4, out.shape[0] // 4), interpolation=cv2.INTER_AREA)
        I = small.astype(np.float32) / 255.0
        dark = cv2.erode(I.min(axis=2), np.ones((7, 7), np.uint8))
        idx = np.argsort(dark.ravel())[-max(1, dark.size // 1000):]
        A = I.reshape(-1, 3)[idx].mean(axis=0)
        tmap = 1 - 0.9 * cv2.erode((I / np.maximum(A, 1e-3)).min(axis=2), np.ones((7, 7), np.uint8))
        tmap = cv2.resize(cv2.GaussianBlur(tmap, (0, 0), 3), (out.shape[1], out.shape[0]))
        tmap = np.maximum(tmap, 0.25)[..., None]
        J = (out.astype(np.float32) / 255.0 - A) / tmap + A
        out = np.clip(J * 255, 0, 255).astype(np.uint8)
    mean = float(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY).mean())
    if mean < 90 or "OVEREXPOSED" in q.conditions:
        gamma = float(np.clip(math.log(110 / 255.0) / math.log(max(mean, 1) / 255.0), 0.35, 2.5))
        lut = np.clip(((np.arange(256) / 255.0) ** gamma) * 255, 0, 255).astype(np.uint8)
        out = cv2.LUT(out, lut)
    if mean < 90 or q.contrast_ratio < 0.6:
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        lab[..., 0] = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(lab[..., 0])
        out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        if q.noise > 2.5:
            out = cv2.bilateralFilter(out, 7, 40, 7)
    return out
