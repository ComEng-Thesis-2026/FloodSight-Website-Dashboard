"""
Tests for the feed service (worker, setup validation, MongoDB documents, HTTP).

    .venv\\Scripts\\python.exe -m unittest discover -s feed-service/tests -v

A short synthetic flood clip from floodmon.synthetic is rendered once;
no MongoDB connection is needed.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from types import SimpleNamespace

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(SERVICE_DIR), "OpenCV-FloodMonitoring-main"))
sys.path.insert(0, SERVICE_DIR)

from floodmon.synthetic import preset, write_scene  # noqa: E402
from mongo_sink import (EventTracker, build_camera_update, build_reading_doc,  # noqa: E402
                        camera_doc_id, map_status)
from worker import FeedConfig, FeedWorker, SetupValidationError, validate_setup_patch  # noqa: E402

_SCENE = {}


def scene_dir() -> str:
    if "dir" not in _SCENE:
        tmp = tempfile.mkdtemp(prefix="feed-test-scene-")
        cfg = preset("quick")          # 30 s: dry 6 s, rise to 0.60 m (the floodmon unit-test scene)
        write_scene(cfg, tmp)
        _SCENE["dir"] = tmp
    return _SCENE["dir"]


def tearDownModule():
    if "dir" in _SCENE:
        shutil.rmtree(_SCENE["dir"], ignore_errors=True)


class FakeSink:
    def __init__(self):
        self.payloads = []
        self.states = []

    def submit(self, feed, payload, setup):
        self.payloads.append((payload, setup))

    def note_state(self, feed, state, message):
        self.states.append(state)


def make_feed(tmp, calibrated=True, video="scene.mp4") -> FeedConfig:
    setup_dir = os.path.join(tmp, "setup")
    os.makedirs(setup_dir, exist_ok=True)
    if calibrated:
        for name in ("flood_config.json", "reference_dry_day.png"):
            shutil.copy(os.path.join(scene_dir(), name), setup_dir)
    return FeedConfig(id="test", location="Test Road", video=os.path.join(scene_dir(), video),
                      setup_dir=setup_dir, lat=8.48, lng=124.64, stream_fps=100.0)


def wait_for(cond, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestSetupValidation(unittest.TestCase):
    def test_accepts_valid_patch(self):
        out = validate_setup_patch({"flood_roi": [[10, 10], [100, 10], [100, 90]],
                                    "marker_roi": [[1, 2], [3, 2], [3, 50], [1, 50]],
                                    "ref_height_m": 1.0}, [640, 360], marker_locked=False)
        self.assertEqual(out["flood_roi"], [[10, 10], [100, 10], [100, 90]])
        self.assertEqual(len(out["marker_roi"]), 4)
        self.assertEqual(out["ref_height_m"], 1.0)

    def test_rejects_bad_values(self):
        cases = [
            {"flood_roi": [[10, 10], [20, 20]]},                       # too few points
            {"flood_roi": [[10, 10], [2000, 10], [100, 90]]},          # outside the frame
            {"marker_roi": [[1, 2], [3, 2], [3, 50]]},                 # not 4 corners
            {"ref_height_m": 9.0},
            {"flood_roi": "nope"},
            {},
        ]
        for body in cases:
            with self.assertRaises(SetupValidationError, msg=body):
                validate_setup_patch(body, [640, 360], marker_locked=False)

    def test_locked_marker_cannot_move(self):
        with self.assertRaises(SetupValidationError):
            validate_setup_patch({"marker_roi": [[1, 2], [3, 2], [3, 50], [1, 50]]}, [640, 360], True)
        with self.assertRaises(SetupValidationError):
            validate_setup_patch({"ref_height_m": 1.2}, [640, 360], True)
        # the flood ROI may still change
        validate_setup_patch({"flood_roi": [[10, 10], [100, 10], [100, 90]]}, [640, 360], True)


class TestMongoDocuments(unittest.TestCase):
    feed = SimpleNamespace(id="mastersons", location="Masterson's Avenue", lat=8.46, lng=124.62)

    def test_status_mapping(self):
        self.assertEqual(map_status("SAFE"), "NORMAL")
        self.assertEqual(map_status("ALERT"), "ALERT")
        self.assertEqual(map_status("DANGER"), "DANGER")
        self.assertIsNone(map_status("NOT_CALIBRATED"))

    def test_camera_update_calibrated(self):
        payload = {"status": "ALERT", "water_level_m": 0.34, "flood_coverage_pct": 40.0,
                   "image_quality": {"score": 0.9, "mode": "GOOD", "lighting": "day"}}
        setup = {"calibrated": True, "flood_roi": [[0, 0], [10, 0], [10, 5]], "frame_size": [960, 540]}
        upd = build_camera_update(self.feed, payload, setup)
        self.assertEqual(upd["$set"]["status"], "ALERT")
        self.assertEqual(upd["$set"]["water_level"], 0.34)
        self.assertEqual(upd["$set"]["roi_config"]["width"], 10)
        self.assertEqual(upd["$set"]["feed"]["stream_path"], "/api/feeds/mastersons/stream")
        self.assertEqual(upd["$setOnInsert"]["location"]["coordinates"], [124.62, 8.46])
        self.assertNotIn("location", upd["$set"])                  # never overwrite /config edits

    def test_uncalibrated_writes_no_level(self):
        payload = {"status": "SAFE", "water_level_m": 0.0}
        upd = build_camera_update(self.feed, payload, {"calibrated": False})
        self.assertNotIn("water_level", upd["$set"])
        self.assertNotIn("status", upd["$set"])
        self.assertFalse(upd["$set"]["feed"]["calibrated"])

    def test_reading_doc(self):
        doc = build_reading_doc(self.feed, {"status": "SAFE", "water_level_m": 0.1, "processing_ms": 12.0},
                                {"calibrated": True}, "START", {"processing_ms": 12.0})
        self.assertEqual(doc["camera_id"], camera_doc_id("mastersons"))
        self.assertEqual(doc["status"], "NORMAL")
        self.assertEqual(doc["event"], "START")
        json.dumps(doc, default=str)

    def test_events(self):
        tr = EventTracker()
        self.assertEqual(tr.update({"status": "SAFE", "water_level_m": 0.0}), "START")
        self.assertIsNone(tr.update({"status": "SAFE", "water_level_m": 0.0}))
        self.assertEqual(tr.update({"status": "ALERT", "water_level_m": 0.31}), "STATUS CHANGED")


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feed-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, feed):
        sink = FakeSink()
        worker = FeedWorker(feed, sink, realtime=False)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(worker.stop)
        return worker, sink

    def test_measures_streams_and_loops(self):
        worker, sink = self._start(make_feed(self.tmp))
        self.assertTrue(wait_for(lambda: worker.loop_index >= 1), "the clip never looped")
        levels = [p["water_level_m"] for p, _ in sink.payloads if p.get("water_level_m") is not None]
        self.assertTrue(levels, "no water levels measured")
        self.assertGreater(max(levels), 0.50)                       # the synthetic flood reaches 0.60 m
        statuses = {p.get("status") for p, _ in sink.payloads}
        self.assertTrue({"SAFE", "ALERT", "DANGER"} <= statuses, statuses)
        setup = worker.setup()
        self.assertTrue(setup["calibrated"])
        self.assertEqual(setup["references"], ["day"])
        for overlay in (False, True):
            jpeg = worker.latest_jpeg(overlay)
            self.assertTrue(jpeg and jpeg[:2] == b"\xff\xd8", "not a JPEG")
        # the clean view must not carry the overlay (draw_overlay draws in place)
        import cv2
        import numpy as np
        clean = cv2.imdecode(np.frombuffer(worker.latest_jpeg(False), np.uint8), cv2.IMREAD_COLOR)
        annotated = cv2.imdecode(np.frombuffer(worker.latest_jpeg(True), np.uint8), cv2.IMREAD_COLOR)
        panel = (slice(0, 170), slice(0, 370))                     # the info panel of draw_overlay
        diff = np.abs(clean[panel].astype(int) - annotated[panel].astype(int)).mean()
        self.assertGreater(diff, 10, "clean frame contains the OpenCV overlay")
        payload = worker.reading()
        for key in ("feed_id", "loop_index", "video_t_s", "processing_ms", "marker_mode"):
            self.assertIn(key, payload)
        json.dumps(payload)

    def test_loop_resets_filter(self):
        worker, sink = self._start(make_feed(self.tmp))
        self.assertTrue(wait_for(lambda: worker.loop_index >= 1))
        n = len(sink.payloads)
        self.assertTrue(wait_for(lambda: len(sink.payloads) > n + 5))
        first_of_loop = [p for p, _ in sink.payloads if p.get("loop_index") == 1 and p.get("video_t_s", 99) < 2.0]
        self.assertTrue(first_of_loop)
        # dry scene again: the level starts from zero, not from the previous loop's flood
        self.assertTrue(all((p.get("water_level_m") or 0) < 0.1 for p in first_of_loop[-3:]))

    def test_commands(self):
        worker, _ = self._start(make_feed(self.tmp))
        self.assertTrue(wait_for(lambda: worker.state == "running"))
        res = worker.submit("set_setup", {"ref_height_m": 0.95})
        self.assertTrue(res["ok"], res)
        with open(worker.cfg.config_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["ref_height_m"], 0.95)
        res = worker.submit("virtual_marker")                       # step 1: lock
        self.assertTrue(res["ok"], res)
        self.assertTrue(wait_for(lambda: worker.setup()["marker_locked"]))
        res = worker.submit("set_setup", {"ref_height_m": 1.0})     # locked: refused by the worker too
        self.assertFalse(res["ok"])
        res = worker.submit("virtual_unlock")
        self.assertTrue(res["ok"], res)
        self.assertTrue(worker.submit("restart")["ok"])
        self.assertTrue(worker.submit("save")["ok"])
        res = worker.submit("capture_reference")
        self.assertIn("message", res)

    def test_uncalibrated_and_missing_video(self):
        worker, _ = self._start(make_feed(self.tmp, calibrated=False))
        self.assertTrue(wait_for(lambda: worker.setup() is not None))
        self.assertFalse(worker.setup()["calibrated"])              # default ROIs only
        tmp2 = tempfile.mkdtemp(prefix="feed-test-missing-")
        self.addCleanup(shutil.rmtree, tmp2, True)
        missing, sink = self._start(make_feed(tmp2, video="does-not-exist.mp4"))
        self.assertTrue(wait_for(lambda: missing.state == "missing_video", 5))
        self.assertIn("missing_video", sink.states)
        self.assertTrue(missing.latest_jpeg(True))                  # placeholder frame
        self.assertFalse(missing.submit("save")["ok"])


class TestHttp(unittest.TestCase):
    def test_endpoints(self):
        from http.server import ThreadingHTTPServer

        from server import FeedService, make_handler
        tmp = tempfile.mkdtemp(prefix="feed-test-http-")
        self.addCleanup(shutil.rmtree, tmp, True)
        worker = FeedWorker(make_feed(tmp), None, realtime=True)
        worker.start()
        self.addCleanup(worker.stop)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FeedService({"test": worker}, None)))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.assertTrue(wait_for(lambda: worker.latest_jpeg(True) is not None and worker.state == "running"))

        with urllib.request.urlopen(f"{base}/feeds") as r:
            body = json.load(r)
        self.assertEqual(body["feeds"][0]["id"], "test")
        with urllib.request.urlopen(f"{base}/feeds/test/frame.jpg?overlay=0") as r:
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
        with urllib.request.urlopen(f"{base}/feeds/test/stream?overlay=1", timeout=10) as r:
            self.assertIn("multipart/x-mixed-replace", r.headers["Content-Type"])
            chunk = r.read(64 * 1024)
            self.assertIn(b"--frame", chunk)
            self.assertIn(b"\xff\xd8", chunk)

        def send(method, path, body):
            req = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(), method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        status, body = send("PUT", "/feeds/test/setup", {"flood_roi": [[1, 1], [2, 2]]})
        self.assertEqual(status, 400)
        self.assertTrue(body["problems"])
        status, body = send("PUT", "/feeds/test/setup", {"ref_height_m": 1.0})
        self.assertEqual(status, 200, body)
        status, body = send("POST", "/feeds/test/actions", {"action": "explode"})
        self.assertEqual(status, 400)
        status, body = send("POST", "/feeds/test/actions", {"action": "restart"})
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()
