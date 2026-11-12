"""
Unit tests for the measuring chain.

Run all tests from the project folder:
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from floodmon.bands import (FLOOD_BANDS, MARKER_MAX_M, StatusTracker, band_boundaries,  # noqa: E402
                            classify_level, is_above_range)
from floodmon.calibration import Calibration, SetupError, validate_setup  # noqa: E402
from floodmon.config import parse_args  # noqa: E402
from floodmon.geometry import MarkerGeometry  # noqa: E402
from floodmon.kalman import FloodKalman  # noqa: E402
from floodmon.quality import RefImage, robust_photometric  # noqa: E402
from floodmon.synthetic import Scene, preset  # noqa: E402


def default_args(*extra):
    return parse_args(["--no-display", *extra])


class TestBands(unittest.TestCase):
    def test_thesis_thresholds(self):
        cases = [(0.0, "SAFE"), (0.2999, "SAFE"), (0.30, "ALERT"), (0.4999, "ALERT"),
                 (0.50, "DANGER"), (0.99, "DANGER"), (1.3, "DANGER")]
        for level, status in cases:
            self.assertEqual(classify_level(level), status, level)
        self.assertEqual(band_boundaries(), [0.30, 0.50])

    def test_bands_are_contiguous(self):
        self.assertEqual(FLOOD_BANDS[0].from_m, 0.0)
        for a, b in zip(FLOOD_BANDS, FLOOD_BANDS[1:]):
            self.assertAlmostEqual(a.to_m, b.from_m)
        self.assertAlmostEqual(FLOOD_BANDS[-1].to_m, MARKER_MAX_M)

    def test_hysteresis(self):
        tr = StatusTracker(0.02)
        self.assertEqual(tr.update(0.30), "ALERT")          # rising: immediate
        self.assertEqual(tr.update(0.29), "ALERT")          # inside the hysteresis
        self.assertEqual(tr.update(0.279), "SAFE")          # clearly below
        self.assertEqual(tr.update(0.55), "DANGER")

    def test_above_range(self):
        self.assertFalse(is_above_range(0.90))
        self.assertTrue(is_above_range(MARKER_MAX_M))
        self.assertTrue(is_above_range(1.08))


class TestGeometry(unittest.TestCase):
    def test_heights_through_perspective(self):
        sc = Scene(preset("physical"))
        mg = MarkerGeometry(sc.setup()["marker_roi"], 1.0)
        for h in (0.05, 0.3, 0.5, 0.75, 0.95):
            x, y = sc.wall_point(0.075, h)                   # exact image point of that height
            self.assertAlmostEqual(mg.to_height(x, y), h, delta=0.002)
            p = mg.to_img(0.5, h)
            self.assertAlmostEqual(mg.to_height(p[0], p[1]), h, places=6)

    def test_resolution(self):
        sc = Scene(preset("physical"))
        mg = MarkerGeometry(sc.setup()["marker_roi"], 1.0)
        r0, r1 = mg.cm_per_px_at(0.0), mg.cm_per_px_at(0.9)
        self.assertTrue(0.3 < r1 < r0 < 1.0, (r0, r1))       # camera looks down: bottom is coarser


class TestSetupFile(unittest.TestCase):
    def good(self):
        return dict(camera_id="CAM", frame_size=[960, 540], flood_roi=[[0, 0], [10, 0], [10, 10]],
                    marker_roi=[[0, 10], [5, 10], [5, 0], [0, 0]], ref_height_m=1.0)

    def test_valid(self):
        self.assertEqual(validate_setup(self.good()), [])

    def test_problems_are_reported(self):
        d = self.good()
        d["marker_roi"] = [[0, 0], [1, 1], [2, 2]]
        d["ref_height_m"] = 12
        probs = validate_setup(d)
        self.assertTrue(any("marker_roi" in p for p in probs))
        self.assertTrue(any("ref_height_m" in p for p in probs))

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "flood_config.json")
            with open(p, "w") as f:
                f.write('{"camera_id": "CAM", "flood_roi": [[0,0],[1,1],}')
            with self.assertRaises(SetupError) as cm:
                Calibration.load(p)
            self.assertIn("not valid JSON", str(cm.exception))

    def test_roundtrip_and_scaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "flood_config.json")
            cal = Calibration(**self.good())
            cal.tag_anchors = {"0": [[100, 100], [110, 100], [110, 110], [100, 110]]}
            cal.save(p)
            back = Calibration.load(p)
            self.assertEqual(back.marker_roi, cal.marker_roi)
            half = back.scaled_to(480, 270)
            self.assertEqual(half.tag_anchors["0"][0], [50.0, 50.0])
            self.assertEqual(half.marker_mode, "physical")


class TestKalman(unittest.TestCase):
    def setUp(self):
        self.args = default_args()

    def run_series(self, kf, levels, dt=0.1, motion=None, support=True):
        out = []
        for i, z in enumerate(levels):
            m = motion[i] if motion is not None else 0.0
            out.append(kf.step(z, i * dt, m, support=support))
        return out

    def test_follows_a_rise(self):
        rng = np.random.default_rng(0)
        t = np.arange(600) * 0.1
        true = np.clip((t - 5) * 0.01, 0, None)
        res = self.run_series(FloodKalman(self.args), true + rng.normal(0, 0.01, t.size))
        err = np.abs(np.array([r.level_m for r in res]) - true)[100:]
        self.assertLess(err.mean(), 0.01)

    def test_vehicle_spike_is_rejected(self):
        levels = [0.2] * 100 + [0.7] * 10 + [0.2] * 50
        motion = [0.0] * 100 + [0.6] * 10 + [0.0] * 50
        res = self.run_series(FloodKalman(self.args), levels, motion=motion)
        self.assertTrue(all(r.anomaly for r in res[100:110]))
        self.assertLess(max(r.level_m for r in res), 0.25)

    def test_parked_car_is_not_accepted(self):
        # a car stops in front of the marker: high reading, no motion, but the road is dry
        levels = [0.05] * 100 + [0.60] * 400
        res = self.run_series(FloodKalman(self.args), levels, support=False)
        self.assertLess(res[-1].level_m, 0.15)

    def test_real_fast_rise_is_accepted(self):
        levels = [0.05] * 100 + [0.40] * 300
        res = self.run_series(FloodKalman(self.args), levels, support=True)
        self.assertAlmostEqual(res[-1].level_m, 0.40, delta=0.03)

    def test_hold_then_invalid(self):
        kf = FloodKalman(self.args)
        kf.step(0.3, 0.0, 0.0)
        r = kf.step(0.0, 60.0, 0.0, reliable=False, why="glare")
        self.assertTrue(r.valid)
        self.assertAlmostEqual(r.level_m, 0.3, delta=0.01)
        r = kf.step(0.0, self.args.hold_s + 1.0, 0.0, reliable=False, why="glare")
        self.assertFalse(r.valid)


class TestBrightness(unittest.TestCase):
    def test_mapping_ignores_flooded_static_pixels(self):
        rng = np.random.default_rng(1)
        ref = rng.uniform(20, 230, 20000).astype(np.float32)
        cur = 0.9 * ref + 5 + rng.normal(0, 1.5, ref.size)
        water = rng.random(ref.size) < 0.45                 # 45 % of the "static" scene flooded
        cur[water] = rng.normal(95, 8, water.sum())
        g, off, share, _ = robust_photometric(cur, ref, tol=4.0)
        self.assertAlmostEqual(g, 0.9, delta=0.02)
        self.assertAlmostEqual(off, 5.0, delta=2.0)
        self.assertAlmostEqual(share, 0.55, delta=0.05)

    def test_reference_adaptation_is_not_rounded_away(self):
        ref = RefImage(np.full((40, 40, 3), 100, np.uint8), "day")
        frame = np.full((40, 40, 3), 110, np.float32)
        for _ in range(100):
            ref = RefImage(cv2.addWeighted(ref.bgr_f, 0.97, frame, 0.03, 0), "day")
        self.assertGreaterEqual(int(ref.bgr[0, 0, 0]), 109)


class TestTags(unittest.TestCase):
    def test_fit_and_realign(self):
        from floodmon.tags import TagAligner, TagDetector, fit_marker_from_tags
        sc = Scene(preset("tags"))
        dry, _ = sc.render(5.0)
        det = TagDetector().detect(dry)
        self.assertEqual(sorted(det), [0, 1, 2, 3, 4])
        fit = fit_marker_from_tags(sc.tag_layout(), det, 0.15, 1.0)
        err = np.abs(np.array(fit["quad"]) - np.array(sc.setup()["marker_roi"])).max()
        self.assertLess(err, 1.5)
        al = TagAligner({k: v.tolist() for k, v in det.items()}, TagDetector(), smooth=1)
        bumped, truth = sc.render(35.0)                       # camera shifted (14, 8) px
        self.assertTrue(truth["shake"])
        out, info = al.update(bumped)
        self.assertTrue(info["corrected"])
        after = TagDetector().detect(out)
        resid = max(np.abs(after[k] - det[k]).max() for k in after)
        self.assertLess(resid, 0.6)


class TestLens(unittest.TestCase):
    def test_calibration_recovers_the_camera(self):
        from floodmon.lens import board_object_points, calibrate
        K = np.array([[800.0, 0, 480], [0, 800.0, 270], [0, 0, 1]])
        D = np.array([-0.28, 0.09, 0.0, 0.0, 0.0])
        obj = board_object_points((9, 6), 0.03)
        rng = np.random.default_rng(3)
        objs, imgs = [], []
        for _ in range(15):
            rvec = rng.normal(0, 0.35, 3)
            tvec = np.array([rng.uniform(-0.15, 0.05), rng.uniform(-0.1, 0.05), rng.uniform(0.45, 0.8)])
            pts, _ = cv2.projectPoints(obj, rvec, tvec, K, D)
            objs.append(obj)
            imgs.append((pts + rng.normal(0, 0.1, pts.shape)).astype(np.float32))
        lens, per_view = calibrate(objs, imgs, (960, 540))
        self.assertLess(lens.rms_px, 0.3)
        self.assertAlmostEqual(lens.K[0, 0], 800, delta=8)
        self.assertAlmostEqual(lens.D[0], -0.28, delta=0.03)
        und = lens.undistorter(960, 540)
        self.assertEqual(und(np.zeros((540, 960, 3), np.uint8)).shape, (540, 960, 3))


class TestEvaluation(unittest.TestCase):
    def test_metrics(self):
        from floodmon.evaluation import evaluate
        truth = [dict(t_s=i / 10, level_m=i / 1000) for i in range(800)]
        perfect = [dict(t_s=g["t_s"], water_level_m=g["level_m"], raw_level_m=g["level_m"],
                        status=classify_level(g["level_m"]), data_valid=1) for g in truth]
        rep = evaluate(perfect, truth)
        self.assertEqual(rep["filtered"]["mae_cm"], 0.0)
        self.assertEqual(rep["status_accuracy_pct"], 100.0)
        biased = [dict(r, water_level_m=r["water_level_m"] + 0.01) for r in perfect]
        rep = evaluate(biased, truth)
        self.assertAlmostEqual(rep["filtered"]["bias_cm"], 1.0, places=2)
        sparse = [dict(t_s=0.0, level_m=0.0), dict(t_s=79.9, level_m=0.799)]      # tape readings
        self.assertEqual(evaluate(perfect, sparse)["filtered"]["mae_cm"], 0.0)


class TestCsvLog(unittest.TestCase):
    def test_flushed_while_running(self):
        from types import SimpleNamespace
        from floodmon.feed import CSV_COLUMNS, CsvLogger
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.csv")
            log = CsvLogger(path, flush_s=0.0)
            payload = dict(timestamp="2026-01-01T00:00:00", t_s=0.0, water_level_m=0.1, raw_level_m=0.1,
                           status="SAFE", flood_coverage_pct=1.0, motion_ratio=0.0, confidence=1.0)
            meas = SimpleNamespace(method="scan", scan_level_m=0.1, step_level_m=None, resolution_cm_per_px=0.6)
            res = SimpleNamespace(anomaly=False, reason=None, valid=True)
            q = SimpleNamespace(score=1.0, mode="GOOD", lighting="day", conditions=[])
            log.write(payload, meas, res, q)
            with open(path, encoding="utf-8") as f:          # readable before close
                lines = f.read().splitlines()
            self.assertEqual(lines[0].split(","), CSV_COLUMNS)
            self.assertEqual(len(lines), 2)
            log.close()


class TestEndToEnd(unittest.TestCase):
    """A short synthetic flood through the full Monitor pipeline (about 20 s)."""

    def test_synthetic_flood(self):
        from floodmon.pipeline import Monitor
        cfg = preset("quick")
        sc = Scene(cfg)
        setup = sc.setup()
        cal = Calibration(**setup)
        args = default_args()
        mon = Monitor(args, cal, (cfg.height, cfg.width, 3), source_kind="file")
        mon.det.add_reference(sc.dry_reference())
        errs, statuses = [], []
        payload = None
        for frame, truth in sc.frames():
            r = mon.process(frame, truth["t_s"])
            payload = r.payload
            if truth["t_s"] >= 3.0:
                errs.append(r.res.level_m - truth["level_m"])
                statuses.append(r.status == truth["status"])
        errs = np.abs(errs)
        self.assertLess(errs.mean(), 0.015, f"MAE {errs.mean() * 100:.2f} cm")
        self.assertLess(errs.max(), 0.06)
        self.assertGreater(np.mean(statuses), 0.9)
        for key in ("above_range", "resolution_cm_per_px", "marker_mode", "t_s", "data_valid"):
            self.assertIn(key, payload)
        json.dumps(payload)                                    # website format must be JSON


if __name__ == "__main__":
    unittest.main()


class TestLensEndToEnd(unittest.TestCase):
    """A wide-angle (barrel distorted) camera: frames are undistorted before measuring."""

    def test_distorted_flood(self):
        from floodmon.lens import LensModel
        from floodmon.pipeline import Monitor
        cfg = preset("quick", duration_s=20.0, rise_end_s=18.0, peak_m=0.45, fall_to_m=0.45)
        sc = Scene(cfg)
        W, H = cfg.width, cfg.height
        K = sc.K.copy()
        D = np.array([-0.25, 0.08, 0.0, 0.0, 0.0])
        xs, ys = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
        ideal = cv2.undistortPoints(np.dstack([xs, ys]).reshape(-1, 1, 2), K, D, P=K).reshape(H, W, 2)

        def distort(img):                                   # what the wide-angle camera delivers
            return cv2.remap(img, ideal[..., 0], ideal[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        lens = LensModel(camera_matrix=K.tolist(), dist_coeffs=D.tolist(), image_size=[W, H])
        und = lens.undistorter(W, H)
        new_K = und.new_K

        def to_view(p):                                     # ideal pixel -> undistorted view pixel
            v = new_K @ np.linalg.inv(K) @ np.array([p[0], p[1], 1.0])
            return [float(v[0] / v[2]), float(v[1] / v[2])]

        setup = sc.setup()
        setup["marker_roi"] = [to_view(p) for p in setup["marker_roi"]]
        setup["flood_roi"] = [to_view(p) for p in setup["flood_roi"]]
        mon = Monitor(default_args(), Calibration(**setup), (H, W, 3), source_kind="file", lens=lens)
        mon.det.add_reference(und(distort(sc.dry_reference())))
        errs = []
        for frame, truth in sc.frames():
            r = mon.process(distort(frame), truth["t_s"])
            if truth["t_s"] >= 3.0:
                errs.append(abs(r.res.level_m - truth["level_m"]))
        self.assertLess(float(np.mean(errs)), 0.02, f"MAE {np.mean(errs) * 100:.2f} cm")


class TestOfflineFeed(unittest.TestCase):
    def test_offline_payload_and_line(self):
        from floodmon.feed import TerminalFeed, offline_payload
        cal = Calibration(camera_id="CAM-01")
        d = offline_payload(cal, "dahua", 12.0, 0.42)
        self.assertEqual(d["status"], "CAMERA_OFFLINE")
        self.assertFalse(d["data_valid"])
        line = TerminalFeed(1.0, json_mode=False, color=False).format_line(d, "CAMERA OFFLINE")
        self.assertIn("CAMERA OFFLINE", line)
        self.assertIn("0.42", line)
        json.dumps(d)


class TestDahuaAddress(unittest.TestCase):
    def test_settings_and_overrides(self):
        from floodmon.video import CameraSettings, dahua_url, mask_url
        cam = CameraSettings(mode="dahua", dahua_ip="10.0.0.5", dahua_password="p@ss:1", dahua_subtype=1)
        old = os.environ.pop("DAHUA_PASSWORD", None)
        try:
            url = dahua_url(parse_args([]), cam)
            self.assertEqual(url, "rtsp://admin:p%40ss%3A1@10.0.0.5:554/cam/realmonitor?channel=1&subtype=1")
            self.assertNotIn("p%40ss", mask_url(url))
            url = dahua_url(parse_args(["--dahua-ip", "10.0.0.9", "--dahua-subtype", "0"]), cam)
            self.assertIn("@10.0.0.9:554", url)
            self.assertTrue(url.endswith("subtype=0"))
        finally:
            if old is not None:
                os.environ["DAHUA_PASSWORD"] = old
