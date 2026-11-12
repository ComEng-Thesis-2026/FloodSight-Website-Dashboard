"""Video sources: Dahua RTSP / local camera (laptop, Iriun) / video file."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import cv2

LOG = logging.getLogger("FloodMonitoring")


@dataclass
class CameraSettings:
    """Which camera to use. The values are edited in the CAMERA SETTINGS block of FloodMonitoring.py."""
    mode: str = "laptop"                 # laptop / iriun / dahua / file
    laptop_index: int = 0
    iriun_index: int = 1
    device_resolution: tuple | None = None
    dahua_ip: str = "192.168.1.108"
    dahua_user: str = "admin"
    dahua_password: str = ""
    dahua_port: int = 554
    dahua_channel: int = 1
    dahua_subtype: int = 0
    video_file: str = ""
    hfov_deg: float = 70.0


def recording_start(path: str) -> tuple[datetime | None, str]:
    """Wall-clock time of the first frame of a video file, and where it came from.
    1. <video>.json  with "start_time" (written by --record)
    2. scene.json in the same folder whose "video" is this file (synthetic scenes)
    3. file modification time minus the video duration (most cameras / phones)"""
    cands = [path + ".json", os.path.splitext(path)[0] + ".json",
             os.path.join(os.path.dirname(path), "scene.json")]
    for c in cands:
        try:
            with open(c, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        if c.endswith("scene.json") and meta.get("video") != os.path.basename(path):
            continue
        st = meta.get("start_time")
        if st:
            try:
                return datetime.fromisoformat(st), os.path.basename(c)
            except ValueError:
                pass
    try:
        cap = cv2.VideoCapture(path)
        n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        dur = n / fps if fps and n and fps > 0 else 0.0
        end = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).astimezone()
        return end - timedelta(seconds=dur), "file time - duration"
    except OSError:
        return None, "unknown"


def dahua_rtsp_url(ip, user, password, port=554, channel=1, subtype=0) -> str:
    """Dahua RTSP format. subtype 0 = main stream, 1 = sub stream (lighter)."""
    u = urllib.parse.quote(user or "", safe="")
    p = urllib.parse.quote(password or "", safe="")
    auth = f"{u}:{p}@" if user else ""
    return f"rtsp://{auth}{ip}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"


def mask_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(":" + parts.password + "@", ":****@")
            return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    except ValueError:
        pass
    return url


class VideoSource:
    """
    Live sources (RTSP / device) are read in a background thread that keeps only
    the newest frame, so the analysis never lags behind the real scene, and the
    stream reconnects automatically if the CCTV drops.  Files are read
    sequentially with their own timestamps (deterministic, good for testing).
    """

    def __init__(self, spec, kind: str, realtime_file: bool = True, loop_file: bool = False,
                 label: str | None = None, resolution=None):
        self.spec, self.kind = spec, kind
        self.label = label or kind
        self.resolution = resolution
        self.realtime_file, self.loop_file = realtime_file, loop_file
        self.cap = None
        self._frame = None
        self._t = 0.0
        self._seq = 0
        self._last_seq = 0
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self.fps_hint = 15.0
        self._file_idx = 0
        self._wall0 = None
        self._last_frame_mono = time.monotonic()
        self.start_time = None           # files: wall-clock time of the first frame (timestamps)
        self.start_source = None
        if kind == "file":
            self._open()
            if not self.cap or not self.cap.isOpened():
                raise RuntimeError(f"Cannot open video file: {spec}")
        else:
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()

    @property
    def is_live(self) -> bool:
        return self.kind != "file"

    def describe(self) -> str:
        return f"{self.label} ({mask_url(str(self.spec))})"

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        if self.kind == "device":
            idx = int(self.spec)
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
            if not cap.isOpened():
                cap = cv2.VideoCapture(idx)
            if self.resolution:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        elif self.kind in ("dahua", "url"):
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            cap = cv2.VideoCapture(
                str(self.spec), cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000],
            )
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(str(self.spec))
        self.cap = cap
        if cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps and 1 <= fps <= 120:
                self.fps_hint = fps

    def _reader(self):
        backoff = 1.0
        while not self._stop.is_set():
            self._open()
            if not self.cap.isOpened():
                LOG.warning("Cannot open %s - retrying in %.0fs", self.describe(), backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            LOG.info("Connected to %s (%.1f fps)", self.describe(), self.fps_hint)
            backoff = 1.0
            fails = 0
            while not self._stop.is_set():
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 25:
                        LOG.warning("Stream lost (%s) - reconnecting", self.describe())
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                self._last_frame_mono = time.monotonic()
                with self._cond:
                    self._frame, self._t = frame, time.monotonic()
                    self._seq += 1
                    self._cond.notify_all()
        if self.cap:
            self.cap.release()

    def read(self, timeout: float = 2.0):
        """Return (ok, frame, timestamp_seconds, end_of_stream)."""
        if self.is_live:
            with self._cond:
                self._cond.wait_for(lambda: self._seq != self._last_seq or self._stop.is_set(), timeout)
                if self._seq == self._last_seq:
                    return False, None, time.monotonic(), False
                self._last_seq = self._seq
                return True, self._frame.copy(), self._t, False
        ok, frame = self.cap.read()
        if not ok:
            if self.loop_file:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.cap.read()
            if not ok:
                return False, None, 0.0, True
        t = self._file_idx / self.fps_hint
        self._file_idx += 1
        if self.realtime_file:
            if self._wall0 is None:
                self._wall0 = time.monotonic() - t
            delay = self._wall0 + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return True, frame, t, False

    def seconds_since_frame(self) -> float:
        """Live sources: seconds since the camera delivered its last frame."""
        return time.monotonic() - self._last_frame_mono if self.is_live else 0.0

    def release(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if not self.is_live and self.cap:
            self.cap.release()


def _file_source(path, args) -> VideoSource:
    src = VideoSource(path, "file", realtime_file=not args.fast, loop_file=args.loop, label="video file")
    if getattr(args, "file_start", None):
        src.start_time, src.start_source = datetime.fromisoformat(args.file_start), "--file-start"
    else:
        src.start_time, src.start_source = recording_start(path)
    return src


def dahua_url(args, cam: CameraSettings) -> str:
    """RTSP address: command line first, then the DAHUA_PASSWORD variable, then CAMERA SETTINGS."""
    password = args.dahua_password or os.environ.get("DAHUA_PASSWORD") or cam.dahua_password
    return dahua_rtsp_url(args.dahua_ip or cam.dahua_ip, args.dahua_user or cam.dahua_user, password,
                          args.dahua_port or cam.dahua_port, args.dahua_channel or cam.dahua_channel,
                          cam.dahua_subtype if args.dahua_subtype is None else args.dahua_subtype)


def build_source(args, cam: CameraSettings) -> VideoSource:
    """Explicit --device / --source / --dahua-ip win; otherwise --camera, else cam.mode."""
    if args.device is not None:
        return VideoSource(args.device, "device", label=f"camera index {args.device}",
                           resolution=cam.device_resolution)
    if args.source:
        if "://" in args.source:
            return VideoSource(args.source, "url", label="stream")
        return _file_source(args.source, args)
    mode = "dahua" if args.dahua_ip and not args.camera else (args.camera or cam.mode)
    if mode == "laptop":
        return VideoSource(cam.laptop_index, "device", label="laptop camera",
                           resolution=cam.device_resolution)
    if mode == "iriun":
        return VideoSource(cam.iriun_index, "device", label="Iriun phone camera",
                           resolution=cam.device_resolution)
    if mode == "dahua":
        return VideoSource(dahua_url(args, cam), "dahua", label="Dahua CCTV")
    if mode == "file":
        if not cam.video_file:
            raise SystemExit("CAMERA_MODE is 'file' but VIDEO_FILE is empty (or use --source path).")
        return _file_source(cam.video_file, args)
    raise SystemExit(f"Unknown camera mode: {mode}")


def list_cameras(cam: CameraSettings, max_index: int = 5):
    """Print which local camera indexes open, so LAPTOP/IRIUN_CAMERA_INDEX can be set."""
    if os.name == "nt":
        try:
            names = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('Camera','Image') }"
                 " | ForEach-Object { $_.Name }"],
                capture_output=True, text=True, timeout=20).stdout.split("\n")
            names = [n.strip() for n in names if n.strip()]
            print("Cameras installed in Windows:", ", ".join(names) or "none found")
        except (OSError, subprocess.SubprocessError):
            pass
    print("Probing OpenCV camera indexes (the camera light may blink):")
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
        if not cap.isOpened():
            print(f"  index {i}: not available")
            continue
        frame = None
        for _ in range(10):
            ok, f = cap.read()
            if ok:
                frame = f
        cap.release()
        if frame is None:
            print(f"  index {i}: opens but gives no frames")
        else:
            h, w = frame.shape[:2]
            note = "  <- black image (Iriun not connected?)" if frame.mean() < 3 else ""
            print(f"  index {i}: {w}x{h}{note}")
    print(f"Current settings: LAPTOP_CAMERA_INDEX = {cam.laptop_index}, "
          f"IRIUN_CAMERA_INDEX = {cam.iriun_index}, CAMERA_MODE = '{cam.mode}'")
