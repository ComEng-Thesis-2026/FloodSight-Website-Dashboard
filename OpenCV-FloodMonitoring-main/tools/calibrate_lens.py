#!/usr/bin/env python3
"""
Lens calibration with a printed checkerboard (once per camera + lens + resolution).

1. Make and print a board (check the printed square size with a ruler):
       python tools/calibrate_lens.py --make-board board.png --cols 9 --rows 6 --square 0.025
   Glue it flat on stiff cardboard.

2. Calibrate from the live camera (same camera settings as FloodMonitoring.py):
       python tools/calibrate_lens.py --camera dahua --square 0.025 --out lens_CAM-01.json
   Hold the board in front of the camera at many positions: centre, all four
   corners and edges of the picture, tilted left / right / up / down, near and far.
   Views are captured automatically when the board is found at a new position
   (or press SPACE). Press C to calibrate when 15-25 views are collected,
   U to toggle the undistorted preview, Q to quit.

   Or from photos taken with the camera:
       python tools/calibrate_lens.py --images "calib/*.jpg" --square 0.025 --out lens_CAM-01.json

3. Use it:   python FloodMonitoring.py --lens lens_CAM-01.json
   (saved into the setup with 's'; re-fit the ROIs and re-capture the dry
   references afterwards, because the picture geometry changes).

A good calibration has an RMS error below about 0.5 px.
"""

import argparse
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from floodmon.lens import board_object_points, calibrate, find_board, max_edge_shift_px  # noqa: E402


def make_board(path, cols, rows, square_m, dpi=300):
    """Board with cols x rows INNER corners = (cols+1) x (rows+1) squares, printable at `dpi`."""
    px = int(round(square_m / 0.0254 * dpi))
    margin = px
    w, h = (cols + 1) * px + 2 * margin, (rows + 1) * px + 2 * margin
    img = np.full((h, w), 255, np.uint8)
    for r in range(rows + 1):
        for c in range(cols + 1):
            if (r + c) % 2 == 0:
                img[margin + r * px:margin + (r + 1) * px, margin + c * px:margin + (c + 1) * px] = 0
    cv2.putText(img, f"{cols}x{rows} inner corners, square {square_m * 1000:.1f} mm - print at 100% ({dpi} dpi)",
                (margin, h - margin // 3), cv2.FONT_HERSHEY_SIMPLEX, px / 120, 0, max(1, px // 60))
    cv2.imwrite(path, img)
    print(f"Board saved to {path}: {w}x{h} px = {w / dpi * 2.54:.1f} x {h / dpi * 2.54:.1f} cm at {dpi} dpi.")
    print("Print at 100 % (no 'fit to page'), then MEASURE one square and use that value for --square.")


def report_and_save(objs, imgs, size, out, camera):
    lens, per_view = calibrate(objs, imgs, size, camera)
    print(f"\nCalibrated from {len(objs)} views at {size[0]}x{size[1]}: RMS reprojection error {lens.rms_px:.3f} px")
    worst = np.argsort(per_view)[::-1][:3]
    print("Worst views:", ", ".join(f"#{i} {per_view[i]:.2f}px" for i in worst))
    print(f"Distortion: k1={lens.D[0]:+.4f} k2={lens.D[1]:+.4f}  -> up to "
          f"{max_edge_shift_px(lens, *size):.1f} px of bending at the picture edges")
    if lens.rms_px > 0.8:
        print("WARNING: high error - use a flatter board, better light, and remove blurry views.")
    lens.save(out)
    print(f"Saved {out}.  Use:  python FloodMonitoring.py --lens {out}")
    return lens


def from_images(pattern_glob, pattern, obj, out, camera):
    files = sorted(glob.glob(pattern_glob))
    if not files:
        raise SystemExit(f"No images match {pattern_glob}")
    objs, imgs, size = [], [], None
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        s = (img.shape[1], img.shape[0])
        if size is None:
            size = s
        elif s != size:
            print(f"skip {f}: size {s} differs from {size}")
            continue
        corners = find_board(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), pattern)
        print(f"{'ok  ' if corners is not None else 'none'} {f}")
        if corners is not None:
            objs.append(obj)
            imgs.append(corners)
    report_and_save(objs, imgs, size, out, camera)


def live(args, pattern, obj):
    import FloodMonitoring  # noqa: F401  (CAMERA SETTINGS block)
    from floodmon.video import build_source
    from floodmon.config import parse_args
    src_args = parse_args([*(["--camera", args.camera] if args.camera else []),
                           *(["--device", str(args.device)] if args.device is not None else []),
                           *(["--source", args.source] if args.source else [])], FloodMonitoring.CAMERA)
    source = build_source(src_args, FloodMonitoring.CAMERA)
    objs, imgs, centers, size, lens, show_und = [], [], [], None, None, False
    win = "Lens calibration"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    und = None
    try:
        while True:
            ok, frame, t, eos = source.read(timeout=5)
            if eos:
                break
            if not ok:
                continue
            size = (frame.shape[1], frame.shape[0])
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            sc = min(1.0, 960 / gray.shape[1])                 # detect on a smaller copy (fast)
            small = cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc < 1 else gray
            ok_b, c_small = cv2.findChessboardCornersSB(small, pattern)
            view = und(frame) if (show_und and und is not None) else frame.copy()
            key = cv2.waitKey(1) & 0xFF
            if ok_b:
                cv2.drawChessboardCorners(view, pattern, c_small / sc, True)
                c = c_small.reshape(-1, 2) / sc
                center = np.r_[c.mean(axis=0) / np.array(size), np.ptp(c[:, 0]) / size[0]]
                new = all(np.linalg.norm(center - p) > 0.08 for p in centers)
                if (args.auto and new) or key == ord(" "):
                    corners = find_board(gray, pattern)          # full-resolution sub-pixel corners
                    if corners is not None:
                        objs.append(obj)
                        imgs.append(corners)
                        centers.append(center)
                        print(f"view {len(objs)} captured")
            cv2.putText(view, f"views {len(objs)}  (SPACE capture, C calibrate, U undistort, Q quit)",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
            cv2.imshow(win, view)
            if key in (ord("q"), 27):
                break
            if key in (ord("c"), ord("C")):
                if len(objs) < 5:
                    print("Need at least 5 views (15-25 recommended).")
                else:
                    lens = report_and_save(objs, imgs, size, args.out, args.camera_name)
                    und = lens.undistorter(*size)
            if key in (ord("u"), ord("U")):
                show_und = not show_und
    finally:
        source.release()
        cv2.destroyAllWindows()
    if lens is None and len(objs) >= 5:
        report_and_save(objs, imgs, size, args.out, args.camera_name)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cols", type=int, default=9, help="inner corners across")
    p.add_argument("--rows", type=int, default=6, help="inner corners down")
    p.add_argument("--square", type=float, default=0.025, help="square size (m) as printed")
    p.add_argument("--make-board", metavar="PNG", help="write a printable board and exit")
    p.add_argument("--images", help="calibrate from photos (glob pattern)")
    p.add_argument("--camera", choices=["laptop", "iriun", "dahua", "file"])
    p.add_argument("--device", type=int)
    p.add_argument("--source")
    p.add_argument("--no-auto", dest="auto", action="store_false", help="capture only with SPACE")
    p.add_argument("--out", default="lens.json")
    p.add_argument("--camera-name", default="", help="stored in the file (e.g. CAM-01 Dahua 2.8mm)")
    a = p.parse_args(argv)
    pattern = (a.cols, a.rows)
    if a.make_board:
        make_board(a.make_board, a.cols, a.rows, a.square)
        return
    obj = board_object_points(pattern, a.square)
    if a.images:
        from_images(a.images, pattern, obj, a.out, a.camera_name)
    else:
        live(a, pattern, obj)


if __name__ == "__main__":
    main()
