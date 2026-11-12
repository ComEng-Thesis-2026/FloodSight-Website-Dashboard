# OpenCV-FloodMonitoring

Real-time flood level monitoring using a camera and computer vision.

Thesis prototype for *"Development of a CCTV-Based Flood Monitoring and Routing Advisory System Using Computer Vision"*.

The program reads a CCTV, webcam or phone camera feed with OpenCV. It finds flood water on the road and measures the water level on a 1 m marker:

| Level | Status | Colour |
|---|---|---|
| 0.00 – 0.30 m | SAFE | GREEN |
| 0.30 – 0.50 m | ALERT | YELLOW |
| 0.50 – 1.00 m | DANGER | RED |

The bands are defined once, in `floodmon/bands.py`.

## Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The program needs OpenCV 4.7 or newer for the ArUco tags. It is tested with Python 3.14, OpenCV 4.13 and NumPy 2.5.

## Run

Choose the camera in the **CAMERA SETTINGS** block at the top of `FloodMonitoring.py`, then:

```
python FloodMonitoring.py                       # camera from CAMERA_MODE
python FloodMonitoring.py --camera dahua        # or laptop / iriun / file
python FloodMonitoring.py --source clip.mp4     # a recorded video
python FloodMonitoring.py --list-cameras        # which camera index is which
python FloodMonitoring.py --help                # every setting
```

Set the Dahua password with the `DAHUA_PASSWORD` environment variable rather than in the file.

## Setting up a camera

The two regions are drawn in the video window:

- **Flood ROI** (cyan): the road area where water coverage is measured.
- **Marker ROI** (magenta): 4 corners on a flat vertical surface. The bottom edge is the road (0 m) and the top edge is the reference height.

| Key | Action |
|---|---|
| `f` / `m` | draw a new flood / marker ROI, then drag its corners |
| `g` | fit the marker ROI from ArUco tags |
| `[` `]` / `,` `.` | reference height −/+ 5 cm / 1 cm |
| `b` | capture the dry reference (checks first that the road is dry and nothing moves) |
| `x` / `Shift+X` | virtual marker: lock, then convert / unlock |
| `s` | save the setup |
| `w` `v` `e` `p` `q` | water overlay, mask view, display enhancement, pause, quit |

### Physical marker mode

A real gauge stays in view.

1. Fit the marker ROI to the gauge, either by dragging the corners or with tags (`g`, see below).
2. On a dry, empty road press `b`. The program captures references for night and IR automatically later, once it has confirmed the road is dry.
3. Press `s`.

### Virtual marker mode

The gauge is only needed once.

1. With the gauge in place and fitted, press `x`. This locks the geometry.
2. Remove the gauge. With the road dry and empty, press `x` again. A new reference without the gauge is captured.
3. From then on the water line is read on the surface behind where the gauge stood.

### ArUco anchor tags (optional, recommended)

The tags give a marker ROI that is accurate to under a pixel, with no clicking. They also keep the picture aligned if the camera is bumped.

```
python tools/make_aruco_tags.py --ids 0 1 2 3 --size 0.12 --out tags/
```

1. Print the tags at 100 % and fix them on the same surface as the gauge. Put two above the highest expected water and one or two near the road. The low tags are only needed while fitting.
2. Measure each tag centre and type it into `tags/tag_layout.json`. The x value is measured from the marker's left edge, the h value from the road.
3. Run the program, then press `g`, `b` and `s`:

```
python FloodMonitoring.py --tags tags/tag_layout.json --marker-width 0.15
```

### Lens calibration (wide-angle CCTV)

Wide lenses bend straight lines, so a marker away from the picture centre is measured with an error of several centimetres. Calibrate once per camera, lens and resolution:

```
python tools/calibrate_lens.py --make-board board.png --cols 9 --rows 6 --square 0.025
python tools/calibrate_lens.py --camera dahua --square 0.025 --out lens_CAM-01.json
python FloodMonitoring.py --lens lens_CAM-01.json
```

Print the board and measure a square before calibrating. Then re-fit the ROIs, capture a new dry reference (`b`) and save (`s`). The ROIs must be drawn on corrected frames, and the program warns if they are not.

## Outputs

- **Terminal feed.** One line per second, plus an immediate line on every status change. Use `--json` for JSON lines, which is the future website format.
- **Payload fields.** `water_level_m`, `status`, `status_color`, `above_range` (water at the top of the gauge), `resolution_cm_per_px` (the real precision), `data_valid`, `anomaly_reason`, `marker_mode`, `image_quality` and `tags`. Timestamps follow the video's own clock for recorded files. While a live camera sends no frames, the status is `CAMERA_OFFLINE`.
- **`--log-csv FILE`.** Every reading is written and flushed to disk every second. A `FILE.settings_<time>.json` sidecar records the program version, git commit, every setting and the setup, so a run can be reproduced.
- **`--record DIR`.** The raw video is saved in segments with start-time sidecars. Re-analyse a segment later with `--source DIR/<segment>.mp4`.

## Measuring accuracy (thesis evaluation)

**Synthetic check.** These scenes are rendered with exact ground truth.

```
python tools/make_synthetic_video.py physical out/synth_physical      # also: virtual, tags, bump
python FloodMonitoring.py --source out/synth_physical/scene.mp4 --config out/synth_physical/flood_config.json --fast --no-display --log-csv out/synth_physical/readings.csv
python tools/evaluate.py out/synth_physical/readings.csv out/synth_physical/ground_truth.csv
```

The evaluation reports:

- MAE, RMSE, bias, P95 and maximum error, overall and per band
- status accuracy, with a confusion matrix
- warning delay at 0.30 m and 0.50 m
- the share of frames with valid data
- errors while a car passes or the camera shakes

Synthetic accuracy only proves the measuring chain. Field accuracy must be measured.

**Field test.** Use a drum or tank with the marker and a tape measure.

1. Record with `--record` and `--log-csv`.
2. Fill the tank in 5 cm steps. At each step, note the tape reading and the video time.
3. Put those readings in a CSV with the columns `t_s,level_m`. Sparse rows are interpolated.
4. Run `python tools/evaluate.py readings.csv tape.csv`.
5. Repeat by day, at night and in IR, and with the marker near the picture edge.
6. Tune settings on one set of clips and report the results on a different set.

## Results on synthetic scenes

The table compares the original program (git tag `baseline`) with this version. Each scene is 900 frames: dry, a rise to 0.80 m, a hold, then a fall. The error is for the filtered level against the exact truth.

| Scene | Original: mean / worst error | This version: mean / worst error | Status correct, original → new |
|---|---|---|---|
| Physical gauge, car passes | 2.29 / 27.1 cm | 0.44 / 2.7 cm | 97.9 → 97.8 % |
| Virtual marker (plain wall), car passes | 0.44 / 2.3 cm | 0.44 / 2.3 cm | 98.2 → 98.2 % |
| Camera shakes 14 px for 20 s, tags off | 2.17 / 27.0 cm | 0.45 / 2.6 cm | 98.4 → 98.0 % |
| Same, tags on | – | 0.73 / 2.4 cm | 97.6 % |
| Camera knocked 70 px for good, tags off | 9.22 / 30.7 cm | 10.94 / 35.4 cm | 56.0 → 55.9 % |
| Same, tags on | – | 0.71 / 2.4 cm | 97.6 % |

Most of the gain comes from the robust brightness mapping (see NOTES.md). Without tags, a knocked camera can only be detected, and the level is held. With tags it is corrected. These are synthetic numbers, and field accuracy will be lower.

## Project layout

| Path | Contents |
|---|---|
| `FloodMonitoring.py` | entry point + CAMERA SETTINGS |
| `floodmon/bands.py` | flood bands, classification, status hysteresis |
| `floodmon/config.py` | every setting (command line) |
| `floodmon/calibration.py` | setup file: ROIs, references, validation |
| `floodmon/geometry.py` | marker homography, perspective, resolution |
| `floodmon/lens.py` | lens calibration and undistortion |
| `floodmon/tags.py` | ArUco anchor tags: marker fit, re-alignment |
| `floodmon/video.py` | Dahua / webcam / file sources, video timestamps |
| `floodmon/quality.py` | image quality, lighting, brightness mapping, stabilisation |
| `floodmon/detector.py` | water evidence, road coverage, water line on the marker |
| `floodmon/kalman.py` | Kalman filter, obstruction rejection, hold |
| `floodmon/modes.py` | virtual-marker conversion, guarded reference capture |
| `floodmon/pipeline.py` | one frame through the whole chain, without a window |
| `floodmon/feed.py` | payload, terminal feed, CSV log, settings sidecar |
| `floodmon/recorder.py` | raw video recording |
| `floodmon/ui.py`, `floodmon/app.py` | window, mouse editor, keys, main loop |
| `floodmon/synthetic.py`, `floodmon/evaluation.py` | synthetic scenes, accuracy metrics |
| `tools/` | `make_synthetic_video`, `evaluate`, `calibrate_lens`, `make_aruco_tags` |
| `tests/` | unit and end-to-end tests |

## Tests

```
python -m unittest discover -s tests -v
```

There are 25 tests, which take about 2 minutes. They cover the bands, geometry, setup-file validation, the Kalman filter (vehicle spike, parked car, fast real rise, hold), brightness mapping, tags, lens calibration, evaluation, the CSV log, and synthetic floods through the full pipeline, including one through a wide-angle lens.
