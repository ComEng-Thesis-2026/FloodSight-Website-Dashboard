"""
MongoDB output for the video feeds.

Two writes per feed, from a background thread so a slow network never stalls a video:
  * the camera document in MONGODB_COLLECTION ("cctv data"), _id "feed-<id>":
    the live level / status / OpenCV metrics, updated every second. The dashboard's
    GET /api/cameras reads these documents like any other camera.
  * one reading per second (plus one immediately on every event) in
    MONGODB_READINGS_COLLECTION ("flood_readings"): the history used for the
    dashboard trend and for the thesis evaluation (status accuracy, latency).

Field names are snake_case (the database contract, see RULES.md section 2).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

LOG = logging.getLogger("feed-service")

WRITE_INTERVAL_S = 1.0
RETRY_S = 10.0


def camera_doc_id(feed_id: str) -> str:
    return f"feed-{feed_id}"


def map_status(status: str | None) -> str | None:
    """OpenCV status -> dashboard status. SAFE is called NORMAL on the dashboard."""
    return {"SAFE": "NORMAL", "ALERT": "ALERT", "DANGER": "DANGER"}.get(status or "")


def _roi_config(flood_roi) -> dict | None:
    if not flood_roi:
        return None
    xs = [p[0] for p in flood_roi]
    ys = [p[1] for p in flood_roi]
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys),
            "vertices": [{"x": p[0], "y": p[1]} for p in flood_roi]}


class EventTracker:
    """Same events as floodmon's TerminalFeed: status changes, obstruction, held data, lighting."""

    def __init__(self):
        self.last_status = None
        self.last_anomaly = False
        self.last_held = False
        self.last_light = None

    def update(self, payload: dict) -> str | None:
        event = None
        st, an = payload.get("status"), bool(payload.get("anomaly"))
        held = "held" in str(payload.get("anomaly_reason") or "")
        light = (payload.get("image_quality") or {}).get("lighting")
        if payload.get("water_level_m") is not None:
            if st != self.last_status:
                event = "STATUS CHANGED" if self.last_status not in (None, "NOT_CALIBRATED") else "START"
            elif an and not self.last_anomaly:
                event = "IMAGE NOT USABLE - LEVEL HELD" if held else "OBSTRUCTION DETECTED"
            elif not an and self.last_anomaly:
                event = "MEASURING AGAIN" if self.last_held else "OBSTRUCTION CLEARED"
        if event is None and self.last_light is not None and light != self.last_light:
            event = f"LIGHTING {str(light).upper()}"
        self.last_status, self.last_anomaly, self.last_held, self.last_light = st, an, held, light
        return event


def build_feed_info(feed, payload: dict | None, setup: dict | None, state: str, message: str | None) -> dict:
    p = payload or {}
    s = setup or {}
    iq = p.get("image_quality") or {}
    return {
        "feed_id": feed.id,
        "stream_path": f"/api/feeds/{feed.id}/stream",
        "frame_size": s.get("frame_size"),
        "calibrated": bool(s.get("calibrated")),
        "marker_mode": s.get("marker_mode"),
        "references": s.get("references") or [],
        "data_valid": p.get("data_valid"),
        "flood_detected": p.get("flood_detected"),
        "flood_coverage_pct": p.get("flood_coverage_pct"),
        "confidence": p.get("confidence"),
        "raw_level_m": p.get("raw_level_m"),
        "rate_m_per_min": p.get("rate_m_per_min"),
        "above_range": p.get("above_range"),
        "resolution_cm_per_px": p.get("resolution_cm_per_px"),
        "anomaly_reason": p.get("anomaly_reason"),
        "image_quality": {"score": iq.get("score"), "mode": iq.get("mode"), "lighting": iq.get("lighting"),
                          "conditions": iq.get("conditions") or []},
        "fps": p.get("fps"),
        "loop_index": p.get("loop_index"),
        "video_t_s": p.get("video_t_s"),
        "processing_ms": p.get("processing_ms"),
        "service_state": state,
        "service_message": message,
    }


def build_camera_update(feed, payload: dict | None, setup: dict | None, state: str = "running",
                        message: str | None = None, now: datetime | None = None) -> dict:
    """The update for the feed's camera document. Location is only set when the document is new,
    so names and coordinates edited on the Camera Config page are never overwritten."""
    now = now or datetime.now(timezone.utc)
    s = setup or {}
    set_fields: dict = {
        "feed": build_feed_info(feed, payload, setup, state, message),
        "timestamp": now.isoformat(),
    }
    if setup:
        set_fields["opencv_setup"] = {k: s.get(k) for k in ("flood_roi", "marker_roi", "ref_height_m",
                                                             "marker_mode", "frame_size")}
        roi = _roi_config(s.get("flood_roi"))
        if roi:
            set_fields["roi_config"] = roi
    status = map_status((payload or {}).get("status"))
    if s.get("calibrated") and status is not None and payload.get("water_level_m") is not None:
        set_fields["water_level"] = payload["water_level_m"]
        set_fields["status"] = status
    on_insert: dict = {"location_label": feed.location}
    if feed.lat is not None and feed.lng is not None:
        on_insert["location"] = {"type": "Point", "coordinates": [feed.lng, feed.lat]}
    return {"$set": set_fields, "$setOnInsert": on_insert}


def build_reading_doc(feed, payload: dict, setup: dict | None, event: str | None,
                      latency: dict | None = None, now: datetime | None = None) -> dict:
    iq = payload.get("image_quality") or {}
    return {
        "camera_id": camera_doc_id(feed.id),
        "feed_id": feed.id,
        "timestamp": now or datetime.now(timezone.utc),
        "video_t_s": payload.get("video_t_s"),
        "loop_index": payload.get("loop_index"),
        "calibrated": bool((setup or {}).get("calibrated")),
        "water_level_m": payload.get("water_level_m"),
        "raw_level_m": payload.get("raw_level_m"),
        "status": map_status(payload.get("status")) or payload.get("status"),
        "flood_detected": payload.get("flood_detected"),
        "flood_coverage_pct": payload.get("flood_coverage_pct"),
        "confidence": payload.get("confidence"),
        "data_valid": payload.get("data_valid"),
        "anomaly_reason": payload.get("anomaly_reason"),
        "marker_mode": payload.get("marker_mode"),
        "image_quality": {"score": iq.get("score"), "mode": iq.get("mode"), "lighting": iq.get("lighting")},
        "fps": payload.get("fps"),
        "event": event,
        "latency": latency or {},
    }


class MongoSink(threading.Thread):
    def __init__(self, uri: str | None, db_name: str, cameras_collection: str, readings_collection: str,
                 feeds=(), interval_s: float = WRITE_INTERVAL_S):
        super().__init__(daemon=True, name="mongo-sink")
        self.feeds = list(feeds)
        self.uri = uri
        self.db_name = db_name
        self.cameras_name = cameras_collection
        self.readings_name = readings_collection
        self.interval_s = interval_s
        self.status = "disabled" if not uri else "connecting"
        self.error: str | None = None
        self.writes = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._latest: dict[str, dict] = {}        # feed id -> {feed, payload, setup, t_mono, events}
        self._trackers: dict[str, EventTracker] = {}
        self._last_write: dict[str, float] = {}
        self._states: dict[str, tuple] = {}
        self._cameras = self._readings = None

    # ---- called from the feed workers (cheap, never blocks on the network) ----
    def submit(self, feed, payload: dict, setup: dict | None):
        tracker = self._trackers.setdefault(feed.id, EventTracker())
        event = tracker.update(payload)
        with self._lock:
            entry = self._latest.setdefault(feed.id, {"events": []})
            entry.update(feed=feed, payload=payload, setup=setup, t_mono=time.monotonic())
            if event:
                entry["events"].append((event, payload, setup, time.monotonic()))
        if event:
            self._wake.set()

    def note_state(self, feed, state: str, message: str | None):
        with self._lock:
            self._states[feed.id] = (feed, state, message)
        self._wake.set()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def describe(self) -> dict:
        return {"status": self.status, "error": self.error, "writes": self.writes,
                "database": self.db_name, "cameras": self.cameras_name, "readings": self.readings_name}

    # ---- sink thread ---------------------------------------------------------
    def _connect(self) -> bool:
        from pymongo import ASCENDING, DESCENDING, MongoClient
        try:
            client = MongoClient(self.uri, serverSelectionTimeoutMS=8000, appname="floodsight-feed-service")
            client.admin.command("ping")
            db = client[self.db_name]
            self._cameras = db[self.cameras_name]
            self._readings = db[self.readings_name]
            self._readings.create_index([("feed_id", ASCENDING), ("timestamp", DESCENDING)])
            self._readings.create_index([("camera_id", ASCENDING), ("timestamp", DESCENDING)])
            self.status, self.error = "connected", None
            LOG.info("MongoDB connected (%s: %s, %s)", self.db_name, self.cameras_name, self.readings_name)
            return True
        except Exception as e:                       # network / auth problems: retry later
            self.status, self.error = "error", str(e).split("\n")[0][:200]
            LOG.warning("MongoDB not reachable (%s) - retrying in %.0f s", self.error, RETRY_S)
            return False

    def run(self):
        if not self.uri:
            LOG.warning("MONGODB_URI is not set - readings are NOT written to MongoDB")
            return
        while not self._stop_event.is_set() and not self._connect():
            self._stop_event.wait(RETRY_S)
        self._apply_coordinates()
        while not self._stop_event.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            try:
                self._flush()
            except Exception as e:
                self.status, self.error = "error", str(e).split("\n")[0][:200]
                LOG.warning("MongoDB write failed: %s", self.error)
                self._stop_event.wait(2.0)

    def _apply_coordinates(self):
        """Coordinates added to feeds.json later are applied once, if the document has none yet."""
        for feed in self.feeds:
            if feed.lat is None or feed.lng is None:
                continue
            try:
                self._cameras.update_one(
                    {"_id": camera_doc_id(feed.id), "location": {"$exists": False}},
                    {"$set": {"location": {"type": "Point", "coordinates": [feed.lng, feed.lat]}}})
            except Exception as e:
                LOG.warning("Could not set coordinates for %s: %s", feed.id, e)

    def _flush(self):
        now = time.monotonic()
        with self._lock:
            states = dict(self._states)
            self._states.clear()
            jobs = []
            for feed_id, entry in self._latest.items():
                events = entry["events"]
                entry["events"] = []
                due = "payload" in entry and now - self._last_write.get(feed_id, 0.0) >= self.interval_s
                if events or due:
                    jobs.append((entry["feed"], entry.get("payload"), entry.get("setup"), entry.get("t_mono"),
                                 events, due))
        for feed, state, message in states.values():           # missing video / error / running
            if state != "running":
                self._cameras.update_one({"_id": camera_doc_id(feed.id)},
                                         build_camera_update(feed, None, None, state, message), upsert=True)
        for feed, payload, setup, t_mono, events, due in jobs:
            for event, ev_payload, ev_setup, ev_t in events:
                self._write(feed, ev_payload, ev_setup, ev_t, event, update_camera=False)
            if due:
                self._write(feed, payload, setup, t_mono, None, update_camera=True)
                self._last_write[feed.id] = now
            elif events:
                self._write_camera(feed, payload, setup)        # status change: show it at once

    def _write_camera(self, feed, payload, setup) -> float:
        t0 = time.perf_counter()
        self._cameras.update_one({"_id": camera_doc_id(feed.id)}, build_camera_update(feed, payload, setup),
                                 upsert=True)
        return (time.perf_counter() - t0) * 1000.0

    def _write(self, feed, payload, setup, t_mono, event, update_camera: bool):
        if payload is None:
            return
        update_ms = self._write_camera(feed, payload, setup) if update_camera else None
        if not (setup or {}).get("calibrated"):
            return                                  # default ROIs: no readings worth keeping yet
        latency = {
            "processing_ms": payload.get("processing_ms"),
            "camera_update_ms": None if update_ms is None else round(update_ms, 1),
            "frame_to_write_ms": None if t_mono is None else round((time.monotonic() - t_mono) * 1000.0, 1),
        }
        self._readings.insert_one(build_reading_doc(feed, payload, setup, event, latency))
        self.writes += 1
        self.status, self.error = "connected", None
