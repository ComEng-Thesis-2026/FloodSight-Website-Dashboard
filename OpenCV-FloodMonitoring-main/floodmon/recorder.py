"""
Raw video recording for later re-analysis (--record DIR).

Field footage is the raw material of the thesis evaluation: when the program
improves, the same floods can be measured again and compared. Frames are saved
exactly as they arrive (after down-scaling to the processing size, before lens
correction), in segments of --record-minutes. Each segment gets:

    <camera>_<YYYYmmdd_HHMMSS>.mp4          the video
    <camera>_<YYYYmmdd_HHMMSS>.mp4.json     start time, fps, frame size (used for timestamps on replay)
    <camera>_<YYYYmmdd_HHMMSS>.frames.csv   exact time of every frame (live cameras deliver unevenly)

Replay:  python FloodMonitoring.py --source recordings/CAM-01_20261108_140000.mp4
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime

import cv2

LOG = logging.getLogger("FloodMonitoring")


class SegmentRecorder:
    def __init__(self, out_dir: str, camera_id: str, fps: float, minutes: float = 10.0,
                 settings_file: str | None = None):
        self.dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.camera_id = camera_id
        self.fps = float(fps) if fps and fps > 0 else 15.0
        self.seconds = max(10.0, minutes * 60.0)
        self.settings_file = settings_file
        self.vw = None
        self.times = None
        self.t0 = None
        self.n = 0
        self.path = None

    def _open(self, frame, t: float, wall: datetime):
        self.close()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.camera_id)
        base = os.path.join(self.dir, f"{safe}_{wall:%Y%m%d_%H%M%S}")
        self.path = base + ".mp4"
        h, w = frame.shape[:2]
        self.vw = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        if not self.vw.isOpened():
            LOG.error("Cannot write %s - recording stopped", self.path)
            self.vw = None
            return
        meta = dict(start_time=wall.isoformat(timespec="milliseconds"), fps=self.fps, frame_size=[w, h],
                    camera_id=self.camera_id, settings=self.settings_file)
        with open(self.path + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        self.times = open(base + ".frames.csv", "w", newline="", encoding="utf-8")
        self._tw = csv.writer(self.times)
        self._tw.writerow(["frame", "t_s", "wall_time"])
        self.t0, self.n = t, 0
        LOG.info("Recording to %s", self.path)

    def write(self, frame, t: float, wall: datetime | None = None):
        wall = wall or datetime.now().astimezone()
        if self.vw is None or t - self.t0 >= self.seconds:
            self._open(frame, t, wall)
            if self.vw is None:
                return
        self.vw.write(frame)
        self._tw.writerow([self.n, round(t - self.t0, 3), wall.isoformat(timespec="milliseconds")])
        self.n += 1
        if self.n % 100 == 0:
            self.times.flush()

    def close(self):
        if self.vw is not None:
            self.vw.release()
            self.vw = None
        if self.times is not None:
            self.times.close()
            self.times = None
