"""
Main program: video loop, keys, window.

Keys in the video window (default ROIs are created if none exist)
----------------------------------------------------------------
  f : draw a new FLOOD ROI rectangle (ENTER) - then drag its vertices
  m : draw a new MARKER ROI rectangle (ENTER) - then drag its 4 corners onto
      the edges of the real post / wall. Bottom edge = road surface (0 m),
      top edge = REF HEIGHT.
  g : fit the MARKER ROI automatically from ArUco anchor tags (needs a tag
      layout and the marker width)
  x : VIRTUAL MARKER (2 steps: lock the geometry, then remove the physical
      marker)                                  Shift+X : unlock the marker
  [ / ] : REF HEIGHT  -/+ 0.05 m        , / . : REF HEIGHT  -/+ 0.01 m
  drag a square handle         : move a vertex / corner
  drag inside a region         : move the whole region
  right-click FLOOD ROI edge   : add a vertex     right-click vertex : remove it
  b : capture the DRY reference for the current lighting (day / night / IR).
      The scene is checked first (dry road, nothing moving); if it does not
      look dry, press b again within 5 s to capture anyway.
  e : picture enhancement on/off     w : water overlay on/off   v : mask debug view
  s : save setup     p : pause     q / ESC : quit
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

from .calibration import Calibration, SetupError, marker_quad_from_line, normalize_polygon, rect_to_polygon
from .config import parse_args, settings_dict
from .feed import CsvLogger, TerminalFeed, offline_payload, write_settings_sidecar
from .lens import LensModel, max_edge_shift_px
from .modes import ReferenceGuard
from .pipeline import Monitor
from .quality import SMALL, classify_lighting, enhance_for_display
from .recorder import SegmentRecorder
from .tags import load_layout
from .ui import MARKER_COLOR, SetupEditor, draw_overlay
from .video import CameraSettings, build_source, list_cameras

LOG = logging.getLogger("FloodMonitoring")


def _floats(text):
    return [float(v) for v in text.split(",")]


def _resolve(path: str | None, base_dir: str) -> str | None:
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


def load_setup(args) -> Calibration:
    try:
        cal = Calibration.load(args.config)
    except SetupError as e:
        raise SystemExit(f"ERROR: {e}") from None
    if args.camera_id:
        cal.camera_id = args.camera_id
    if args.ref_height:
        cal.ref_height_m = args.ref_height
    if args.marker_width:
        cal.marker_width_m = args.marker_width
    if args.reference:
        cal.reference_image = os.path.abspath(args.reference)
    return cal


def load_lens(args, cal: Calibration, cfg_dir: str) -> tuple[LensModel | None, str | None]:
    if args.no_undistort:
        return None, None
    path = os.path.abspath(args.lens) if args.lens else _resolve(cal.lens_file, cfg_dir)
    if not path:
        return None, None
    try:
        return LensModel.load(path), path
    except (OSError, ValueError) as e:
        raise SystemExit(f"ERROR: lens calibration {path}: {e}") from None


def load_tag_layout(args, cal: Calibration) -> dict | None:
    if args.no_tags:
        return None
    if args.tags:
        try:
            layout = load_layout(args.tags)
        except (OSError, ValueError) as e:
            raise SystemExit(f"ERROR: {e}") from None
        cal.tag_layout = layout
        return layout
    return cal.tag_layout


def main(argv=None, cam: CameraSettings | None = None):
    cam = cam or CameraSettings()
    args = parse_args(argv, cam)
    # Log messages go to stderr so the data feed on stdout stays clean.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.list_cameras:
        list_cameras(cam)
        return

    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    cal = load_setup(args)
    lens, lens_path = load_lens(args, cal, cfg_dir)
    tag_layout = load_tag_layout(args, cal)

    source = build_source(args, cam)
    LOG.info("Video source: %s", source.describe())
    if source.kind == "file" and source.start_time is not None:
        LOG.info("Timestamps follow the video: first frame = %s (%s)",
                 source.start_time.isoformat(timespec="seconds"), source.start_source)

    ok, frame, t, eos = source.read(timeout=15)
    while not ok and not eos and source.is_live:
        LOG.info("Waiting for the first frame ...")
        ok, frame, t, eos = source.read(timeout=10)
    if not ok:
        raise SystemExit("No video frames received.")
    if source.kind == "device":                   # webcams send dark frames while warming up
        t_end = time.monotonic() + 2.0
        while frame.mean() < 3 and time.monotonic() < t_end:
            ok2, f2, t2, _ = source.read(timeout=1.0)
            if ok2:
                frame, t = f2, t2
        if frame.mean() < 3:
            if "Iriun" in source.label:
                LOG.warning("Iriun camera gives a black image: open the Iriun app on the phone and on the "
                            "PC (same Wi-Fi or USB cable). Check IRIUN_CAMERA_INDEX with --list-cameras.")
            else:
                LOG.warning("%s gives a black image (lens covered or wrong index? try --list-cameras).",
                            source.describe())
    if args.max_width and frame.shape[1] > args.max_width:      # e.g. Dahua 4 MP main stream
        sc = args.max_width / frame.shape[1]
        frame = cv2.resize(frame, (args.max_width, int(round(frame.shape[0] * sc))), interpolation=cv2.INTER_AREA)
        LOG.info("Processing at %dx%d (camera frames are scaled down for speed)", frame.shape[1], frame.shape[0])
    H, W = frame.shape[:2]
    cal = cal.scaled_to(W, H)
    if args.flood_roi:                            # CLI coordinates refer to the actual frame
        cal.flood_roi = normalize_polygon(_floats(args.flood_roi))
    if args.marker_roi:
        q = normalize_polygon(_floats(args.marker_roi), as_int=False)
        if len(q) != 4:
            raise SystemExit("--marker-roi needs a rectangle x,y,w,h or exactly 4 corners")
        cal.marker_roi = q
    elif args.marker:
        m = _floats(args.marker)
        if len(m) != 4:
            raise SystemExit("--marker needs base_x,base_y,top_x,top_y")
        cal.marker_roi = marker_quad_from_line(m[:2], m[2:])
    if cal.fill_defaults(W, H):
        LOG.warning("No saved ROIs - default FLOOD ROI and MARKER ROI created. Drag them into place, "
                    "set the reference height ([ ]), then press 's'.")

    # ---- lens correction: the setup must be made on frames with the same correction ----
    if lens is not None:
        LOG.info("Lens correction ON (%s, calibration error %.2f px; up to %.0f px corrected at the edges)",
                 os.path.basename(lens_path), lens.rms_px, max_edge_shift_px(lens, W, H))
    if not cal.is_default and cal.ready and bool(lens) != bool(cal.lens_applied):
        LOG.warning("The saved setup was made %s lens correction, but it is now %s. Re-fit the ROIs, "
                    "capture new dry references (b) and save (s) - until then readings may be off.",
                    "WITH" if cal.lens_applied else "WITHOUT", "ON" if lens else "OFF")

    def save_setup():
        cal.frame_size = [W, H]
        cal.lens_applied = lens is not None
        if lens_path:
            rel = os.path.relpath(lens_path, cfg_dir)
            cal.lens_file = rel if not rel.startswith("..") else lens_path
        cal.save(args.config)

    mon = Monitor(args, cal, frame.shape, source_kind=source.kind, start_time=source.start_time,
                  lens=lens, tag_layout=tag_layout, save_setup=save_setup)
    det, kalman, vmark = mon.det, mon.kalman, mon.vmark
    first = mon.prepare(frame)

    ref_files = [args.reference] if args.reference else \
        list((cal.reference_images or {}).values()) + ([cal.reference_image] if cal.reference_image else [])
    for name in dict.fromkeys(ref_files):                       # unique, keep order
        path = _resolve(name, cfg_dir)
        img = cv2.imread(path)
        if img is None:
            LOG.warning("Dry reference image not found: %s", path)
            continue
        light = det.add_reference(img)
        LOG.info("Dry reference loaded for %s lighting: %s", light, path)
    det._activate(classify_lighting(cv2.resize(first, SMALL, interpolation=cv2.INTER_AREA),
                                    args.night_brightness))
    if not det.uses_reference and args.method != "color":
        LOG.warning("No dry reference frame - using the color method. Press 'b' on a dry, empty road.")

    def reference_captured(light, bgr, replaced=False):
        tag = "_virtual" if (replaced or cal.virtual_marker) else ""
        name = f"reference_dry_{cal.camera_id}_{light}{tag}.png"
        cv2.imwrite(os.path.join(cfg_dir, name), bgr)
        if replaced and vmark.state == "capturing":
            vmark.finish(light, name)                             # virtual marker conversion done
            return
        cal.reference_images = dict(cal.reference_images or {}, **{light: name})
        if os.path.isfile(args.config) and not cal.is_default:
            save_setup()                                          # keep the bank for the next start
        LOG.info("Saved %s dry reference: %s", light, name)
    det.on_reference_captured = reference_captured
    if det.perspective:
        LOG.info("Perspective compensation: %s", det.perspective)
    if cal.virtual_marker:
        LOG.info("Virtual marker active (locked) - no physical marker needed.")
    elif vmark.blocking:
        LOG.warning("Marker is locked but not converted yet: %s", vmark.status_text())
    if tag_layout and not cal.tag_anchors:
        LOG.info("ArUco tag layout loaded - press 'g' to fit the marker ROI from the tags.")
    elif mon.aligner is not None:
        LOG.info("ArUco anchors active: frames are re-aligned if the camera moves.")

    script_keys = []
    if args.keys:
        for item in args.keys.split(","):
            ts, k = item.split(":")
            script_keys.append((float(ts), k.strip()))
        script_keys.sort()
    feed = TerminalFeed(args.print_interval, args.json, not args.no_color)
    guard = ReferenceGuard(wet_coverage=args.wet_coverage, wet_level=args.wet_level)

    # ---- outputs: CSV log + settings sidecar, recording ----
    logger = None
    sidecar = None
    run_info = {"source": source.describe(), "lens_file": lens_path,
                "file_start_time": source.start_time.isoformat() if source.start_time else None}
    if args.log_csv:
        logger = CsvLogger(args.log_csv, args.csv_flush_s)
        base = os.path.splitext(logger.path)[0]
        sidecar = write_settings_sidecar(f"{base}.settings_{datetime.now():%Y%m%d_%H%M%S}.json",
                                         settings_dict(args), cal, run_info)
        LOG.info("Logging to %s (settings: %s)", logger.path, sidecar)
    recorder = None
    if args.record:
        if sidecar is None:
            os.makedirs(args.record, exist_ok=True)
            sidecar = write_settings_sidecar(os.path.join(args.record, f"settings_{datetime.now():%Y%m%d_%H%M%S}.json"),
                                             settings_dict(args), cal, run_info)
        recorder = SegmentRecorder(args.record, cal.camera_id, source.fps_hint, args.record_minutes, sidecar)

    win = "Flood Monitoring"
    editor = SetupEditor(cal, W, H)
    if not args.no_display:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(win, editor.on_mouse)

    show_mask, show_water, paused, enhance = False, True, False, not args.no_enhance
    fps, t_fps, n_fps = 0.0, time.monotonic(), 0
    view_frame = first
    r = None
    meas = res = status = None
    q = det.quality
    offline_since = None

    def apply_setup_change(msg):
        mon.set_calibration(view_frame.shape)
        LOG.info("%s | ref %.2f m | perspective %s", msg, cal.ref_height_m, det.perspective)

    def common_key(k: str):
        if k == "x":
            LOG.info(vmark.press(t, meas, q))
        elif k == "X":
            LOG.info(vmark.unlock())
        elif k == "b":
            go, msg = guard.press(time.monotonic(), t, meas, q, mon.last_motion_t)
            (LOG.info if go else LOG.warning)(msg)
            if go:
                mon.record_anchors(view_frame)
                det.start_capture(q.lighting, reason="manual")     # averaged = less noise
                kalman.reset()
        elif k == "g":
            msg = mon.fit_marker_from_tags(view_frame)
            LOG.info(msg)
            if msg.startswith("Marker ROI fitted"):
                apply_setup_change("Marker ROI from tags")
        elif k == "s":
            mon.record_anchors(view_frame)
            save_setup()

    try:
        while True:
            if not paused:
                if frame is None:
                    ok, frame, t, eos = source.read()
                    if eos:
                        LOG.info("End of video.")
                        break
                    if not ok:
                        # live camera silent: report it (the website shows 'camera offline')
                        gap = source.seconds_since_frame()
                        if source.is_live and gap >= args.offline_s:
                            offline_since = offline_since or time.monotonic()
                            feed.emit(offline_payload(cal, source.kind, gap, mon.last_level), time.monotonic())
                        frame = None
                        if args.no_display:
                            continue
                else:
                    ok = True
                if frame is not None:
                    offline_since = None
                    if frame.shape[:2] != (H, W):
                        frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
                    if recorder is not None:
                        recorder.write(frame, t, mon.timestamp(t) if source.kind == "file" else None)
                    # lens -> tags -> quality / stabilisation -> measurement (not while dragging a region)
                    r = mon.process(frame, t, measuring=editor.drag is None)
                    view_frame, meas, res, status, q = r.frame, r.meas, r.res, r.status, r.quality
                    frame = None

                    n_fps += 1
                    if time.monotonic() - t_fps >= 1.0:
                        fps = n_fps / (time.monotonic() - t_fps)
                        t_fps, n_fps = time.monotonic(), 0
                    mon.fps = fps
                    r.payload["fps"] = round(fps, 1)
                    feed.emit(r.payload, time.monotonic())
                    if logger is not None and res is not None:
                        logger.write(r.payload, meas, res, q,
                                     {"tags_visible": (r.tag_info or {}).get("visible", ""),
                                      "camera_shift_px": r.payload.get("camera_shift_px", "")})

            while script_keys and script_keys[0][0] <= t:
                common_key(script_keys.pop(0)[1])
            if args.no_display:
                continue

            # ---- window: redraw every loop so dragging is smooth even while paused
            if offline_since is not None:
                hint = f"CAMERA OFFLINE for {time.monotonic() - offline_since + args.offline_s:.0f} s - waiting for video"
            elif vmark.status_text():
                hint = vmark.status_text()
            elif cal.is_default:
                hint = "DEFAULT ROIs: drag cyan (flood) & magenta (marker) corners, [ ] ref height, s save"
            elif paused:
                hint = "PAUSED - drag corners / regions | right-click flood edge: add / remove vertex"
            else:
                hint = "drag corners / regions | [ ] , . ref height | f m g new ROI | b dry ref | s save"
            extra = [(f"MARKER: {cal.marker_mode.upper()}", MARKER_COLOR)]
            if lens is not None:
                extra.append(("lens corrected", (200, 200, 200)))
            if r is not None and r.tag_info is not None:
                extra.append((f"tags {r.tag_info['visible']} visible - {r.tag_info.get('status', '')}",
                              (200, 200, 200) if r.tag_info["visible"] else (0, 140, 255)))
            if recorder is not None and recorder.path:
                extra.append((f"REC {os.path.basename(recorder.path)}", (0, 0, 255)))
            base = enhance_for_display(view_frame, q) if enhance else view_frame.copy()
            view = draw_overlay(base, det, cal, res, meas, status, fps, hint, show_water, extra_lines=extra)
            if show_mask and meas is not None and det.geom is not None:
                x, y, w, h = det.geom["crop"]
                mv = cv2.merge([meas.object_mask, np.zeros_like(meas.water_mask), meas.water_mask])
                view[y:y + h, x:x + w] = cv2.addWeighted(view[y:y + h, x:x + w], 0.4, mv, 0.6, 0)
                rv = cv2.cvtColor(cv2.resize(meas.rect_view, (60, min(H - 20, 300))), cv2.COLOR_GRAY2BGR)
                view[10:10 + rv.shape[0], W - 70:W - 10] = rv
            cv2.imshow(win, view)
            key = cv2.waitKey(1 if not paused else 30) & 0xFF

            if editor.changed:
                editor.changed = False
                apply_setup_change("Setup updated")

            if key in (ord("q"), 27):
                break
            elif key == ord("p"):
                paused = not paused
            elif key == ord("v"):
                show_mask = not show_mask
            elif key == ord("w"):
                show_water = not show_water
            elif key == ord("e"):
                enhance = not enhance
                LOG.info("Display enhancement %s", "on" if enhance else "off")
            elif key in (ord("x"), ord("X"), ord("b"), ord("s"), ord("g")):
                common_key(chr(key))
            elif cal.marker_locked and key in (ord("m"), ord("["), ord("]"), ord(","), ord(".")):
                LOG.warning("The marker is locked as a virtual marker - press Shift+X to unlock it first.")
            elif key in (ord("f"), ord("r"), ord("m")):
                which = "marker" if key == ord("m") else "flood"
                roi = cv2.selectROI(win, view_frame, showCrosshair=True, fromCenter=False)
                if roi[2] > 10 and roi[3] > 10:
                    setattr(cal, f"{which}_roi", rect_to_polygon(*roi))
                    apply_setup_change(f"New {which.upper()} ROI - drag its corners to fit")
                cv2.setMouseCallback(win, editor.on_mouse)
            elif key in (ord("["), ord("]"), ord(","), ord(".")):
                step = {ord("["): -0.05, ord("]"): 0.05, ord(","): -0.01, ord("."): 0.01}[key]
                cal.ref_height_m = round(float(np.clip(cal.ref_height_m + step, 0.05, 5.0)), 2)
                apply_setup_change("Reference height changed")

    except KeyboardInterrupt:
        pass
    finally:
        source.release()
        if logger is not None:
            logger.close()
        if recorder is not None:
            recorder.close()
        cv2.destroyAllWindows()
