"""Video window: mouse setup editor and drawing (ROIs, perspective-correct gauge, info panel)."""

from __future__ import annotations

import cv2
import numpy as np

from .bands import FLOOD_BANDS, MARKER_MAX_M, STATUS_BGR, STATUS_COLOR_NAME, is_above_range
from .calibration import Calibration
from .geometry import MarkerGeometry

FLOOD_COLOR = (255, 200, 0)     # cyan  (BGR) - flood ROI
MARKER_COLOR = (255, 0, 255)    # magenta     - marker ROI


def _seg_dist(p, a, b) -> float:
    p, a, b = (np.asarray(v, np.float64) for v in (p, a, b))
    ab = b - a
    t = float(np.clip(np.dot(p - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1))
    return float(np.linalg.norm(p - (a + t * ab)))


def _inside(poly, x, y) -> bool:
    return bool(poly) and cv2.pointPolygonTest(np.array(poly, np.float32), (float(x), float(y)), False) >= 0


class SetupEditor:
    """
    Mouse editing of both ROIs in the video window:
      left-drag a magenta corner  -> move a MARKER ROI corner
      left-drag a cyan vertex     -> move a FLOOD ROI vertex
      left-drag inside a region   -> move the whole region (marker has priority)
      right-click FLOOD ROI edge  -> insert a vertex
      right-click FLOOD ROI vertex-> delete it (keeps at least 3)
    `changed` becomes True when an edit is finished.
    """

    HANDLE_R = 12

    def __init__(self, cal: Calibration, frame_w: int, frame_h: int):
        self.cal = cal
        self.W, self.H = frame_w, frame_h
        self.drag = None            # ("marker", i) / ("flood", i) / ("move_marker"|"move_flood", x0, y0, orig)
        self.changed = False

    def _clamp(self, x, y):
        return int(np.clip(x, 0, self.W - 1)), int(np.clip(y, 0, self.H - 1))

    def _hit_vertex(self, x, y):
        best, best_d = None, self.HANDLE_R
        for name in ("marker", "flood"):
            if name == "marker" and self.cal.marker_locked:
                continue                                   # locked virtual marker
            for i, p in enumerate(getattr(self.cal, f"{name}_roi") or []):
                d = float(np.hypot(p[0] - x, p[1] - y))
                if d < best_d or (d <= best_d and name == "marker"):
                    best, best_d = (name, i), d
        return best

    def on_mouse(self, event, x, y, flags=0, param=None):
        x, y = self._clamp(x, y)
        cal = self.cal
        if event == cv2.EVENT_LBUTTONDOWN:
            hit = self._hit_vertex(x, y)
            if hit:
                self.drag = hit
            elif _inside(cal.marker_roi, x, y) and not cal.marker_locked:
                self.drag = ("move_marker", x, y, [list(p) for p in cal.marker_roi])
            elif _inside(cal.flood_roi, x, y):
                self.drag = ("move_flood", x, y, [list(p) for p in cal.flood_roi])
        elif event == cv2.EVENT_MOUSEMOVE and self.drag is not None:
            kind = self.drag[0]
            if kind in ("marker", "flood"):
                getattr(cal, f"{kind}_roi")[self.drag[1]] = [x, y]
            else:
                dx, dy = x - self.drag[1], y - self.drag[2]
                moved = [list(self._clamp(px + dx, py + dy)) for px, py in self.drag[3]]
                setattr(cal, "marker_roi" if kind == "move_marker" else "flood_roi", moved)
        elif event == cv2.EVENT_LBUTTONUP and self.drag is not None:
            self.drag = None
            self.changed = True
        elif event == cv2.EVENT_RBUTTONDOWN and cal.flood_roi:
            hit = self._hit_vertex(x, y)
            if hit and hit[0] == "flood":
                if len(cal.flood_roi) > 3:
                    del cal.flood_roi[hit[1]]
                    self.changed = True
                return
            roi = cal.flood_roi
            dists = [_seg_dist((x, y), roi[i], roi[(i + 1) % len(roi)]) for i in range(len(roi))]
            i = int(np.argmin(dists))
            if dists[i] <= self.HANDLE_R:
                roi.insert(i + 1, [x, y])
                self.changed = True


def _pt(p):
    return int(round(p[0])), int(round(p[1]))


def _text(img, text, org, scale, color, thick=1, bg=True):
    font = cv2.FONT_HERSHEY_SIMPLEX
    if bg:
        (tw, th), base = cv2.getTextSize(text, font, scale, thick)
        x, y = org
        cv2.rectangle(img, (x - 2, y - th - 3), (x + tw + 2, y + base), (0, 0, 0), -1)
    cv2.putText(img, text, org, font, scale, color, thick, cv2.LINE_AA)


class LabelLayout:
    """Places text labels in priority order and skips any label that would overlap another."""

    def __init__(self):
        self.boxes = []

    def place(self, img, text, org, scale, color, thick=1, bg=True, force=False) -> bool:
        (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x, y = org
        box = (x - 2, y - th - 3, x + tw + 2, y + base)
        if not force and any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                             for b in self.boxes):
            return False
        self.boxes.append(box)
        _text(img, text, org, scale, color, thick, bg)
        return True


def draw_virtual_marker(out, mg: MarkerGeometry, level: float | None, raw: float | None, anomaly: bool):
    """Virtual staff gauge drawn through the homography (accurate under perspective)."""
    def P(s, h):
        return _pt(mg.to_img(s, h))

    def band_poly(a, b):
        return np.array([P(0, a), P(1, a), P(1, b), P(0, b)], np.int32)

    # faint full gauge
    layer = out.copy()
    for band in FLOOD_BANDS:
        cv2.fillPoly(layer, [band_poly(band.from_m, band.to_m)], band.bgr)
    cv2.addWeighted(layer, 0.30, out, 0.70, 0, out)
    # strong fill up to the current water level (thermometer style)
    if level is not None and level > 0:
        layer = out.copy()
        for band in FLOOD_BANDS:
            top = min(band.to_m, level)
            if top > band.from_m:
                cv2.fillPoly(layer, [band_poly(band.from_m, top)], band.bgr)
        cv2.addWeighted(layer, 0.75, out, 0.25, 0, out)
    cv2.polylines(out, [band_poly(0, MARKER_MAX_M)], True, (255, 255, 255), 1, cv2.LINE_AA)

    # ticks: every 0.05 m (minor) and 0.10 m (major), adaptive labels
    sp_top = mg.px_per_m(MARKER_MAX_M - 0.1, MARKER_MAX_M) * 0.1      # px per 0.10 m at the top
    label_all = sp_top >= 14
    minor = sp_top >= 10
    for i in range(0, int(round(MARKER_MAX_M / 0.05)) + 1):
        h = i * 0.05
        if i % 2 == 0:
            cv2.line(out, P(0, h), P(1, h), (255, 255, 255), 1, cv2.LINE_AA)
        elif minor:
            cv2.line(out, P(0, h), P(0.3, h), (255, 255, 255), 1, cv2.LINE_AA)
            cv2.line(out, P(0.7, h), P(1, h), (255, 255, 255), 1, cv2.LINE_AA)
    def side(h, s, gap):
        """Label anchor beside the marker at height h (s=1 right side, s=0 left side)."""
        edge = mg.to_img(s, h)
        across = mg.to_img(1, h) - mg.to_img(0, h)
        across = across / max(np.linalg.norm(across), 1e-6)
        return edge + across * (gap if s == 1 else -gap)

    # water line (perspective-correct)
    if level is not None:
        lv = min(level, mg.max_m)
        a, b = mg.to_img(-0.6, lv), mg.to_img(1.6, lv)
        cv2.line(out, _pt(a), _pt(b), (0, 0, 0), 4, cv2.LINE_AA)
        cv2.line(out, _pt(a), _pt(b), (255, 255, 0), 2, cv2.LINE_AA)
    if raw is not None:
        cv2.circle(out, P(-0.3, min(raw, mg.max_m)), 4, (0, 140, 255) if anomaly else (255, 255, 255), -1)

    # labels in priority order; lower-priority labels are skipped if they would overlap
    lay = LabelLayout()
    if level is not None:                              # value tag at the end of the water line
        tag = mg.to_img(1.6, min(level, mg.max_m)) + np.array([4, 5])
        lay.place(out, f"{level:.2f} m", _pt(tag), 0.5, (255, 255, 0), 1, force=True)
    # band boundaries first (highest band first), then the road, the top and the reference
    right = [(b.from_m, f"{b.from_m:.2f} m {b.status}", b.bgr, 0.42) for b in reversed(FLOOD_BANDS[1:])]
    right.append((0.0, "0.00 m road", (255, 255, 255), 0.40))
    if abs(mg.ref_h - MARKER_MAX_M) < 1e-6:
        right.append((MARKER_MAX_M, f"{MARKER_MAX_M:.2f} m = REF", (255, 255, 255), 0.42))
    else:
        right.append((MARKER_MAX_M, f"{MARKER_MAX_M:.2f} m", (255, 255, 255), 0.42))
        if mg.ref_h < mg.max_m:
            right.append((mg.ref_h, f"REF {mg.ref_h:.2f} m", MARKER_COLOR, 0.40))
    if label_all:
        taken = {round(b.from_m, 2) for b in FLOOD_BANDS} | {round(MARKER_MAX_M, 2)}
        right += [(round(i * 0.1, 1), f"{i * 0.1:.1f}", (230, 230, 230), 0.38)
                  for i in range(1, int(round(MARKER_MAX_M / 0.1))) if round(i * 0.1, 2) not in taken]
    for h, text, color, scale in right:
        lay.place(out, text, _pt(side(h, 1, 8) + np.array([0, 4])), scale, color, 1)
    for band in FLOOD_BANDS:                          # band names on the left side
        (tw, _), _ = cv2.getTextSize(band.status, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
        mid = (band.from_m + band.to_m) / 2
        lay.place(out, band.status, _pt(side(mid, 0, tw + 10) + np.array([0, 4])), 0.38, band.bgr, 1)


def draw_overlay(frame, det, cal: Calibration, res, meas, status: str | None, fps: float, hint: str | None,
                 show_water: bool = True, show_handles: bool = True, extra_lines: list | None = None):
    out = frame
    # ---- flood ROI + detected water ----
    if cal.flood_roi:
        if show_water and meas is not None and det.geom is not None:
            x, y, w, h = det.geom["crop"]
            wm = (meas.water_mask > 0) & (det.geom["flood_mask"] > 0) & (meas.object_mask == 0)
            if wm.any():
                sub = out[y:y + h, x:x + w]
                tint = sub.copy()
                tint[wm] = (230, 140, 40)
                out[y:y + h, x:x + w] = cv2.addWeighted(sub, 0.6, tint, 0.4, 0)
        poly = np.array(cal.flood_roi, np.int32)
        cv2.polylines(out, [poly], True, FLOOD_COLOR, 2, cv2.LINE_AA)
        tl = min(cal.flood_roi, key=lambda p: p[0] + p[1])
        label = "FLOOD ROI" + (f"  water {meas.flood_coverage * 100:.0f}%" if meas is not None else "")
        _text(out, label, (tl[0] + 8, tl[1] + 20), 0.45, FLOOD_COLOR, 1)
        if show_handles:
            for px, py in cal.flood_roi:
                cv2.rectangle(out, (px - 5, py - 5), (px + 5, py + 5), FLOOD_COLOR, -1)
                cv2.rectangle(out, (px - 5, py - 5), (px + 5, py + 5), (0, 0, 0), 1)
    # ---- marker ROI + virtual marker ----
    if cal.marker_roi:
        quad = np.array(cal.marker_roi, np.int32)
        cv2.polylines(out, [quad], True, MARKER_COLOR, 1, cv2.LINE_AA)
        if det.mg is not None:
            draw_virtual_marker(out, det.mg, res.level_m if res else None,
                                meas.raw_level_m if meas else None, bool(res and res.anomaly))
        # obstructions are detected ONLY inside the marker ROI
        if meas is not None and meas.objects:
            cv2.polylines(out, [np.round(np.array(cal.marker_roi)).astype(np.int32)], True,
                          (0, 140, 255), 2, cv2.LINE_AA)
            for bx, by, bw, bh in meas.objects:
                cv2.rectangle(out, (bx, by), (bx + bw, by + bh), (0, 140, 255), 2)
            bx, by = meas.objects[0][:2]
            _text(out, "OBSTRUCTION", (bx, max(12, by - 6)), 0.42, (0, 140, 255), 1)
        top = min(cal.marker_roi, key=lambda p: p[1])
        if cal.marker_locked:
            tag = "VIRTUAL MARKER (locked)" if cal.virtual_marker else "MARKER LOCKED - remove physical marker"
            _text(out, tag, (int(top[0]) - 40, max(14, int(top[1]) - 22)), 0.42, MARKER_COLOR, 1)
        if show_handles and not cal.marker_locked:     # small hollow handles (grab radius stays 12 px)
            for px, py in cal.marker_roi:
                px, py = int(round(px)), int(round(py))
                cv2.rectangle(out, (px - 4, py - 4), (px + 4, py + 4), (0, 0, 0), 3)
                cv2.rectangle(out, (px - 4, py - 4), (px + 4, py + 4), MARKER_COLOR, 1)
    # ---- info panel ----
    layer = out.copy()
    cv2.rectangle(layer, (0, 0), (380, 176), (0, 0, 0), -1)
    cv2.addWeighted(layer, 0.6, out, 0.4, 0, out)
    font = cv2.FONT_HERSHEY_SIMPLEX
    if res is not None and status:
        cv2.putText(out, f"WATER LEVEL: {res.level_m:.2f} m", (8, 24), font, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        if res.valid:
            above = "  ABOVE RANGE" if is_above_range(res.level_m) else ""
            cv2.putText(out, f"{status} ({STATUS_COLOR_NAME[status]}){above}", (8, 50), font, 0.65,
                        STATUS_BGR[status], 2, cv2.LINE_AA)
        else:
            cv2.putText(out, "NO RELIABLE DATA", (8, 50), font, 0.65, (0, 0, 255), 2, cv2.LINE_AA)
        resol = f"  +/-{meas.resolution_cm_per_px:.1f}cm/px" if meas.resolution_cm_per_px else ""
        cv2.putText(out, f"raw {meas.raw_level_m:.2f} m  rate {res.rate_m_per_min:+.3f} m/min{resol}",
                    (8, 72), font, 0.45, (220, 220, 220), 1, cv2.LINE_AA)
        fl = "FLOOD DETECTED" if meas.flood_detected else "no flood"
        cv2.putText(out, f"{fl}  road water {meas.flood_coverage * 100:.0f}%  marker {'OBSTRUCTED' if meas.objects else 'clear'}",
                    (8, 92), font, 0.45, (230, 180, 60), 1, cv2.LINE_AA)
        if res.anomaly:
            cv2.putText(out, f"ANOMALY: {res.reason}", (8, 112), font, 0.45, (0, 140, 255), 1, cv2.LINE_AA)
    else:
        cv2.putText(out, "SETUP NEEDED", (8, 30), font, 0.7, (0, 200, 255), 2, cv2.LINE_AA)
        cv2.putText(out, "f: flood ROI  m: marker ROI  b: dry ref  s: save", (8, 56), font, 0.42,
                    (255, 255, 255), 1, cv2.LINE_AA)
    pi = det.perspective
    if pi:
        tilt = f"{pi['camera_tilt_deg_est']:.0f} deg" if pi.get("camera_tilt_deg_est") is not None else "n/a"
        cam_h = f"{pi['camera_height_m_est']:.1f} m" if pi.get("camera_height_m_est") else "n/a"
        acc = pi.get("setup_accuracy_cm_per_px", {}).get("1.0m")
        acc_txt = f"  +/-{acc:.1f}cm/px" if acc is not None else ""
        cv2.putText(out, f"tilt {tilt}  cam h {cam_h}  "
                         f"{pi['scale_px_per_m_bottom']:.0f}/{pi['scale_px_per_m_top']:.0f} px/m{acc_txt}",
                    (8, 132), font, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
    q = det.quality
    qcol = {"GOOD": (0, 200, 0), "FAIR": (0, 220, 255), "POOR": (0, 0, 255)}[q.mode]
    cv2.putText(out, f"IMAGE {q.mode} {q.score:.2f}  {q.lighting}  avg x{q.avg_frames}  noise {q.noise:.1f}",
                (8, 150), font, 0.42, qcol, 1, cv2.LINE_AA)
    cond = ", ".join(q.conditions) if q.conditions else "normal"
    cv2.putText(out, (cond if not q.reason else f"{cond}: {q.reason}")[:62], (8, 166), font, 0.36, qcol, 1,
                cv2.LINE_AA)
    cv2.putText(out, f"{fps:4.1f} fps", (322, 24), font, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
    for i, (text, color) in enumerate(extra_lines or []):        # marker mode, lens, tags, recording
        _text(out, text, (8, 192 + 18 * i), 0.42, color, 1)
    if hint:
        _text(out, hint, (8, out.shape[0] - 10), 0.45, (0, 255, 255), 1)
    return out
