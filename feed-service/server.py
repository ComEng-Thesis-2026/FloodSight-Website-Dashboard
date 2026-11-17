#!/usr/bin/env python3
"""
FloodSight feed service - the OpenCV flood monitoring program behind the dashboard's video feeds.

Plays each video in feeds.json in a loop, runs the floodmon pipeline from
OpenCV-FloodMonitoring-main on every frame, streams the clean and the annotated
video (MJPEG), accepts ROI / marker changes from the Camera Config page,
writes every reading to MongoDB and saves measurement captures (snapshot locally,
metrics in MongoDB; see metrics_capture.py).

    npm run feeds                                   (from the project root)
    .venv\\Scripts\\python.exe feed-service/server.py [--port 8765] [--no-db] [--fast]
                                                    [--no-captures] [--capture-interval 10]

Endpoints (local only; the dashboard proxies them as /api/feeds/...):
    GET  /feeds                              feeds, state, latest reading, MongoDB status
    GET  /feeds/<id>                         one feed
    GET  /feeds/<id>/reading                 latest reading (payload)
    GET  /feeds/<id>/stream?overlay=1|0      MJPEG stream, with or without the OpenCV overlay
    GET  /feeds/<id>/frame.jpg?overlay=0|1   single frame
    GET  /feeds/<id>/setup                   ROIs, reference height, marker mode, references
    PUT  /feeds/<id>/setup                   {flood_roi?, marker_roi?, ref_height_m?}
    POST /feeds/<id>/actions                 {action: capture_reference | virtual_marker |
                                              virtual_unlock | save | restart | pause | resume}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SERVICE_DIR)
OPENCV_DIR = os.path.join(PROJECT_DIR, "OpenCV-FloodMonitoring-main")
sys.path.insert(0, OPENCV_DIR)
sys.path.insert(0, SERVICE_DIR)

from metrics_capture import CAPTURE_INTERVAL_S, MetricsRecorder  # noqa: E402
from mongo_sink import MongoSink  # noqa: E402
from worker import ACTIONS, FeedConfig, FeedWorker, SetupValidationError, validate_setup_patch  # noqa: E402

LOG = logging.getLogger("feed-service")
MAX_BODY_BYTES = 1_000_000
ROUTE = re.compile(r"^/feeds(?:/(?P<id>[A-Za-z0-9_-]+)(?:/(?P<sub>[A-Za-z0-9_.-]+))?)?/?$")


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """HTTPServer sets SO_REUSEADDR, which on Windows lets a second copy of the service
    bind the same port while the first is still running (requests then reach the old one).
    Take the port exclusively instead, so a duplicate fails to start."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def load_feeds(path: str) -> list[FeedConfig]:
    with open(path, "r", encoding="utf-8") as f:
        items = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    feeds = []
    for item in items:
        coords = item.get("coordinates") or {}
        video = item["video"]
        setup_dir = item.get("setup_dir") or os.path.join("setups", item["id"])
        feeds.append(FeedConfig(
            id=item["id"],
            location=item.get("location") or item["id"],
            video=video if os.path.isabs(video) else os.path.join(base, video),
            setup_dir=setup_dir if os.path.isabs(setup_dir) else os.path.join(base, setup_dir),
            lat=coords.get("lat"),
            lng=coords.get("lng"),
            max_width=int(item.get("max_width", 960)),
            stream_fps=float(item.get("stream_fps", 12.0)),
            jpeg_quality=int(item.get("jpeg_quality", 75)),
        ))
    ids = [f.id for f in feeds]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"{path}: feed ids must be unique ({ids})")
    return feeds


class FeedService:
    def __init__(self, workers: dict[str, FeedWorker], sink: MongoSink | None,
                 recorder: MetricsRecorder | None = None):
        self.workers = workers
        self.sink = sink
        self.recorder = recorder

    def db_status(self) -> dict:
        status = self.sink.describe() if self.sink is not None else {"status": "disabled"}
        status["metrics"] = self.recorder.describe() if self.recorder is not None else {"status": "disabled"}
        return status


def make_handler(service: FeedService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FloodSightFeeds/1.0"

        def log_message(self, fmt, *args):            # quiet: streams would flood the log
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        # ---- helpers ----
        def _json(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str, **extra) -> None:
            self._json(status, {"error": message, **extra})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _route(self):
            url = urlparse(self.path)
            m = ROUTE.match(url.path)
            if not m:
                return None, None, None, url
            worker = None
            if m.group("id"):
                worker = service.workers.get(m.group("id"))
            return m, worker, m.group("sub"), url

        def _overlay(self, url) -> bool:
            value = (parse_qs(url.query).get("overlay") or ["1"])[0]
            return value not in ("0", "false", "off")

        # ---- GET ----
        def do_GET(self):
            m, worker, sub, url = self._route()
            if m is None:
                return self._error(404, "Not found")
            if not m.group("id"):
                return self._json(200, {"feeds": [w.snapshot() for w in service.workers.values()],
                                        "db": service.db_status()})
            if worker is None:
                return self._error(404, f"Unknown feed: {m.group('id')}")
            if sub is None:
                return self._json(200, dict(worker.snapshot(), db=service.db_status()))
            if sub == "reading":
                return self._json(200, {"id": worker.cfg.id, "state": worker.state, "message": worker.message,
                                        "reading": worker.reading(), "setup": worker.setup()})
            if sub == "setup":
                setup = worker.setup()
                if setup is None:
                    return self._error(503, f"The feed is not running ({worker.state}).", state=worker.state)
                return self._json(200, setup)
            if sub == "frame.jpg":
                jpeg = worker.latest_jpeg(self._overlay(url))
                if not jpeg:
                    return self._error(503, "No frame yet.")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)
                return None
            if sub == "stream":
                return self._stream(worker, self._overlay(url))
            return self._error(404, "Not found")

        def _stream(self, worker: FeedWorker, overlay: bool):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            try:
                while not getattr(self.server, "stopping", False):
                    jpeg, seq = worker.wait_jpeg(overlay, seq, timeout=5.0)
                    if not jpeg:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass                                       # viewer closed the page
            self.close_connection = True

        # ---- PUT / POST ----
        def do_PUT(self):
            m, worker, sub, _ = self._route()
            if m is None or worker is None or sub != "setup":
                return self._error(404, "Not found")
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._error(400, f"Invalid JSON: {e}")
            setup = worker.setup()
            if setup is None:
                return self._error(503, f"The feed is not running ({worker.state}).", state=worker.state)
            try:
                patch = validate_setup_patch(body, setup.get("frame_size"), bool(setup.get("marker_locked")))
            except SetupValidationError as e:
                return self._error(400, "Invalid setup", problems=e.problems)
            result = worker.submit("set_setup", patch)
            return self._json(200 if result.get("ok") else 409, result)

        def do_POST(self):
            m, worker, sub, _ = self._route()
            if m is None or worker is None or sub != "actions":
                return self._error(404, "Not found")
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._error(400, f"Invalid JSON: {e}")
            action = body.get("action") if isinstance(body, dict) else None
            if action not in ACTIONS:
                return self._error(400, f"action must be one of: {', '.join(ACTIONS)}")
            result = worker.submit(action)
            # a refused capture (road wet, motion) is a normal answer, not a server error
            return self._json(200 if result.get("ok") or result.get("needs_confirm") else 409, result)

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="FloodSight OpenCV video feed service")
    ap.add_argument("--feeds", default=os.path.join(SERVICE_DIR, "feeds.json"))
    ap.add_argument("--host", default=os.environ.get("FEED_SERVICE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FEED_SERVICE_PORT") or 8765))
    ap.add_argument("--no-db", action="store_true", help="do not write to MongoDB")
    ap.add_argument("--fast", action="store_true", help="process the videos as fast as possible (testing)")
    ap.add_argument("--no-captures", action="store_true",
                    help="do not save measurement captures (snapshot + metrics)")
    ap.add_argument("--capture-interval", type=float, default=CAPTURE_INTERVAL_S,
                    help=f"seconds of video between periodic captures (default {CAPTURE_INTERVAL_S:g}); "
                         "events are always captured")
    ap.add_argument("--captures-dir", default=os.path.join(SERVICE_DIR, "captures"),
                    help="where the capture snapshots, JSON files and summary.csv are saved")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    except ImportError:
        LOG.warning("python-dotenv is not installed - using the process environment only")

    feeds = load_feeds(args.feeds)
    sink = None
    if not args.no_db:
        sink = MongoSink(os.environ.get("MONGODB_URI"),
                         os.environ.get("MONGODB_DB") or "floodsight",
                         os.environ.get("MONGODB_COLLECTION") or "cameras",
                         os.environ.get("MONGODB_READINGS_COLLECTION") or "flood_readings",
                         feeds=feeds)
        sink.start()
    recorder = None
    if not args.no_captures:
        recorder = MetricsRecorder(os.path.abspath(args.captures_dir), PROJECT_DIR,
                                   uri=None if args.no_db else os.environ.get("MONGODB_URI"),
                                   db_name=os.environ.get("MONGODB_DB") or "floodsight",
                                   collection=os.environ.get("MONGODB_METRICS_COLLECTION") or "camera_metrics")
        recorder.start()
        LOG.info("Measurement captures: on events and every %g s -> %s", args.capture_interval,
                 os.path.relpath(recorder.captures_dir, PROJECT_DIR))
    workers = {f.id: FeedWorker(f, sink, realtime=not args.fast, recorder=recorder,
                                capture_interval_s=args.capture_interval) for f in feeds}

    # bind before the feeds start, so a second copy of the service fails right away
    try:
        httpd = ExclusiveHTTPServer((args.host, args.port), make_handler(FeedService(workers, sink, recorder)))
    except OSError as e:
        LOG.error("Cannot use port %d (%s). Another feed service is probably still running - "
                  "stop it first (Ctrl+C in its terminal, or Task Manager > python.exe).", args.port, e)
        if recorder is not None:
            recorder.stop()
        sys.exit(1)
    for w in workers.values():
        w.start()
    httpd.daemon_threads = True
    httpd.stopping = False
    LOG.info("Feed service on http://%s:%d/feeds (%s)", args.host, args.port,
             ", ".join(f"{f.id}: {os.path.relpath(f.video, SERVICE_DIR)}" for f in feeds))

    def shutdown(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Stopping ...")
    finally:
        httpd.stopping = True
        for w in workers.values():
            w.stop()
        if sink is not None:
            sink.stop()
        if recorder is not None:
            recorder.stop()
            recorder.join(timeout=5.0)                 # finish the captures still queued
        httpd.server_close()


if __name__ == "__main__":
    main()
