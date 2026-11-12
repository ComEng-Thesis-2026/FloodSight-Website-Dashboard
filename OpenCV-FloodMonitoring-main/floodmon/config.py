"""
All settings in one place.

Every tuning value of the program is a command-line option with its default
here, including thresholds that used to be hidden inside the code. The full
set is written next to every log / recording (settings sidecar), so each
experiment can be reproduced exactly.
"""

from __future__ import annotations

import argparse
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser(cam=None) -> argparse.ArgumentParser:
    mode = getattr(cam, "mode", "laptop")
    p = argparse.ArgumentParser(description="CCTV-based flood monitoring (OpenCV + Kalman filter)")

    src = p.add_argument_group("video source (default: CAMERA_MODE in the CAMERA SETTINGS block)")
    src.add_argument("--camera", choices=["laptop", "iriun", "dahua", "file"],
                     help=f"camera to use (overrides CAMERA_MODE = '{mode}')")
    src.add_argument("--list-cameras", action="store_true", help="probe local camera indexes and exit")
    src.add_argument("--dahua-ip", help="Dahua camera / NVR IP address (default: CAMERA SETTINGS)")
    src.add_argument("--dahua-user", default=None)
    src.add_argument("--dahua-password", default=None, help="or set environment variable DAHUA_PASSWORD")
    src.add_argument("--dahua-port", type=int, default=None)
    src.add_argument("--dahua-channel", type=int, default=None)
    src.add_argument("--dahua-subtype", type=int, default=None, help="0 = main stream, 1 = sub stream")
    src.add_argument("--device", type=int, help="any local camera index (overrides --camera)")
    src.add_argument("--source", help="video file path or any RTSP/HTTP stream URL (overrides --camera)")
    src.add_argument("--loop", action="store_true", help="loop a video file")
    src.add_argument("--fast", action="store_true", help="process a video file as fast as possible")
    src.add_argument("--file-start", help="video files: wall-clock time of the first frame (ISO, e.g. "
                     "2026-11-08T14:00:00+08:00). Default: recording sidecar, else file time - duration")
    src.add_argument("--offline-s", type=float, default=5.0,
                     help="live camera: report CAMERA_OFFLINE after this many seconds without frames")

    cal = p.add_argument_group("setup / calibration")
    cal.add_argument("--config", default=os.path.join(PROJECT_DIR, "flood_config.json"))
    cal.add_argument("--camera-id", help="camera name shown in the data feed")
    cal.add_argument("--flood-roi", "--roi", dest="flood_roi",
                     help="x,y,w,h rectangle or x1,y1,x2,y2,x3,y3,... polygon (overrides config)")
    cal.add_argument("--marker-roi", help="x,y,w,h rectangle or 4 corners x1,y1,...,x4,y4 (overrides config)")
    cal.add_argument("--marker", help="(old format) base_x,base_y,top_x,top_y")
    cal.add_argument("--ref-height", type=float, help="real height (m) of the marker ROI's top edge")
    cal.add_argument("--marker-width", type=float, help="real width (m) of the marker surface (distance estimate)")
    cal.add_argument("--hfov", type=float, default=None, help="lens horizontal field of view (deg)")
    cal.add_argument("--reference", help="dry reference image (overrides config)")
    cal.add_argument("--lens", help="lens calibration file from tools/calibrate_lens.py (overrides config)")
    cal.add_argument("--no-undistort", action="store_true", help="ignore the lens calibration")
    cal.add_argument("--tags", help="ArUco tag layout JSON (overrides the setup's tag_layout)")
    cal.add_argument("--no-tags", action="store_true", help="do not use ArUco anchor tags")
    cal.add_argument("--tag-max-err", type=float, default=1.5,
                     help="tags: largest corner error (px) accepted for re-alignment")

    det = p.add_argument_group("flood detection tuning")
    det.add_argument("--method", choices=["auto", "reference", "color"], default="auto")
    det.add_argument("--diff-thresh", type=float, default=22.0, help="Lab color distance for water vs dry road")
    det.add_argument("--row-fraction", type=float, default=0.55, help="share of a marker row that must be water")
    det.add_argument("--gap-m", type=float, default=0.06, help="tolerated non-water gap (m) while scanning up")
    det.add_argument("--min-segment-m", type=float, default=0.0,
                     help="a water band above a gap must be at least this tall (m) to raise the level; "
                          "0 = off (stops thin false bands, e.g. gauge tick marks, pulling the level up)")
    det.add_argument("--mask-smooth", type=float, default=0.5, help="temporal smoothing of water mask (0-0.95)")
    det.add_argument("--min-object-area", type=float, default=0.003,
                     help="smallest moving object, as a fraction of the frame area")
    det.add_argument("--min-obstruction-height", type=float, default=0.10,
                     help="moving object must cover at least this much of the marker height (m)")
    det.add_argument("--obstruction-row-cover", type=float, default=0.3,
                     help="share of a marker row an object must cover to count as covering that row")
    det.add_argument("--flood-min-coverage", type=float, default=0.10,
                     help="road water share that counts as 'flood detected'")
    det.add_argument("--water-hsv-low", type=int, nargs=3, default=[5, 40, 40])
    det.add_argument("--water-hsv-high", type=int, nargs=3, default=[35, 255, 230])
    det.add_argument("--step-strength", type=float, default=6.0,
                     help="water-line step strength needed to use the step method")
    det.add_argument("--step-min-rows-m", type=float, default=0.03,
                     help="step method: shortest piece (m) on each side of the water line")
    det.add_argument("--unknown-row-frac", type=float, default=0.3,
                     help="a marker row with less usable pixels than this share is skipped (glare / black)")
    det.add_argument("--night-brightness", type=float, default=55.0,
                     help="mean grey level below which the scene counts as night")
    det.add_argument("--no-stabilize", action="store_true", help="turn off camera-shake compensation")
    det.add_argument("--percentile-mapping", action="store_true",
                     help="old brightness mapping from percentiles of the whole static scene (biased "
                          "when water or cars cover part of it; for comparison only)")
    det.add_argument("--no-auto-reference", action="store_true",
                     help="do not capture references for new lighting automatically")
    det.add_argument("--ref-adapt", type=float, default=0.002,
                     help="dry reference update speed while dry and empty (0 = off)")
    det.add_argument("--ref-adapt-every", type=int, default=15,
                     help="update the dry reference every N dry frames")
    det.add_argument("--capture-frames", type=int, default=15, help="frames averaged into a dry reference")
    det.add_argument("--wet-level", type=float, default=0.02, help="level (m) that counts as 'road wet'")
    det.add_argument("--wet-coverage", type=float, default=0.05, help="road water share that counts as 'wet'")
    det.add_argument("--dry-coverage", type=float, default=0.03, help="road water share below which it is 'dry'")
    det.add_argument("--trusted-dry-s", type=float, default=120.0,
                     help="a dry confirmation is trusted this long (auto reference capture)")
    det.add_argument("--frozen-s", type=float, default=2.0, help="identical frames for this long = FROZEN")
    det.add_argument("--moved-frac", type=float, default=0.05,
                     help="camera shift (share of frame width) that counts as 'camera moved'")
    det.add_argument("--moved-s", type=float, default=3.0, help="shift must last this long to count as moved")
    det.add_argument("--shake-px", type=float, default=1.5, help="shift (px) reported as CAMERA-SHAKE")

    kal = p.add_argument_group("Kalman filter / anomaly")
    kal.add_argument("--meas-std", type=float, default=0.015, help="measurement noise std (m)")
    kal.add_argument("--accel-std", type=float, default=0.002, help="process noise, level acceleration std (m/s^2)")
    kal.add_argument("--gate-sigma", type=float, default=3.0, help="innovation gate in standard deviations")
    kal.add_argument("--min-jump", type=float, default=0.06, help="smallest jump (m) treated as a spike")
    kal.add_argument("--motion-thresh", type=float, default=0.25, help="object share of marker zone = obstruction")
    kal.add_argument("--hold-s", type=float, default=120.0,
                     help="after this many seconds without a usable image the data is reported as not valid")
    kal.add_argument("--persist-s", type=float, default=10.0,
                     help="seconds a consistent level change (without motion) needs to be accepted")
    kal.add_argument("--max-rate", type=float, default=0.05, help="fastest believable level change (m/s)")
    kal.add_argument("--relock-max-resid", type=float, default=0.03,
                     help="re-lock: readings must agree with each other within this (m)")
    kal.add_argument("--relock-support", type=float, default=0.05,
                     help="re-lock above 0.10 m needs at least this road water share (stops parked cars)")
    kal.add_argument("--hold-extrapolate-s", type=float, default=10.0,
                     help="while holding, keep the rise rate for this long, then freeze the level")
    kal.add_argument("--status-hysteresis", type=float, default=0.02, help="status hysteresis (m)")

    out = p.add_argument_group("data feed / output")
    out.add_argument("--print-interval", type=float, default=1.0,
                     help="seconds between terminal lines (status changes print immediately)")
    out.add_argument("--json", action="store_true", help="print the feed as JSON lines")
    out.add_argument("--no-color", action="store_true", help="plain text without terminal colors")
    out.add_argument("--log-csv", help="append every reading to this CSV file (a settings sidecar is written too)")
    out.add_argument("--csv-flush-s", type=float, default=1.0, help="write the CSV to disk at least this often")
    out.add_argument("--record", metavar="DIR",
                     help="save the raw video in segments (with start-time sidecars) for re-analysis")
    out.add_argument("--record-minutes", type=float, default=10.0, help="length of one recording segment")
    out.add_argument("--max-width", type=int, default=960,
                     help="frames wider than this are scaled down for processing (0 = never)")
    out.add_argument("--keys", help="scripted key presses 'seconds:key,...' e.g. '1:x,6:x' "
                     "(video time; for tests and demos)")
    out.add_argument("--no-display", action="store_true", help="run without a video window")
    out.add_argument("--no-enhance", action="store_true",
                     help="show the raw picture (no brightening / dehazing of the display)")
    out.add_argument("--verbose", action="store_true")
    return p


def parse_args(argv=None, cam=None):
    args = build_parser(cam).parse_args(argv)
    if args.hfov is None:
        args.hfov = getattr(cam, "hfov_deg", 70.0)
    return args


def settings_dict(args) -> dict:
    """All settings as plain JSON values (passwords removed)."""
    d = {}
    for k, v in sorted(vars(args).items()):
        if "password" in k:
            v = "****" if v else v
        d[k] = list(v) if isinstance(v, tuple) else v
    return d
