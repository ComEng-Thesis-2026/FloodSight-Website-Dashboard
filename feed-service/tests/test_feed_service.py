"""
Tests for the feed service (worker, setup validation, MongoDB documents, HTTP).

    .venv\\Scripts\\python.exe -m unittest discover -s feed-service/tests -v

A short synthetic flood clip from floodmon.synthetic is rendered once;
no MongoDB connection is needed.
"""

import csv
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
from metrics_capture import (SUMMARY_HEADER, CaptureJob, CaptureScheduler,  # noqa: E402
                             MetricsRecorder, accuracy_metrics, compute_accuracy)
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

    def test_road_condition_events(self):
        tr = EventTracker()
        tr.update({"status": "SAFE", "water_level_m": 0.0, "road_condition": "DRY"})
        self.assertEqual(tr.update({"status": "SAFE", "water_level_m": 0.0, "road_condition": "WET"}), "ROAD WET")
        self.assertIsNone(tr.update({"status": "SAFE", "water_level_m": 0.0, "road_condition": "WET"}))
        # a status change wins; the road change is not reported twice afterwards
        self.assertEqual(tr.update({"status": "ALERT", "water_level_m": 0.4, "road_condition": "FLOODED"}),
                         "STATUS CHANGED")
        self.assertIsNone(tr.update({"status": "ALERT", "water_level_m": 0.4, "road_condition": "FLOODED"}))
        doc = build_reading_doc(self.feed, {"status": "ALERT", "water_level_m": 0.4, "road_condition": "FLOODED",
                                            "level_source": "flood_roi"}, {"calibrated": True}, None)
        self.assertEqual((doc["road_condition"], doc["level_source"]), ("FLOODED", "flood_roi"))


class TestMarkerMemory(unittest.TestCase):
    def test_learns_the_synthetic_marker(self):
        import cv2
        from floodmon.calibration import Calibration
        import marker_memory
        cal = Calibration.load(os.path.join(scene_dir(), "flood_config.json"))
        img = cv2.imread(os.path.join(scene_dir(), "reference_dry_day.png"))      # dry, marker in view
        mem, msg = marker_memory.learn(img, cal, source="test", reading_m=0.0)
        self.assertIsNotNone(mem, msg)
        self.assertEqual([b["color"] for b in mem.bands], ["GREEN", "YELLOW", "RED"])
        self.assertFalse(mem.upside_down)
        # the synthetic marker is painted with floodmon's bands: 0.30 m and 0.50 m
        self.assertAlmostEqual(mem.bands[0]["to_m"], 0.30, delta=0.03)
        self.assertAlmostEqual(mem.bands[1]["to_m"], 0.50, delta=0.03)
        self.assertEqual((mem.status_at(0.1), mem.status_at(0.4), mem.status_at(0.7), mem.status_at(1.5)),
                         ("SAFE", "ALERT", "DANGER", "DANGER"))
        tmp = tempfile.mkdtemp(prefix="feed-test-mem-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mem.save(tmp)
        back = marker_memory.MarkerMemory.load(tmp)
        self.assertEqual(back.bands, mem.bands)
        self.assertIsNotNone(back.photo)
        self.assertTrue(back.matches(cal))

    def test_upside_down_marker_is_measured_the_right_way_up(self):
        import cv2
        import numpy as np
        from floodmon.calibration import Calibration
        import marker_memory
        img = np.full((200, 200, 3), 90, np.uint8)
        # a 1.0 m marker 100 px tall, painted RED at the road (y 150), then YELLOW, GREEN on top
        for (y0, y1), bgr in (((100, 150), (0, 0, 200)), ((80, 100), (0, 210, 240)), ((50, 80), (0, 170, 0))):
            cv2.rectangle(img, (90, y0), (110, y1 - 1), bgr, -1)
        cal = Calibration(flood_roi=[[0, 150], [199, 150], [199, 199]],
                          marker_roi=[[90, 150], [110, 150], [110, 50], [90, 50]], ref_height_m=1.0)
        mem, _ = marker_memory.learn(img, cal, source="test")
        self.assertTrue(mem.upside_down)
        self.assertEqual([b["color"] for b in mem.seen_bands], ["RED", "YELLOW", "GREEN"])
        self.assertEqual([b["color"] for b in mem.bands], ["GREEN", "YELLOW", "RED"])
        self.assertAlmostEqual(mem.bands[0]["to_m"], 0.30, delta=0.02)
        self.assertAlmostEqual(mem.bands[1]["to_m"], 0.50, delta=0.02)

    def test_nothing_learned_without_a_marker(self):
        import numpy as np
        from floodmon.calibration import Calibration
        import marker_memory
        cal = Calibration(flood_roi=[[0, 150], [199, 150], [199, 199]],
                          marker_roi=[[90, 150], [110, 150], [110, 50], [90, 50]], ref_height_m=1.0)
        mem, msg = marker_memory.learn(np.full((200, 200, 3), 90, np.uint8), cal, source="test")
        self.assertIsNone(mem)
        self.assertIn("no green / yellow / red", msg)


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

    def test_lock_remembers_the_physical_marker(self):
        worker, sink = self._start(make_feed(self.tmp))
        self.assertTrue(wait_for(lambda: worker.setup() is not None))
        self.assertIsNone(worker.setup()["marker_memory"])          # physical, nothing remembered yet
        self.assertTrue(worker.submit("pause")["ok"])
        self.assertTrue(worker.submit("restart")["ok"])               # the dry scene, marker in view
        res = worker.submit("virtual_marker")                         # step 1: lock -> remember
        self.assertTrue(res["ok"], res)
        self.assertIn("Physical marker remembered", res["message"])
        self.assertTrue(wait_for(lambda: worker.setup()["marker_memory"] is not None))
        mem = worker.setup()["marker_memory"]
        self.assertEqual([b["color"] for b in mem["bands"]], ["GREEN", "YELLOW", "RED"])
        for name in ("marker_memory.json", "marker_memory.png"):
            self.assertTrue(os.path.isfile(os.path.join(worker.cfg.setup_dir, name)), name)
        self.assertTrue(wait_for(lambda: (worker.reading() or {}).get("marker_color") == "GREEN"))
        # a moved marker ROI no longer matches what was remembered
        self.assertTrue(worker.submit("virtual_unlock")["ok"])
        roi = worker.setup()["marker_roi"]
        res = worker.submit("set_setup", {"marker_roi": [[x + 3, y] for x, y in roi]})
        self.assertTrue(res["ok"], res)
        self.assertIn("remembered marker was cleared", res["message"])
        self.assertIsNone(res["setup"]["marker_memory"])
        self.assertTrue(wait_for(lambda: worker.setup()["marker_memory"] is None))
        self.assertFalse(os.path.isfile(os.path.join(worker.cfg.setup_dir, "marker_memory.json")))

    def test_locked_feed_remembers_from_its_dry_reference(self):
        feed = make_feed(self.tmp)
        with open(feed.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(marker_locked=True, virtual_marker=True)
        with open(feed.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        worker, _ = self._start(feed)
        self.assertTrue(wait_for(lambda: worker.setup() is not None))
        mem = worker.setup()["marker_memory"]
        self.assertIsNotNone(mem)
        self.assertTrue(mem["source"].startswith("dry reference"))
        self.assertTrue(wait_for(lambda: (worker.reading() or {}).get("marker_color") is not None))

    def test_virtual_marker_measures_from_flood_roi(self):
        feed = make_feed(self.tmp)
        with open(feed.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(marker_locked=True, virtual_marker=True)          # calibrated, then made virtual
        with open(feed.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        worker, sink = self._start(feed)
        self.assertTrue(wait_for(lambda: worker.loop_index >= 1, 60.0), "the clip never looped")
        first = [p for p, _ in sink.payloads if p.get("loop_index") == 0 and p.get("water_level_m") is not None]
        self.assertTrue(all(p.get("level_source") == "flood_roi" for p in first))
        self.assertTrue(all(p.get("method") == "flood_roi" for p in first))
        dry = [p for p in first if p["video_t_s"] < 5.0]               # the scene is dry for 6 s
        self.assertTrue(dry and all(p["road_condition"] == "DRY" and p["water_level_m"] == 0.0 for p in dry))
        flooded = [p for p in first if p.get("road_condition") == "FLOODED"]
        self.assertTrue(flooded, "the synthetic flood was never classified FLOODED")
        # depth = water share of the flood ROI x the site's full-cover depth (default 0.20 m)
        last = flooded[-1]
        self.assertGreater(last["water_level_m"], 0.0)
        self.assertEqual(last["full_cover_depth_m"], 0.20)
        self.assertAlmostEqual(last["raw_level_m"], last["flood_coverage_pct"] / 100 * 0.20, delta=0.005)
        self.assertLessEqual(max(p["raw_level_m"] for p in first), 0.20)
        self.assertFalse(last["obstruction_at_marker"])          # the marker ROI is not looked at
        self.assertTrue(all(p["road_condition"] in ("DRY", "WET", "FLOODED") for p in first))

    def test_pause_resume(self):
        worker, sink = self._start(make_feed(self.tmp))
        self.assertTrue(wait_for(lambda: worker.state == "running"))
        self.assertTrue(worker.submit("pause")["ok"])
        self.assertTrue(wait_for(lambda: worker.setup()["paused"] and worker.reading()["paused"]))
        n = len(sink.payloads)
        self.assertTrue(wait_for(lambda: len(sink.payloads) > n + 3))     # still measuring
        frozen = [p for p, _ in sink.payloads[n:]]
        self.assertEqual(len({p["video_t_s"] for p in frozen}), 1)        # clip position frozen
        # stay paused longer than floodmon's frozen-video limit (--frozen-s 2): the feed must not
        # crash or flag the paused clip as a frozen camera
        time.sleep(3.0)
        self.assertEqual(worker.state, "running", worker.message)
        problem = (worker.reading().get("image_quality") or {}).get("problem") or ""
        self.assertNotIn("frozen", problem)
        # restart while paused: steps to the first frame, stays paused
        self.assertTrue(worker.submit("restart")["ok"])
        self.assertTrue(wait_for(lambda: worker.reading()["video_t_s"] < 0.2 and worker.setup()["paused"]))
        # the dry reference capture completes on the frozen frame
        res = worker.submit("capture_reference")
        if not res["ok"] and res.get("needs_confirm"):
            res = worker.submit("capture_reference")
        self.assertTrue(res["ok"], res)
        self.assertTrue(wait_for(lambda: not worker.setup()["capturing"]))
        self.assertTrue(worker.submit("resume")["ok"])
        self.assertTrue(wait_for(lambda: not worker.setup()["paused"]))
        t0 = worker.reading()["video_t_s"]
        self.assertTrue(wait_for(lambda: worker.reading()["video_t_s"] != t0))  # playing again

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


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def insert_one(self, doc):
        doc["_id"] = len(self.docs) + 1
        self.docs[doc["_id"]] = json.loads(json.dumps(doc, default=str))
        return SimpleNamespace(inserted_id=doc["_id"])

    def update_one(self, flt, update):
        doc = self.docs[flt["_id"]]
        for key, value in update["$set"].items():
            parent, _, leaf = key.rpartition(".")
            (doc[parent] if parent else doc)[leaf] = value


class TestMetricsCapture(unittest.TestCase):
    feed = SimpleNamespace(id="kauswagan", location="Kauswagan Highway")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feed-test-captures-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_accuracy_follows_camera_test(self):
        self.assertEqual(compute_accuracy(0.4, None), 1.0)
        self.assertAlmostEqual(compute_accuracy(0.4, 0.5), 0.9)
        self.assertEqual(compute_accuracy(0.0, 2.0), 0.0)
        m = accuracy_metrics({"flood_coverage_pct": 60.0, "water_level_m": 0.3}, {"ref_height_m": 1.0})
        self.assertEqual(m["pixel_detection_accuracy"], 1.0)        # self-referenced, as in cameraTest.py
        self.assertAlmostEqual(m["classification_accuracy"], 0.7)  # 1 - |0.3/1.0 - 0.6|
        m = accuracy_metrics({"flood_coverage_pct": 60.0, "water_level_m": 0.3}, {"ref_height_m": 1.0}, 0.5)
        self.assertAlmostEqual(m["pixel_detection_accuracy"], 0.9)
        none = accuracy_metrics({}, {"ref_height_m": 1.0})
        self.assertIsNone(none["pixel_detection_accuracy"])
        self.assertIsNone(none["classification_accuracy"])

    def test_scheduler(self):
        s = CaptureScheduler(10.0)
        cal = {"calibrated": True}
        self.assertIsNone(s.check({"status": "SAFE", "water_level_m": 0.0}, {"calibrated": False}, 0.0))
        self.assertEqual(s.check({"status": "SAFE", "water_level_m": 0.0}, cal, 0.5), ("interval", None))
        self.assertIsNone(s.check({"status": "SAFE", "water_level_m": 0.0}, cal, 5.0))
        self.assertEqual(s.check({"status": "ALERT", "water_level_m": 0.31}, cal, 6.0),
                         ("event", "STATUS CHANGED"))
        self.assertIsNone(s.check({"status": "ALERT", "water_level_m": 0.32}, cal, 15.9))
        self.assertEqual(s.check({"status": "ALERT", "water_level_m": 0.32}, cal, 16.0), ("interval", None))
        self.assertIsNone(s.check({"status": "ALERT", "water_level_m": None}, cal, 40.0))

    def test_recorder_writes_metrics_not_images(self):
        rec = MetricsRecorder(self.tmp, self.tmp, uri="mongodb://unused", db_name="CCTVs", collection="camera_metrics")
        rec._collection = coll = FakeCollection()
        job = CaptureJob(feed=self.feed, payload={"status": "DANGER", "water_level_m": 0.55, "flood_coverage_pct": 80.0,
                                                  "image_quality": {"score": 0.8, "mode": "GOOD", "lighting": "day"}},
                         setup={"calibrated": True, "ref_height_m": 1.0, "frame_size": [960, 540]},
                         trigger="event", event="STATUS CHANGED", clean_jpeg=b"\xff\xd8clean",
                         processed_jpeg=b"\xff\xd8overlay",
                         latency={"tcapture_ms": 3.0, "tprocessing_ms": 20.0, "thsv_classification_ms": 15.0,
                                  "tkalman_ms": 0.2, "tsnapshot_ms": 4.0},
                         t_frame_ready=time.perf_counter())
        record = rec.record(job)
        doc = next(iter(coll.docs.values()))
        self.assertEqual(doc["camera_id"], "feed-kauswagan")
        self.assertEqual(doc["status"], "DANGER")
        self.assertEqual(doc["water_level_m"], 0.55)
        self.assertAlmostEqual(doc["classification_accuracy"], 0.75)
        self.assertIsNotNone(doc["latency"]["tdb_write_ms"])        # completed after the insert
        self.assertGreaterEqual(doc["latency"]["Tresponse_ms"], 3.0)
        self.assertFalse(any(isinstance(v, (bytes, bytearray)) for v in doc.values()))
        self.assertNotIn("clean", json.dumps(doc))                  # no image data in the database
        out = os.path.join(self.tmp, "kauswagan")
        name = doc["capture_id"]
        for suffix in (".jpg", "_processed.jpg", ".json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name + suffix)), suffix)
        self.assertEqual(doc["snapshot"]["file"], f"kauswagan/{name}.jpg")
        self.assertTrue(record["db_written"])
        with open(os.path.join(out, "summary.csv"), encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], SUMMARY_HEADER)
        self.assertEqual(len(rows), 2)

    def test_manual_recorder_writes_to_snapshots_override(self):
        rec = MetricsRecorder(os.path.join(self.tmp, "automatic"), self.tmp)
        snapshots_dir = os.path.join(self.tmp, "Snapshots")
        job = CaptureJob(
            feed=self.feed,
            payload={"status": "ALERT", "water_level_m": 0.35, "flood_coverage_pct": 50.0,
                     "marker_mode": "virtual"},
            setup={"calibrated": True, "ref_height_m": 1.0,
                   "marker_roi": [[100, 100], [200, 100], [200, 500], [100, 500]],
                   "frame_size": [960, 540]},
            trigger="manual",
            event="admin_save",
            clean_jpeg=b"\xff\xd8clean",
            processed_jpeg=b"\xff\xd8overlay",
            latency={"tcapture_ms": 0.0, "tprocessing_ms": 20.0,
                     "thsv_classification_ms": 15.0, "tkalman_ms": 0.2, "tsnapshot_ms": 4.0},
            t_frame_ready=time.perf_counter(),
            captures_dir=snapshots_dir,
        )
        record = rec.record(job)
        self.assertEqual(record["trigger"], "manual")
        self.assertEqual(record["event"], "admin_save")
        self.assertTrue(os.path.isfile(os.path.join(
            snapshots_dir, "kauswagan", record["capture_id"] + ".jpg")))
        self.assertTrue(os.path.isfile(os.path.join(
            snapshots_dir, "kauswagan", record["capture_id"] + ".json")))

    def test_worker_captures_events_and_intervals(self):
        rec = MetricsRecorder(os.path.join(self.tmp, "captures"), self.tmp)     # no MongoDB: local only
        rec.start()
        self.addCleanup(rec.stop)
        worker = FeedWorker(make_feed(self.tmp), FakeSink(), realtime=False, recorder=rec, capture_interval_s=5.0)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(worker.stop)
        self.assertTrue(wait_for(lambda: worker.loop_index >= 1), "the clip never looped")
        self.assertTrue(wait_for(lambda: rec.captures >= 8))
        with open(os.path.join(self.tmp, "captures", "test", "summary.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        triggers = {r["trigger"] for r in rows}
        self.assertEqual(triggers, {"event", "interval"})
        self.assertTrue({"ALERT", "DANGER"} <= {r["status"] for r in rows if r["event"] == "STATUS CHANGED"})
        for r in rows:
            self.assertGreater(float(r["tprocessing_ms"]), 0.0)
            self.assertGreater(float(r["thsv_classification_ms"]), 0.0)     # timed, not estimated
            self.assertLessEqual(float(r["thsv_classification_ms"]), float(r["tprocessing_ms"]))
            self.assertGreaterEqual(float(r["Tresponse_ms"]), float(r["tprocessing_ms"]))
            self.assertEqual(r["db_written"], "False")
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, r["snapshot_file"])))


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
