"""
Measurement captures for the video feeds: a snapshot saved locally plus its metrics in MongoDB.

A capture is taken on every event (start, status change, obstruction, lighting change) and
every CAPTURE_INTERVAL_S seconds of video, but only while the feed is calibrated and measuring.

Each capture writes, locally (feed-service/captures/<feed id>/, git-ignored):
    <feed>_<UTC time>.jpg            the frame as captured (clean)
    <feed>_<UTC time>_processed.jpg  the same frame with the OpenCV overlay
    <feed>_<UTC time>.json           the capture's data and metrics
    summary.csv                      one row per capture (same columns as paper_results/summary.csv,
                                     plus the feed fields)

and one document in MONGODB_METRICS_COLLECTION ("camera_metrics") with the water level, flood
status, OpenCV metrics, pixel / classification accuracy and the latency breakdown. The images are
NOT stored in MongoDB; the document only names the local files so a record can be matched to them.

Accuracy and latency follow cameraTest.py (the camera test used for the thesis experiments):
    compute_accuracy(measured, truth) = max(0, 1 - |measured - truth|), and 1.0 without a truth
    pixel_detection_accuracy  = compute_accuracy(water_ratio, reference_ratio)
    classification_accuracy   = compute_accuracy(water_level / level_range, reference_ratio)
with reference_ratio = the measured water_ratio when no ground truth is given (as in cameraTest.py).
For a feed, water_ratio is the flood ROI coverage and level_range is the marker's reference height
(cameraTest.py used its 0-3 m depth range). The latency stages are timed on the actual frame instead
of the fixed 60 % / 10 % shares cameraTest.py estimated.

Field names are snake_case (the database contract, see RULES.md section 2).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mongo_sink import EventTracker, camera_doc_id, map_status

LOG = logging.getLogger("feed-service")

CAPTURE_INTERVAL_S = 10.0
QUEUE_SIZE = 64
RETRY_S = 30.0
ACCURACY_METHOD = "cameraTest.py compute_accuracy (self-referenced, no ground truth)"

LATENCY_KEYS = ("tcapture_ms", "tprocessing_ms", "thsv_classification_ms", "tkalman_ms", "tsnapshot_ms",
                "tdb_write_ms", "tsync_ms", "troute_ms", "Tresponse_ms")
SUMMARY_HEADER = (["camera_id", "timestamp", "location", "trigger", "event", "loop_index", "video_t_s",
                   "water_level_m", "status", "flood_coverage_pct", "confidence", "data_valid"]
                  + list(LATENCY_KEYS)
                  + ["pixel_detection_accuracy", "classification_accuracy", "snapshot_file",
                     "processed_image_path", "db_written"])


def compute_accuracy(measured_value: float, ground_truth_value: float | None) -> float:
    """Same as cameraTest.py."""
    if ground_truth_value is None:
        return 1.0
    return max(0.0, 1.0 - abs(measured_value - ground_truth_value))


def accuracy_metrics(payload: dict, setup: dict | None, reference_ratio: float | None = None) -> dict:
    """Pixel and classification accuracy the way cameraTest.py computes them (see module docstring)."""
    coverage = payload.get("flood_coverage_pct")
    level = payload.get("water_level_m")
    level_range = (setup or {}).get("ref_height_m")
    water_ratio = None if coverage is None else max(0.0, min(1.0, float(coverage) / 100.0))
    reference = reference_ratio if reference_ratio is not None else water_ratio
    pa = ca = None
    if water_ratio is not None:
        pa = round(compute_accuracy(water_ratio, reference), 4)
    if level is not None and level_range and reference is not None:
        level_fraction = max(0.0, min(1.0, float(level) / float(level_range)))
        ca = round(compute_accuracy(level_fraction, reference), 4)
    return {
        "pixel_detection_accuracy": pa,
        "classification_accuracy": ca,
        "accuracy_inputs": {
            "water_ratio": None if water_ratio is None else round(water_ratio, 4),
            "reference_ratio": None if reference is None else round(reference, 4),
            "water_level_m": level,
            "level_range_m": level_range,
            "method": ACCURACY_METHOD,
        },
    }


class CaptureScheduler:
    """Decides, frame by frame, whether this frame is captured: on an event or every interval_s of video."""

    def __init__(self, interval_s: float = CAPTURE_INTERVAL_S):
        self.interval_s = interval_s
        self.events = EventTracker()
        self.last_t: float | None = None

    def check(self, payload: dict, setup: dict | None, t: float) -> tuple[str, str | None] | None:
        event = self.events.update(payload)           # always fed, so events are not missed
        if not (setup or {}).get("calibrated") or payload.get("water_level_m") is None:
            return None
        if event:
            self.last_t = t
            return "event", event
        if self.last_t is None or t - self.last_t >= self.interval_s or t < self.last_t:
            self.last_t = t
            return "interval", None
        return None


@dataclass
class CaptureJob:
    feed: object                      # FeedConfig
    payload: dict
    setup: dict | None
    trigger: str                      # "event" | "interval"
    event: str | None
    clean_jpeg: bytes | None
    processed_jpeg: bytes | None
    latency: dict                     # stages measured on the worker thread (ms)
    t_frame_ready: float              # perf_counter() when the frame was decoded and resized
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    t_queued: float = field(default_factory=time.perf_counter)


def capture_name(feed_id: str, when: datetime) -> str:
    return f"{feed_id}_{when.strftime('%Y%m%dT%H%M%S%fZ')}"


def build_metrics_doc(job: CaptureJob, snapshot: dict, reference_ratio: float | None = None) -> dict:
    p, feed = job.payload, job.feed
    iq = p.get("image_quality") or {}
    doc = {
        "camera_id": camera_doc_id(feed.id),
        "feed_id": feed.id,
        "location_label": feed.location,
        "capture_id": capture_name(feed.id, job.captured_at),
        "timestamp": job.captured_at,
        "trigger": job.trigger,
        "event": job.event,
        "video_t_s": p.get("video_t_s"),
        "loop_index": p.get("loop_index"),
        "calibrated": bool((job.setup or {}).get("calibrated")),
        "marker_mode": p.get("marker_mode"),
        "water_level_m": p.get("water_level_m"),
        "raw_level_m": p.get("raw_level_m"),
        "status": map_status(p.get("status")) or p.get("status"),
        "rate_m_per_min": p.get("rate_m_per_min"),
        "above_range": p.get("above_range"),
        "flood_detected": p.get("flood_detected"),
        "flood_coverage_pct": p.get("flood_coverage_pct"),
        "confidence": p.get("confidence"),
        "data_valid": p.get("data_valid"),
        "anomaly_reason": p.get("anomaly_reason"),
        "resolution_cm_per_px": p.get("resolution_cm_per_px"),
        "image_quality": {"score": iq.get("score"), "mode": iq.get("mode"), "lighting": iq.get("lighting"),
                          "conditions": iq.get("conditions") or []},
        "fps": p.get("fps"),
    }
    doc.update(accuracy_metrics(p, job.setup, reference_ratio))
    doc["latency"] = {k: job.latency.get(k) for k in LATENCY_KEYS}
    doc["snapshot"] = snapshot
    return doc


def _rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except ValueError:                                # another drive on Windows
        return path


class MetricsRecorder(threading.Thread):
    """Writes the captures (local files + MongoDB) off the video threads, so a capture never stalls a feed."""

    def __init__(self, captures_dir: str, project_dir: str, uri: str | None = None, db_name: str | None = None,
                 collection: str | None = None, reference_ratio: float | None = None):
        super().__init__(daemon=True, name="metrics-recorder")
        self.captures_dir = captures_dir
        self.project_dir = project_dir
        self.uri = uri
        self.db_name = db_name
        self.collection_name = collection
        self.reference_ratio = reference_ratio
        self.status = "disabled" if not (uri and collection) else "connecting"
        self.error: str | None = None
        self.captures = 0
        self.db_writes = 0
        self.dropped = 0
        self._queue: queue.Queue[CaptureJob] = queue.Queue(maxsize=QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._collection = None
        self._next_connect = 0.0
        self._csv_lock = threading.Lock()

    # ---- called from the feed workers (never blocks) ----
    def submit(self, job: CaptureJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            LOG.warning("[%s] capture dropped: the recorder is %d captures behind", job.feed.id, QUEUE_SIZE)
            return False

    def stop(self):
        self._stop_event.set()

    def describe(self) -> dict:
        return {"status": self.status, "error": self.error, "captures": self.captures,
                "db_writes": self.db_writes, "dropped": self.dropped,
                "collection": self.collection_name, "captures_dir": _rel(self.captures_dir, self.project_dir)}

    # ---- recorder thread ----
    def run(self):
        self._connect()                               # up front: the connection time is not a capture's latency
        while not (self._stop_event.is_set() and self._queue.empty()):
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.record(job)
            except Exception:                         # one bad capture must not end the recorder
                LOG.exception("[%s] capture failed", job.feed.id)

    def _connect(self):
        if self._collection is not None or not (self.uri and self.collection_name):
            return self._collection
        if time.monotonic() < self._next_connect:
            return None
        from pymongo import ASCENDING, DESCENDING, MongoClient
        try:
            client = MongoClient(self.uri, serverSelectionTimeoutMS=8000, appname="floodsight-metrics")
            client.admin.command("ping")
            coll = client[self.db_name][self.collection_name]
            coll.create_index([("feed_id", ASCENDING), ("timestamp", DESCENDING)])
            coll.create_index([("camera_id", ASCENDING), ("timestamp", DESCENDING)])
            self._collection = coll
            self.status, self.error = "connected", None
            LOG.info("Metrics captures -> MongoDB %s: %s", self.db_name, self.collection_name)
        except Exception as e:                        # network / auth problems: keep saving locally
            self.status, self.error = "error", str(e).split("\n")[0][:200]
            self._next_connect = time.monotonic() + RETRY_S
            LOG.warning("Metrics collection not reachable (%s) - captures are saved locally only", self.error)
        return self._collection

    def record(self, job: CaptureJob) -> dict:
        """Save one capture. Returns the local record (also written as <name>.json)."""
        tsync_ms = (time.perf_counter() - job.t_queued) * 1000.0
        name = capture_name(job.feed.id, job.captured_at)
        out_dir = os.path.join(self.captures_dir, job.feed.id)
        os.makedirs(out_dir, exist_ok=True)

        t0 = time.perf_counter()
        files = {}
        for key, data, suffix in (("file", job.clean_jpeg, ""), ("processed_file", job.processed_jpeg, "_processed")):
            if data:
                path = os.path.join(out_dir, f"{name}{suffix}.jpg")
                with open(path, "wb") as f:
                    f.write(data)
                files[key] = _rel(path, self.project_dir)
        write_ms = (time.perf_counter() - t0) * 1000.0
        size = (job.setup or {}).get("frame_size") or [None, None]
        snapshot = {"file": files.get("file"), "processed_file": files.get("processed_file"),
                    "json_file": _rel(os.path.join(out_dir, f"{name}.json"), self.project_dir),
                    "width": size[0], "height": size[1], "format": "jpeg"}

        latency = dict(job.latency)
        latency["tsnapshot_ms"] = round(float(latency.get("tsnapshot_ms") or 0.0) + write_ms, 3)
        latency["tsync_ms"] = round(tsync_ms, 3)
        latency.setdefault("troute_ms", 0.0)          # routing is not part of the feed pipeline (as in cameraTest.py)
        job.latency = latency
        doc = build_metrics_doc(job, snapshot, self.reference_ratio)

        db_written = False
        coll = self._connect()
        if coll is not None:
            try:
                t_db = time.perf_counter()
                inserted = coll.insert_one(doc)
                tdb_write_ms = (time.perf_counter() - t_db) * 1000.0
                latency["tdb_write_ms"] = round(tdb_write_ms, 3)
                latency["Tresponse_ms"] = round(float(latency.get("tcapture_ms") or 0.0)
                                                + (time.perf_counter() - job.t_frame_ready) * 1000.0, 3)
                # the insert can only be timed once it is done: complete the document's latency
                coll.update_one({"_id": inserted.inserted_id},
                                {"$set": {"latency.tdb_write_ms": latency["tdb_write_ms"],
                                          "latency.Tresponse_ms": latency["Tresponse_ms"]}})
                db_written = True
                self.db_writes += 1
                self.status, self.error = "connected", None
            except Exception as e:
                self.status, self.error = "error", str(e).split("\n")[0][:200]
                self._collection, self._next_connect = None, time.monotonic() + RETRY_S
                LOG.warning("[%s] metrics write failed (%s) - capture saved locally only", job.feed.id, self.error)
        if not db_written:
            latency["tdb_write_ms"] = None
            latency["Tresponse_ms"] = round(float(latency.get("tcapture_ms") or 0.0)
                                            + (time.perf_counter() - job.t_frame_ready) * 1000.0, 3)

        doc.pop("_id", None)
        doc["latency"] = {k: latency.get(k) for k in LATENCY_KEYS}
        doc["db_written"] = db_written
        doc["db_collection"] = self.collection_name if db_written else None
        record = json.loads(json.dumps(doc, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)))
        with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        self._append_summary(out_dir, record)
        self.captures += 1
        return record

    def _append_summary(self, out_dir: str, record: dict):
        lat = record.get("latency") or {}
        snap = record.get("snapshot") or {}
        row = [record.get("camera_id"), record.get("capture_id", "").rsplit("_", 1)[-1], record.get("location_label"),
               record.get("trigger"), record.get("event") or "", record.get("loop_index"), record.get("video_t_s"),
               record.get("water_level_m"), record.get("status"), record.get("flood_coverage_pct"),
               record.get("confidence"), record.get("data_valid")]
        row += [lat.get(k) for k in LATENCY_KEYS]
        row += [record.get("pixel_detection_accuracy"), record.get("classification_accuracy"),
                snap.get("file"), snap.get("processed_file"), record.get("db_written")]
        path = os.path.join(out_dir, "summary.csv")
        with self._csv_lock:
            new = not os.path.isfile(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if new:
                    writer.writerow(SUMMARY_HEADER)
                writer.writerow(["" if v is None else v for v in row])
