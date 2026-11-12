"""Live data feed: payload (future website format), terminal output."""

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

import cv2
import numpy as np

from . import __version__
from .bands import MARKER_MAX_M, STATUS_COLOR_NAME, STATUS_HEX, is_above_range
from .calibration import Calibration

LOG = logging.getLogger("FloodMonitoring")


def build_payload(cal: Calibration, source_kind: str, fps: float, meas, res, status: str | None, det,
                  timestamp: datetime | None = None, t_s: float | None = None, extra: dict | None = None) -> dict:
    """One reading as a dict (the future website format). `timestamp` is the time the frame was
    recorded (video time for files), `t_s` the source time in seconds."""
    q = det.quality
    ts = timestamp or datetime.now().astimezone()
    payload = {
        "camera_id": cal.camera_id,
        "timestamp": ts.isoformat(timespec="milliseconds" if source_kind == "file" else "seconds"),
        "t_s": None if t_s is None else round(float(t_s), 3),
        "calibrated": cal.ready and not cal.is_default,
        "marker_mode": cal.marker_mode,
        "virtual_marker": cal.virtual_marker,
        "marker_locked": cal.marker_locked,
        "source": source_kind,
        "fps": round(fps, 1),
        "image_quality": {
            "score": q.score, "mode": q.mode, "lighting": q.lighting, "conditions": q.conditions,
            "problem": q.reason, "noise": round(q.noise, 2), "frames_averaged": q.avg_frames,
            "reference": (det.active.lighting if det.active else None), "reference_mismatch": det.mismatch,
        },
    }
    if res is None:
        payload.update({"water_level_m": None, "status": "NOT_CALIBRATED"})
        return payload
    payload.update({
        "data_valid": res.valid,
        "data_held_s": round(res.held_s, 1),
        "water_level_m": round(res.level_m, 3),
        "raw_level_m": round(meas.raw_level_m, 3),
        "rate_m_per_min": round(res.rate_m_per_min, 4),
        "status": status,
        "status_color": STATUS_COLOR_NAME[status],
        "status_hex": STATUS_HEX[status],
        "above_range": bool(is_above_range(res.level_m)),
        "range_max_m": MARKER_MAX_M,
        "resolution_cm_per_px": meas.resolution_cm_per_px,
        "flood_detected": meas.flood_detected,
        "flood_coverage_pct": round(meas.flood_coverage * 100, 1),
        "anomaly": res.anomaly,
        "anomaly_reason": res.reason,
        "obstruction_at_marker": bool(meas.objects),
        "motion_ratio": round(meas.motion_ratio, 3),
        "confidence": round(meas.confidence, 3),
        "method": "reference" if det.uses_reference else "color",
        "ref_height_m": cal.ref_height_m,
        "perspective": det.perspective,
    })
    if extra:
        payload.update(extra)
    return payload


def offline_payload(cal: Calibration, source_kind: str, seconds: float, last_level: float | None,
                    timestamp: datetime | None = None) -> dict:
    """Sent while a live camera delivers no frames, so the website can show 'camera offline'."""
    ts = timestamp or datetime.now().astimezone()
    return {
        "camera_id": cal.camera_id,
        "timestamp": ts.isoformat(timespec="seconds"),
        "t_s": None,
        "calibrated": cal.ready and not cal.is_default,
        "marker_mode": cal.marker_mode,
        "source": source_kind,
        "status": "CAMERA_OFFLINE",
        "data_valid": False,
        "offline_s": round(seconds, 1),
        "water_level_m": None,
        "last_water_level_m": None if last_level is None else round(last_level, 3),
        "anomaly": True,
        "anomaly_reason": f"no video from the camera for {seconds:.0f} s",
    }


def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class TerminalFeed:
    """
    Prints the live readings in the terminal:
      * one line every `interval` seconds, and
      * immediately when the flood status changes or an obstruction starts/ends.
    """

    ANSI = {"SAFE": "\033[92m", "ALERT": "\033[93m", "DANGER": "\033[91m",
            "WARN": "\033[38;5;208m", "END": "\033[0m"}

    def __init__(self, interval_s: float, json_mode: bool, color: bool):
        self.interval = interval_s
        self.json_mode = json_mode
        self.color = color and not json_mode and _enable_ansi()
        self._last_print = -1e9
        self._last_status = None
        self._last_anomaly = False
        self._last_held = False
        self._last_qmode = None
        self._last_light = None

    def _c(self, key, text):
        return f"{self.ANSI[key]}{text}{self.ANSI['END']}" if self.color else text

    def format_line(self, d: dict, event: str | None) -> str:
        ts = d["timestamp"][11:19]
        if d.get("status") == "CAMERA_OFFLINE":
            last = d.get("last_water_level_m")
            return (f"{ts}  {d['camera_id']}  " + self._c("WARN", f"CAMERA OFFLINE {d['offline_s']:.0f}s")
                    + (f"  last level {last:.2f} m" if last is not None else "")
                    + (f"  << {event}" if event else ""))
        if d.get("water_level_m") is None:
            return f"{ts}  {d['camera_id']}  NOT CALIBRATED  (f: flood ROI, m: marker ROI, b: dry reference, s: save)"
        st = d["status"]
        line = (f"{ts}  {d['camera_id']}  LEVEL {d['water_level_m']:.2f} m  "
                + self._c(st, f"[{st:<6} {d['status_color']:<6}]")
                + f"  raw {d['raw_level_m']:.2f} m  rate {d['rate_m_per_min']:+.3f} m/min"
                + f"  flood {d['flood_coverage_pct']:5.1f}%  conf {d['confidence']:.2f}"
                + f"  img {d['image_quality']['mode']} {d['image_quality']['score']:.2f}"
                + (f" {','.join(d['image_quality']['conditions'])}" if d['image_quality']['conditions'] else "")
                + f"  {d['fps']:4.1f} fps  ")
        if d.get("above_range"):
            line += self._c("DANGER", f"ABOVE {d['range_max_m']:.2f} m GAUGE RANGE  ")
        if not d.get("data_valid", True):
            line += self._c("WARN", f"NO RELIABLE DATA ({d['anomaly_reason']})")
        elif d["anomaly"]:
            line += self._c("WARN", ("HOLDING: " if "held" in (d['anomaly_reason'] or "") else "OBSTRUCTION: ")
                            + str(d['anomaly_reason']))
        else:
            line += "OK"
        if not d["calibrated"]:
            line += "  (default ROIs - adjust & press s)"
        if event:
            warn = any(k in event for k in ("OBSTRUCTION", "HELD", "QUALITY", "LIGHTING"))
            line += "  " + self._c("WARN" if warn else st, f"<< {event}")
        return line

    def emit(self, d: dict, now: float):
        event = None
        st, an = d.get("status"), bool(d.get("anomaly"))
        iq = d.get("image_quality", {})
        qmode, light = iq.get("mode"), iq.get("lighting")
        held = "held" in str(d.get("anomaly_reason") or "")
        if st == "CAMERA_OFFLINE":
            if self._last_status != "CAMERA_OFFLINE":
                event = "CAMERA OFFLINE"
        elif d.get("water_level_m") is not None:
            if self._last_status == "CAMERA_OFFLINE":
                event = "CAMERA BACK ONLINE"
            elif st != self._last_status:
                event = "STATUS CHANGED" if self._last_status not in (None, "NOT_CALIBRATED") else "START"
            elif an and not self._last_anomaly:
                event = "IMAGE NOT USABLE - LEVEL HELD" if held else "OBSTRUCTION DETECTED"
            elif not an and self._last_anomaly:
                event = "MEASURING AGAIN" if self._last_held else "OBSTRUCTION CLEARED"
        if event is None and self._last_light is not None and light != self._last_light:
            event = f"LIGHTING {str(light).upper()}"
        elif event is None and self._last_qmode is not None and qmode != self._last_qmode:
            event = f"IMAGE QUALITY {qmode}"
        self._last_status, self._last_anomaly, self._last_held = st, an, held
        self._last_qmode, self._last_light = qmode, light
        if event is None and now - self._last_print < self.interval:
            return
        self._last_print = now
        if self.json_mode:
            print(json.dumps(dict(d, event=event)), flush=True)
        else:
            print(self.format_line(d, event), flush=True)


# --------------------------------------------------------------------------
# CSV log + settings sidecar
# --------------------------------------------------------------------------
CSV_COLUMNS = ["timestamp", "t_s", "water_level_m", "raw_level_m", "status", "above_range",
               "flood_coverage_pct", "anomaly", "anomaly_reason", "motion_ratio", "confidence",
               "data_valid", "quality", "quality_mode", "lighting", "conditions", "method",
               "scan_level_m", "step_level_m", "resolution_cm_per_px", "marker_mode", "tags_visible",
               "camera_shift_px"]


def git_commit(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", path, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return out.stdout.strip() + ("-modified" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def write_settings_sidecar(path: str, settings: dict, cal: Calibration, extra: dict | None = None) -> str:
    """Everything needed to reproduce a run: program version, git commit, settings, setup."""
    from dataclasses import asdict
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = {
        "written": datetime.now().astimezone().isoformat(timespec="seconds"),
        "program_version": __version__,
        "git_commit": git_commit(project),
        "python": platform.python_version(),
        "opencv": cv2.__version__,
        "numpy": np.__version__,
        "settings": settings,
        "setup": {k: v for k, v in asdict(cal).items() if k != "is_default"},
    }
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return path


class CsvLogger:
    """Appends every reading to a CSV file and flushes it to disk regularly, so a crash or a
    power cut loses at most `flush_s` seconds of data."""

    def __init__(self, path: str, flush_s: float = 1.0):
        self.path = path
        self.flush_s = flush_s
        new = not os.path.isfile(path) or os.path.getsize(path) == 0
        if not new:
            with open(path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
            if header != CSV_COLUMNS:
                base, ext = os.path.splitext(path)
                self.path = f"{base}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
                LOG.warning("%s has different columns (older program version) - logging to %s instead",
                            path, self.path)
                new = True
        self.f = open(self.path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(CSV_COLUMNS)
            self.f.flush()
        self._last_flush = time.monotonic()

    def write(self, payload: dict, meas, res, q, extra: dict | None = None):
        extra = extra or {}
        row = {
            "timestamp": payload["timestamp"], "t_s": payload.get("t_s"),
            "water_level_m": payload["water_level_m"], "raw_level_m": payload["raw_level_m"],
            "status": payload["status"], "above_range": int(bool(payload.get("above_range"))),
            "flood_coverage_pct": payload["flood_coverage_pct"], "anomaly": int(res.anomaly),
            "anomaly_reason": res.reason or "", "motion_ratio": payload["motion_ratio"],
            "confidence": payload["confidence"], "data_valid": int(res.valid), "quality": q.score,
            "quality_mode": q.mode, "lighting": q.lighting, "conditions": "|".join(q.conditions),
            "method": meas.method,
            "scan_level_m": "" if meas.scan_level_m is None else round(meas.scan_level_m, 4),
            "step_level_m": "" if meas.step_level_m is None else round(meas.step_level_m, 4),
            "resolution_cm_per_px": meas.resolution_cm_per_px if meas.resolution_cm_per_px is not None else "",
            "marker_mode": payload.get("marker_mode", ""),
            "tags_visible": extra.get("tags_visible", ""), "camera_shift_px": extra.get("camera_shift_px", ""),
        }
        self.w.writerow([row[c] for c in CSV_COLUMNS])
        now = time.monotonic()
        if now - self._last_flush >= self.flush_s:
            self.flush()

    def flush(self):
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass
        self._last_flush = time.monotonic()

    def close(self):
        if not self.f.closed:
            self.flush()
            self.f.close()
