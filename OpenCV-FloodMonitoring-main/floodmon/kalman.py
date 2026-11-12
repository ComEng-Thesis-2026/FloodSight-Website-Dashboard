"""Kalman filter: smoothing + obstruction / anomaly rejection + hold on bad images."""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass

import cv2
import numpy as np

from .bands import MARKER_MAX_M

LOG = logging.getLogger("FloodMonitoring")


@dataclass
class FilterResult:
    level_m: float
    rate_m_per_min: float
    anomaly: bool
    reason: str | None
    innovation_sigma: float
    valid: bool = True           # False -> the level is unknown / too old to trust
    held_s: float = 0.0          # seconds since the last reliable measurement


class FloodKalman:
    """
    1-D constant-velocity Kalman filter (OpenCV cv2.KalmanFilter).
        state       = [water_level, rise_rate]
        measurement = raw water level from the marker

    Flood water rises/falls slowly. A car, motorcycle or person crossing the
    marker produces a sudden high spike. Every measurement is tested against the
    filter prediction (innovation gate, in standard deviations). Measurements
    that fail the gate, or that come with a moving object at the marker, are
    flagged as an ANOMALY (obstruction) and are NOT used to update the level.

    Re-lock: if, over the last `persist_s` seconds, most readings disagree with
    the filter but agree with EACH OTHER (consistent level or steady rise), nothing
    is moving at the marker and the road also shows water, the change is real
    (e.g. a fast rise, or shallow water that was missed at first) and the filter
    re-locks onto it. A parked car does not pass the road-water check.

    Image-quality contingency: an unreliable frame (night without detail, glare
    on the marker, smoke, frozen / lost video) is never used. The filter holds
    the last level (short gaps keep the rise rate, longer gaps freeze it) and
    after `hold_s` seconds the data is reported as not valid. Poor-but-usable
    images get a larger measurement noise, so they move the level less.
    """

    def __init__(self, args):
        self.r = args.meas_std ** 2
        self.q = args.accel_std ** 2
        self.gate = args.gate_sigma
        self.min_jump = args.min_jump
        self.motion_thresh = args.motion_thresh
        self.persist_s = args.persist_s
        self.hold_s = args.hold_s
        self.last_good_t = None
        self.max_rate = args.max_rate          # m/s - faster "rises" are never extrapolated
        self.relock_max_resid = args.relock_max_resid
        self.hold_extrapolate_s = args.hold_extrapolate_s
        self.adapt_thresh = 0.8  # |mean normalized innovation| that means "filter is lagging"
        self.adapt_factor = 50.0 # process-noise boost while lagging
        self.kf = None
        self.t_prev = None
        self.anomaly_since = None
        self.anomaly_hist = collections.deque()
        self.recent = collections.deque()     # (t, z, anomalous, moving) of reliable readings
        self.bias = 0.0          # running mean of the signed, normalized innovation

    def reset(self):
        self.kf = None
        self.anomaly_since = None
        self.anomaly_hist.clear()
        self.recent.clear()
        self.bias = 0.0

    def _init(self, z: float, t: float, v: float = 0.0):
        kf = cv2.KalmanFilter(2, 1, 0, cv2.CV_32F)
        kf.measurementMatrix = np.array([[1, 0]], np.float32)
        kf.measurementNoiseCov = np.array([[self.r]], np.float32)
        kf.statePost = np.array([[z], [v]], np.float32)
        kf.errorCovPost = np.diag([self.r, 1e-4]).astype(np.float32)
        self.kf, self.t_prev = kf, t
        self.anomaly_since = None
        self.anomaly_hist.clear()

    def _consistent_change(self, t: float, support: bool):
        """Readings of the last persist_s seconds: mostly anomalous, consistent, no motion?"""
        if not self.recent or t - self.recent[0][0] < 0.8 * self.persist_s:
            return None
        arr = np.array([(a, b, c, d) for a, b, c, d in self.recent], np.float64)
        if arr[:, 2].mean() < 0.7 or arr[:, 3].mean() > 0.1:
            return None
        sel = arr[(arr[:, 2] > 0) & (arr[:, 3] == 0)]
        if len(sel) < 5:
            return None
        A = np.vstack([sel[:, 0] - t, np.ones(len(sel))]).T
        (slope, z_now), *_ = np.linalg.lstsq(A, sel[:, 1], rcond=None)
        resid = float(np.std(sel[:, 1] - A @ np.array([slope, z_now])))
        if resid > self.relock_max_resid or abs(slope) > self.max_rate:
            return None
        if z_now > 0.10 and not support:          # road shows no water -> e.g. parked car
            return None
        return float(np.clip(z_now, 0, MARKER_MAX_M * 1.1)), float(slope)

    def step(self, z: float, t: float, motion_ratio: float, reliable: bool = True,
             why: str | None = None, quality: float = 1.0, support: bool = True) -> FilterResult:
        if not reliable:
            return self._hold(t, why)
        self.last_good_t = t
        if self.kf is None:
            self._init(z, t)
            return FilterResult(z, 0.0, False, None, 0.0)
        dt = float(np.clip(t - self.t_prev, 1e-3, 5.0))
        self.t_prev = t
        kf = self.kf
        kf.transitionMatrix = np.array([[1, dt], [0, 1]], np.float32)
        # Adaptive process noise: if the measurements keep landing on the same side of
        # the prediction (water started / stopped rising), let the filter catch up faster.
        q = self.q * (self.adapt_factor if abs(self.bias) > self.adapt_thresh else 1.0)
        kf.processNoiseCov = (q * np.array([[dt ** 4 / 4, dt ** 3 / 2],
                                            [dt ** 3 / 2, dt ** 2]])).astype(np.float32)
        r = self.r / max(quality, 0.25) ** 2      # poorer image -> trust each reading less
        kf.measurementNoiseCov = np.array([[r]], np.float32)
        pred = float(kf.predict()[0, 0])          # also copies prediction to statePost
        S = float(kf.errorCovPre[0, 0]) + r
        innov = z - pred
        sigma = abs(innov) / np.sqrt(S)

        reasons = []
        if motion_ratio > self.motion_thresh:
            reasons.append("moving object at marker")
        if sigma > self.gate and abs(innov) > self.min_jump:
            reasons.append("sudden spike (rise)" if innov > 0 else "sudden drop")

        self.recent.append((t, z, bool(reasons), motion_ratio > self.motion_thresh))
        while self.recent and t - self.recent[0][0] > self.persist_s:
            self.recent.popleft()
        if not reasons:
            self.bias = 0.9 * self.bias + 0.1 * float(innov / np.sqrt(S))
            kf.correct(np.array([[z]], np.float32))
            self.anomaly_since = None
            self.anomaly_hist.clear()
            anomaly, reason = False, None
        else:
            if self.anomaly_since is None:
                self.anomaly_since = t
            self.anomaly_hist.append((t, z))
            while self.anomaly_hist and t - self.anomaly_hist[0][0] > 3.0:
                self.anomaly_hist.popleft()
            relock = self._consistent_change(t, support) if motion_ratio <= self.motion_thresh else None
            if relock is not None:
                LOG.info("Consistent level change for %.0fs without motion -> re-locking at %.2f m",
                         self.persist_s, relock[0])
                self._init(relock[0], t, relock[1])
                self.recent.clear()
                anomaly, reason = False, None
            else:
                # keep predicting with the current rise rate (flood keeps rising while a car
                # passes), but never extrapolate an implausible rate
                kf.statePost[1, 0] = float(np.clip(kf.statePost[1, 0], -self.max_rate, self.max_rate))
                anomaly, reason = True, " + ".join(reasons)

        level = float(np.clip(kf.statePost[0, 0], 0.0, MARKER_MAX_M * 1.1))
        kf.statePost[0, 0] = level
        rate = float(kf.statePost[1, 0]) * 60.0
        return FilterResult(level, rate, anomaly, reason, float(sigma))

    def _hold(self, t: float, why: str | None) -> FilterResult:
        """No usable measurement: keep the last level (contingency)."""
        why = why or "image not usable"
        if self.kf is None:
            return FilterResult(0.0, 0.0, True, f"no data yet: {why}", 0.0, valid=False)
        held = t - (self.last_good_t if self.last_good_t is not None else t)
        dt = float(np.clip(t - self.t_prev, 1e-3, 5.0))
        self.t_prev = t
        kf = self.kf
        if held > self.hold_extrapolate_s:
            kf.statePost[1, 0] = 0.0                # long gap: stop extrapolating
        else:
            kf.statePost[1, 0] = float(np.clip(kf.statePost[1, 0], -self.max_rate, self.max_rate))
        kf.transitionMatrix = np.array([[1, dt], [0, 1]], np.float32)
        kf.processNoiseCov = (self.q * np.array([[dt ** 4 / 4, dt ** 3 / 2],
                                                 [dt ** 3 / 2, dt ** 2]])).astype(np.float32)
        kf.predict()
        level = float(np.clip(kf.statePost[0, 0], 0.0, MARKER_MAX_M * 1.1))
        kf.statePost[0, 0] = level
        return FilterResult(level, float(kf.statePost[1, 0]) * 60.0, True, f"data held: {why}", 0.0,
                            valid=held <= self.hold_s, held_s=held)
