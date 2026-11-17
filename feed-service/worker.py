"""
One video feed: the floodmon pipeline running headless on a looping clip.

The worker is a copy of the start-up and key-handling glue of
OpenCV-FloodMonitoring-main/floodmon/app.py (which keeps that glue inside main()).
The measuring itself is floodmon's Monitor, unchanged, so the dashboard feed
measures exactly like the stand-alone program.

Thread model: the HTTP threads never touch Monitor or Calibration. They queue a
Command and wait for the worker to apply it between two frames.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from floodmon.bands import STATUS_ORDER
from floodmon.calibration import Calibration, SetupError, normalize_polygon
from floodmon.config import parse_args
from floodmon.modes import ReferenceGuard
from floodmon.pipeline import Monitor
from floodmon.quality import SMALL, classify_lighting, enhance_for_display
from floodmon.ui import MARKER_COLOR, draw_overlay
from metrics_capture import CAPTURE_INTERVAL_S, CaptureJob, CaptureScheduler

LOG = logging.getLogger("feed-service")

ACTIONS = ("capture_reference", "virtual_marker", "virtual_unlock", "save", "restart", "pause", "resume")
COMMAND_TIMEOUT_S = 3.0
MISSING_VIDEO_RETRY_S = 10.0


@dataclass
class FeedConfig:
    id: str
    location: str
    video: str                      # absolute path of the clip
    setup_dir: str                  # flood_config.json + dry references for this feed
    lat: float | None = None
    lng: float | None = None
    max_width: int = 960
    stream_fps: float = 12.0
    jpeg_quality: int = 75

    @property
    def config_path(self) -> str:
        return os.path.join(self.setup_dir, "flood_config.json")


@dataclass
class Command:
    kind: str
    data: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class SetupValidationError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v)


def validate_setup_patch(body, frame_size, marker_locked: bool) -> dict:
    """Check a setup change from the website; return the cleaned values or raise SetupValidationError."""
    if not isinstance(body, dict):
        raise SetupValidationError(["the request body must be a JSON object"])
    problems: list[str] = []
    out: dict = {}
    w, h = frame_size if frame_size else (None, None)

    def points(name, n_min, n_exact=None):
        try:
            pts = normalize_polygon(body[name], as_int=False)
        except (ValueError, TypeError, IndexError, KeyError):
            problems.append(f"{name}: not a list of [x, y] points")
            return None
        if not pts or any(len(p) != 2 or not (_is_num(p[0]) and _is_num(p[1])) for p in pts):
            problems.append(f"{name}: every point needs two numbers [x, y]")
            return None
        if n_exact is not None and len(pts) != n_exact:
            problems.append(f"{name}: needs exactly {n_exact} corners (found {len(pts)})")
            return None
        if len(pts) < n_min:
            problems.append(f"{name}: needs at least {n_min} points (found {len(pts)})")
            return None
        if w and h and any(not (-1 <= x <= w + 1 and -1 <= y <= h + 1) for x, y in pts):
            problems.append(f"{name}: every point must lie inside the {w}x{h} frame")
            return None
        return pts

    if "flood_roi" in body:
        pts = points("flood_roi", 3)
        if pts:
            out["flood_roi"] = [[int(round(x)), int(round(y))] for x, y in pts]
    if "marker_roi" in body:
        if marker_locked:
            problems.append("marker_roi: the marker is locked (virtual marker) - unlock it first")
        else:
            pts = points("marker_roi", 4, n_exact=4)
            if pts:
                out["marker_roi"] = [[round(float(x), 2), round(float(y), 2)] for x, y in pts]
    if "ref_height_m" in body:
        rh = body["ref_height_m"]
        if marker_locked:
            problems.append("ref_height_m: the marker is locked (virtual marker) - unlock it first")
        elif not (_is_num(rh) and 0.05 <= rh <= 5.0):
            problems.append(f"ref_height_m: must be a number between 0.05 and 5 m (found {rh!r})")
        else:
            out["ref_height_m"] = round(float(rh), 3)
    if not out and not problems:
        problems.append("nothing to change: send flood_roi, marker_roi or ref_height_m")
    if problems:
        raise SetupValidationError(problems)
    return out


class LoopingClip:
    """
    A video file played in real time and looped forever.

    Time keeps increasing across loops (the Kalman filter needs a monotonic clock).
    When processing falls behind the clip's own clock, frames are skipped with
    grab() so the feed stays real-time instead of slowing down.

    Pausing freezes the clip position but not the clock: held_t keeps running so the
    worker can keep measuring the frozen frame (e.g. to capture a dry reference from
    it), and on resume the clip continues from where it stopped without skipping ahead.
    """

    def __init__(self, path: str, realtime: bool = True):
        self.path = path
        self.realtime = realtime
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open video file: {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and 1 <= fps <= 120 else 15.0
        frames = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self.duration_s = frames / self.fps if frames and frames > 0 else None
        self.index = 0                  # frames delivered or skipped since start (all loops)
        self.loop_start_index = 0
        self.skipped = 0
        self._wall0 = None
        self.wrapped = False            # set when the last read started a new loop
        self.last_read_ms = 0.0         # decode time of the last delivered frame (no pacing sleep)
        self.paused = False
        self._paused_at = None          # wall clock of the pause (or of the last step while paused)
        self._t_offset = 0.0            # time spent paused, added to the clip clock
        self._last_t = 0.0              # t of the last delivered frame

    @property
    def video_t_s(self) -> float:
        """Position inside the current loop of the clip."""
        return (self.index - self.loop_start_index) / self.fps

    @property
    def held_t(self) -> float:
        """Clock for re-measuring the last frame while paused (keeps increasing)."""
        if not self.paused:
            return self._last_t
        return self._last_t + (time.monotonic() - self._paused_at)

    def _seek_start(self):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self.loop_start_index = self.index

    def restart(self):
        self._seek_start()

    def pause(self):
        if not self.paused:
            self.paused = True
            self._paused_at = time.monotonic()

    def resume(self):
        if not self.paused:
            return
        t = self.held_t
        self.paused, self._paused_at = False, None
        # continue just after the held clock, and pace from now on (no catch-up skipping)
        self._t_offset = t + 1.0 / self.fps - self.index / self.fps
        self._wall0 = time.monotonic() - self.index / self.fps

    def read(self):
        """Return (frame, t) or (None, t) if the file cannot be read at all.
        While paused this is a single step (e.g. after restart): no pacing, no skipping."""
        self.wrapped = False
        if self.realtime and not self.paused:
            if self._wall0 is None:
                self._wall0 = time.monotonic() - self.index / self.fps
            behind = int((time.monotonic() - self._wall0) * self.fps) - self.index
            for _ in range(min(max(behind - 1, 0), int(self.fps))):     # skip at most 1 s per read
                if not self.cap.grab():
                    break
                self.index += 1
                self.skipped += 1
        t_read = time.perf_counter()
        ok, frame = self.cap.read()
        if not ok:
            self._seek_start()
            self.wrapped = True
            ok, frame = self.cap.read()
            if not ok:
                return None, self.index / self.fps + self._t_offset
        self.last_read_ms = (time.perf_counter() - t_read) * 1000.0
        pos = self.index / self.fps
        if self.paused:                     # stepping while paused: keep the held clock monotonic
            now = time.monotonic()
            t = self.held_t
            self._paused_at = now
            self._t_offset = t - pos
        else:
            t = pos + self._t_offset
        self._last_t = t
        self.index += 1
        if self.realtime and not self.paused:
            delay = self._wall0 + pos - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return frame, t

    def release(self):
        self.cap.release()


def placeholder_jpeg(text: str, sub: str = "", size=(960, 540), quality: int = 75) -> bytes:
    img = np.full((size[1], size[0], 3), 28, np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for i, (line, scale, color) in enumerate(((text, 0.9, (235, 235, 235)), (sub, 0.55, (170, 170, 170)))):
        if not line:
            continue
        (tw, th), _ = cv2.getTextSize(line, font, scale, 2)
        cv2.putText(img, line, ((size[0] - tw) // 2, size[1] // 2 + i * 40), font, scale, color, 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def install_stage_timers(mon, stage: dict):
    """Time water detection and the Kalman step inside Monitor.process without changing floodmon:
    this feed's own detector and filter instances get timing wrappers (Monitor calls them as
    self.det.process(...) and self.kalman.step(...), and never replaces either object)."""
    det_process, kalman_step = mon.det.process, mon.kalman.step

    def timed_process(*a, **kw):
        t0 = time.perf_counter()
        try:
            return det_process(*a, **kw)
        finally:
            stage["thsv_classification_ms"] = (time.perf_counter() - t0) * 1000.0

    def timed_step(*a, **kw):
        t0 = time.perf_counter()
        try:
            return kalman_step(*a, **kw)
        finally:
            stage["tkalman_ms"] = (time.perf_counter() - t0) * 1000.0

    mon.det.process, mon.kalman.step = timed_process, timed_step


class FeedWorker(threading.Thread):
    def __init__(self, cfg: FeedConfig, sink=None, realtime: bool = True, recorder=None,
                 capture_interval_s: float = CAPTURE_INTERVAL_S):
        super().__init__(daemon=True, name=f"feed-{cfg.id}")
        self.cfg = cfg
        self.sink = sink
        self.recorder = recorder         # MetricsRecorder: snapshot + metrics captures (None = off)
        self.capture_interval_s = capture_interval_s
        self.realtime = realtime
        self.state = "starting"          # starting | running | missing_video | error | stopped
        self.message: str | None = None
        self.loop_index = 0
        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop_event = threading.Event()
        self._cond = threading.Condition()
        self._seq = 0
        self._jpeg: dict[bool, bytes | None] = {False: None, True: None}
        self._payload: dict | None = None
        self._setup: dict | None = None
        self._ctx: dict | None = None    # live objects, used only by the worker thread

    # ---- public, thread-safe -------------------------------------------
    def stop(self):
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "id": self.cfg.id,
                "location": self.cfg.location,
                "state": self.state,
                "message": self.message,
                "video": os.path.basename(self.cfg.video),
                "coordinates": {"lat": self.cfg.lat, "lng": self.cfg.lng},
                "loop_index": self.loop_index,
                "setup": self._setup,
                "reading": self._payload,
            }

    def reading(self) -> dict | None:
        with self._cond:
            return self._payload

    def setup(self) -> dict | None:
        with self._cond:
            return self._setup

    def latest_jpeg(self, overlay: bool) -> bytes | None:
        with self._cond:
            return self._jpeg[overlay]

    def wait_jpeg(self, overlay: bool, last_seq: int, timeout: float = 5.0) -> tuple[bytes | None, int]:
        """Block until a newer frame than last_seq is published (MJPEG streaming)."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq or self._stop_event.is_set(), timeout)
            return self._jpeg[overlay], self._seq

    def submit(self, kind: str, data: dict | None = None, timeout: float = COMMAND_TIMEOUT_S) -> dict:
        if self.state != "running":
            return {"ok": False, "message": f"The feed is not running ({self.state}"
                                            + (f": {self.message}" if self.message else "") + ")."}
        cmd = Command(kind, data or {})
        self._commands.put(cmd)
        if not cmd.done.wait(timeout):
            return {"ok": False, "message": "The feed did not answer in time - try again."}
        return cmd.result or {"ok": False, "message": "No result."}

    # ---- worker thread ---------------------------------------------------
    def run(self):
        while not self._stop_event.is_set():
            if not os.path.isfile(self.cfg.video):
                # placeholder first: a viewer that sees the new state always gets a frame too
                self._publish_placeholder("Video not found", os.path.basename(self.cfg.video))
                self._set_state("missing_video", f"video file {os.path.basename(self.cfg.video)} not found "
                                                 "in feed-service/videos")
                self._fail_pending()
                self._stop_event.wait(MISSING_VIDEO_RETRY_S)
                continue
            try:
                self._run_video()
            except SetupError as e:
                LOG.error("[%s] %s", self.cfg.id, e)
                self._publish_placeholder("Setup file error", "see the feed-service log")
                self._set_state("error", str(e))
                self._stop_event.wait(MISSING_VIDEO_RETRY_S)
            except Exception as e:                       # keep the service alive, retry
                LOG.exception("[%s] feed crashed", self.cfg.id)
                self._publish_placeholder("Feed error", str(e)[:80])
                self._set_state("error", str(e))
                self._fail_pending()
                self._stop_event.wait(5.0)
        self._set_state("stopped", None)

    def _set_state(self, state: str, message: str | None):
        with self._cond:
            self.state, self.message = state, message
        if self.sink is not None:
            self.sink.note_state(self.cfg, state, message)

    def _publish_placeholder(self, text: str, sub: str):
        jpeg = placeholder_jpeg(text, sub, quality=self.cfg.jpeg_quality)
        with self._cond:
            self._jpeg = {False: jpeg, True: jpeg}
            self._seq += 1
            self._cond.notify_all()

    def _fail_pending(self):
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            cmd.result = {"ok": False, "message": f"The feed is not running ({self.state})."}
            cmd.done.set()

    def _resize(self, frame):
        mw = self.cfg.max_width
        if mw and frame.shape[1] > mw:
            sc = mw / frame.shape[1]
            frame = cv2.resize(frame, (mw, int(round(frame.shape[0] * sc))), interpolation=cv2.INTER_AREA)
        return frame

    def _run_video(self):
        cfg = self.cfg
        os.makedirs(cfg.setup_dir, exist_ok=True)
        args = parse_args(["--no-display", "--config", cfg.config_path, "--camera-id", cfg.id,
                           "--max-width", str(cfg.max_width)])
        cal = Calibration.load(cfg.config_path)
        cal.camera_id = cfg.id
        clip = LoopingClip(cfg.video, realtime=self.realtime)
        try:
            frame, t = clip.read()
            if frame is None:
                raise RuntimeError(f"no frames in {cfg.video}")
            frame = self._resize(frame)
            H, W = frame.shape[:2]
            cal = cal.scaled_to(W, H)
            if cal.fill_defaults(W, H):
                LOG.warning("[%s] no saved ROIs - default ROIs created; place them on the Camera Config page",
                            cfg.id)

            def save_setup():
                cal.frame_size = [W, H]
                cal.lens_applied = False
                cal.save(cfg.config_path)

            mon = Monitor(args, cal, frame.shape, source_kind="file", start_time=None, save_setup=save_setup)
            stage: dict = {}
            install_stage_timers(mon, stage)
            det = mon.det
            for name in dict.fromkeys(list((cal.reference_images or {}).values())
                                      + ([cal.reference_image] if cal.reference_image else [])):
                path = name if os.path.isabs(name) else os.path.join(cfg.setup_dir, name)
                img = cv2.imread(path)
                if img is None:
                    LOG.warning("[%s] dry reference image not found: %s", cfg.id, path)
                    continue
                if img.shape[:2] != (H, W):
                    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
                LOG.info("[%s] dry reference loaded for %s lighting: %s", cfg.id, det.add_reference(img), name)
            det._activate(classify_lighting(cv2.resize(frame, SMALL, interpolation=cv2.INTER_AREA),
                                            args.night_brightness))

            def reference_captured(light, bgr, replaced=False):
                tag = "_virtual" if (replaced or cal.virtual_marker) else ""
                name = f"reference_dry_{cal.camera_id}_{light}{tag}.png"
                cv2.imwrite(os.path.join(cfg.setup_dir, name), bgr)
                if replaced and mon.vmark.state == "capturing":
                    mon.vmark.finish(light, name)            # virtual marker conversion done (saves)
                    return
                cal.reference_images = dict(cal.reference_images or {}, **{light: name})
                if not cal.is_default:
                    save_setup()
                LOG.info("[%s] saved %s dry reference: %s", cfg.id, light, name)
            det.on_reference_captured = reference_captured

            ctx = dict(args=args, cal=cal, mon=mon, clip=clip, save_setup=save_setup, W=W, H=H,
                       guard=ReferenceGuard(wet_coverage=args.wet_coverage, wet_level=args.wet_level),
                       view_frame=frame, meas=None, q=det.quality, t=t, stage=stage,
                       captures=CaptureScheduler(self.capture_interval_s))
            self._ctx = ctx
            self._set_state("running", None)
            LOG.info("[%s] playing %s (%dx%d, %.1f fps, %s)", cfg.id, os.path.basename(cfg.video), W, H,
                     clip.fps, f"{clip.duration_s:.0f} s" if clip.duration_s else "unknown length")
            self._loop(ctx, frame, t)
        finally:
            self._ctx = None
            clip.release()
            self._fail_pending()

    def _loop(self, ctx, frame, t):
        cfg, mon, cal, clip = self.cfg, ctx["mon"], ctx["cal"], ctx["clip"]
        H, W = ctx["H"], ctx["W"]
        fps, t_fps, n_fps = 0.0, time.monotonic(), 0
        last_pub = 0.0
        pub_interval = 1.0 / max(cfg.stream_fps, 1.0)
        stage = ctx["stage"]
        capture_ms = clip.last_read_ms
        held = frame                                    # last clip frame, re-measured while paused
        while not self._stop_event.is_set():
            self._apply_commands(ctx)
            if frame is None and clip.paused and not ctx.pop("step", False):
                # paused: keep measuring the frozen frame at the clip's frame rate, so a dry
                # reference capture (which averages several frames) completes on it
                self._stop_event.wait(1.0 / clip.fps)
                frame, t, capture_ms = held, clip.held_t, 0.0
            elif frame is None:
                frame, t = clip.read()
                if frame is None:
                    raise RuntimeError("the video stopped delivering frames")
                if clip.wrapped:
                    self._new_loop(ctx)
                t_resize = time.perf_counter()
                frame = self._resize(frame)
                if frame.shape[:2] != (H, W):
                    frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
                capture_ms = clip.last_read_ms + (time.perf_counter() - t_resize) * 1000.0
                held = frame
            if clip.paused:
                # a deliberately paused clip is not a frozen camera: without this, floodmon flags
                # FROZEN after --frozen-s, marks the data unreliable and blocks a dry reference capture
                mon.det.qm.frozen_since = None
            stage["thsv_classification_ms"] = stage["tkalman_ms"] = 0.0
            t0 = time.perf_counter()
            r = mon.process(frame, t, measuring=True)
            processing_ms = (time.perf_counter() - t0) * 1000.0
            ctx.update(view_frame=r.frame, meas=r.meas, q=r.quality, t=t)

            n_fps += 1
            if time.monotonic() - t_fps >= 1.0:
                fps = n_fps / (time.monotonic() - t_fps)
                t_fps, n_fps = time.monotonic(), 0
            mon.fps = fps
            payload = r.payload
            payload.update({
                "fps": round(fps, 1),
                "feed_id": cfg.id,
                "loop_index": self.loop_index,
                "video_t_s": round(clip.video_t_s, 2),
                "paused": clip.paused,
                "processing_ms": round(processing_ms, 1),
                "frames_skipped": clip.skipped,
            })
            setup = self._setup_snapshot(ctx)
            now = time.monotonic()
            jpegs, encode_ms = None, 0.0
            if now - last_pub >= pub_interval:
                last_pub = now
                t_enc = time.perf_counter()
                jpegs = self._encode(frame, r, ctx, fps)
                encode_ms = (time.perf_counter() - t_enc) * 1000.0
            if self.recorder is not None and not clip.paused:   # a frozen frame is not a new sample
                due = ctx["captures"].check(payload, setup, t)
                if due is not None:
                    if jpegs is None:                       # this exact frame must be the snapshot
                        t_enc = time.perf_counter()
                        jpegs = self._encode(frame, r, ctx, fps)
                        encode_ms = (time.perf_counter() - t_enc) * 1000.0
                    self.recorder.submit(CaptureJob(
                        feed=cfg, payload=dict(payload), setup=setup, trigger=due[0], event=due[1],
                        clean_jpeg=jpegs[False], processed_jpeg=jpegs[True],
                        latency={"tcapture_ms": round(capture_ms, 3),
                                 "tprocessing_ms": round(processing_ms, 3),
                                 "thsv_classification_ms": round(stage["thsv_classification_ms"], 3),
                                 "tkalman_ms": round(stage["tkalman_ms"], 3),
                                 "tsnapshot_ms": round(encode_ms, 3)},
                        t_frame_ready=t0))
            with self._cond:
                self._payload, self._setup = payload, setup
                if jpegs is not None:
                    self._jpeg = jpegs
                    self._seq += 1
                    self._cond.notify_all()
            if self.sink is not None:
                self.sink.submit(cfg, payload, setup)
            frame = None

    def _encode(self, raw, r, ctx, fps) -> dict[bool, bytes | None]:
        cal, mon = ctx["cal"], ctx["mon"]
        if mon.vmark.status_text():
            hint = mon.vmark.status_text()
        elif cal.is_default:
            hint = "SETUP NEEDED - place the ROIs on the Camera Config page"
        else:
            hint = None
        params = [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality]
        out: dict[bool, bytes | None] = {}
        ok, buf = cv2.imencode(".jpg", raw, params)          # clean view first: drawing is in place
        out[False] = buf.tobytes() if ok else None
        # enhance_for_display may hand back the very same array (and r.frame can be the raw
        # frame), so draw on a copy or the clean view and the next measurement get the overlay
        base = enhance_for_display(r.frame, r.quality).copy()
        # hard failures (no signal, frozen video) give a measurement without masks: no water tint
        show_water = r.meas is None or getattr(r.meas, "water_mask", None) is not None
        annotated = draw_overlay(base, mon.det, cal, r.res, r.meas, r.status, fps, hint, show_water,
                                 show_handles=False,
                                 extra_lines=[(f"MARKER: {cal.marker_mode.upper()}", MARKER_COLOR)])
        ok, buf = cv2.imencode(".jpg", annotated, params)
        out[True] = buf.tobytes() if ok else None
        return out

    def _setup_snapshot(self, ctx) -> dict:
        cal, mon = ctx["cal"], ctx["mon"]
        refs = sorted((cal.reference_images or {}).keys()) + (["legacy"] if cal.reference_image else [])
        return {
            "frame_size": [ctx["W"], ctx["H"]],
            "flood_roi": cal.flood_roi,
            "marker_roi": cal.marker_roi,
            "ref_height_m": cal.ref_height_m,
            "marker_mode": cal.marker_mode,
            "marker_locked": cal.marker_locked,
            "virtual_marker": cal.virtual_marker,
            "references": refs,
            "calibrated": bool(cal.ready and not cal.is_default),
            "capturing": mon.det.capture is not None,
            "conversion_hint": mon.vmark.status_text(),
            "paused": ctx["clip"].paused,
        }

    def _new_loop(self, ctx):
        """The clip started again from its dry scene: start the filter and status fresh."""
        mon = ctx["mon"]
        mon.kalman.reset()
        mon.tracker.status = STATUS_ORDER[0]
        with self._cond:
            self.loop_index += 1
        LOG.info("[%s] video loop %d", self.cfg.id, self.loop_index)

    # ---- commands (applied between frames, on the worker thread) --------
    def _apply_commands(self, ctx):
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                cmd.result = self._apply(ctx, cmd)
            except Exception as e:                       # never let a bad command kill the feed
                LOG.exception("[%s] command %s failed", self.cfg.id, cmd.kind)
                cmd.result = {"ok": False, "message": f"{cmd.kind} failed: {e}"}
            cmd.done.set()

    def _apply(self, ctx, cmd: Command) -> dict:
        cal, mon, clip = ctx["cal"], ctx["mon"], ctx["clip"]
        kind, data = cmd.kind, cmd.data
        if kind == "set_setup":
            if cal.marker_locked and ("marker_roi" in data or "ref_height_m" in data):
                return {"ok": False, "message": "The marker is locked (virtual marker) - unlock it first."}
            for key, value in data.items():
                setattr(cal, key, value)
            cal.frame_size = [ctx["W"], ctx["H"]]
            mon.set_calibration(ctx["view_frame"].shape)
            ctx["save_setup"]()
            return {"ok": True, "message": "Setup applied and saved. Capture a dry reference if the view changed.",
                    "setup": self._setup_snapshot(ctx)}
        if kind == "capture_reference":
            q = ctx["q"]
            go, msg = ctx["guard"].press(time.monotonic(), ctx["t"], ctx["meas"], q, mon.last_motion_t)
            if go:
                mon.record_anchors(ctx["view_frame"])
                mon.det.start_capture(q.lighting, reason="manual")
                mon.kalman.reset()
            return {"ok": go, "message": msg, "needs_confirm": not go}
        if kind == "virtual_marker":
            msg = mon.vmark.press(ctx["t"], ctx["meas"], ctx["q"])
            ok = not msg.startswith(("Cannot", "Fit the marker", "The virtual marker is already"))
            return {"ok": ok, "message": msg}
        if kind == "virtual_unlock":
            msg = mon.vmark.unlock()
            return {"ok": not msg.startswith("The marker is not locked"), "message": msg}
        if kind == "save":
            mon.record_anchors(ctx["view_frame"])
            ctx["save_setup"]()
            return {"ok": True, "message": "Setup saved."}
        if kind == "restart":
            clip.restart()
            self._new_loop(ctx)
            if clip.paused:
                ctx["step"] = True                   # show the first (dry) frame, stay paused
                return {"ok": True, "message": "Video restarted - paused on the first frame (dry scene)."}
            return {"ok": True, "message": "Video restarted from the beginning (dry scene)."}
        if kind == "pause":
            clip.pause()
            return {"ok": True, "message": "Video paused - the frozen frame is still measured, "
                                           "so you can capture the dry reference from it."}
        if kind == "resume":
            clip.resume()
            return {"ok": True, "message": "Video playing."}
        return {"ok": False, "message": f"Unknown action: {kind}"}
