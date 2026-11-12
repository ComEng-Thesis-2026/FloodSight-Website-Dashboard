# Improvement notes

This file tracks the review of `FloodMonitoring.py` from 2026-11-08 and what was done about each recommendation.

The review was first written for the older copy in `.venv\FloodMonitoring\`, which is identical to `FloodMonitoring_overwritten_backup.py.txt`. The work below was done on the newer version in this folder. That version already handled day, night and IR references, camera-shake stabilisation, data hold on bad images, the virtual-marker workflow, and rejection of parked cars when re-locking.

## Status of the recommendations

### Code and project

| Recommendation | Status |
|---|---|
| Move the program out of `.venv` | Done. The project lives in `test1\FloodMonitoring\`, and the old copy in `.venv` was left untouched. |
| Put the project under git | Done. The original code is tagged `baseline`, and the work is on the `improvements` branch. |
| Requirements file | Done (`requirements.txt`). |
| Split the file into modules | Done (`floodmon/`). Readings before and after the split were identical on 2,700 synthetic frames. |
| Separate the analysis from the display | Done. `floodmon/pipeline.py` runs without a window. |
| Shared handling of the two marker modes | Done. `Calibration.marker_mode` is physical, converting or virtual, and the mode is in the payload. |
| Define the flood bands once | Done (`floodmon/bands.py`). |
| Move hidden thresholds into settings | Done for the decision thresholds, about 25 new options with the same defaults. Small numeric constants inside formulas stay in the code with comments. |
| Break up the measuring function | Done. `measure()` now calls 8 small steps. |
| Flush the CSV log regularly | Done. The log is flushed every second and synced to disk. |
| Camera offline status | Done. A live camera with no frames for `--offline-s` seconds reports `CAMERA_OFFLINE`. |
| Check before dry-reference capture | Done. A wet road, motion or a bad image blocks the capture, and pressing `b` twice overrides it. |
| Validate the setup file | Done. Every problem is listed with a readable message. |
| Video time for recorded files | Done. The start time comes from the recording sidecar, `scene.json`, `--file-start`, or file time minus duration. |
| Unit tests | Done. There are 25 tests, run with `python -m unittest discover -s tests`. |
| Synthetic test video | Done (`tools/make_synthetic_video.py`). |
| Offline evaluation script | Done (`tools/evaluate.py`). |
| Recording option | Done (`--record`). |
| Settings sidecar with every log | Done. |

### Accuracy

| Recommendation | Status |
|---|---|
| Store the dry reference as float32 | Done. The newer version blended every 15 frames in 8-bit, which still lost changes below about 17 grey levels. |
| Never auto-accept a jump while something is parked | Already in the newer version, because a re-lock above 0.10 m needs road water. It is now a setting (`--relock-support`) and is tested. |
| Above-range status | Done. `above_range` is in the payload, the CSV, the terminal and the window. |
| Lens calibration and undistortion | Done (`tools/calibrate_lens.py`, `--lens`). It is tested end to end with a synthetic wide-angle lens. |
| ArUco tags for the physical marker | Done. The `g` key fits the marker ROI and the tags re-align the frame. |
| Read the gauge row by row against its dry appearance | Already present in the newer version as the step method and the texture term. |
| Keep the virtual mode simple | Already present: the lock-and-remove workflow. |
| Report the real resolution, not 2.5 mm | Done. `resolution_cm_per_px` is in the payload, the CSV and the window. |
| Day and night references | Already in the newer version as a reference bank for day, night and IR. |
| Camera movement detection | Already present: phase correlation. Tags now also correct large moves. |
| Water segmentation model | **Not done.** It needs labelled flood images from your own cameras. The pipeline is ready for it: replace `FloodDetector._evidence`. |

### Problems found and fixed during the work

- **Brightness mapping corrupted by the flood.** Gain and offset came from percentiles of the whole "static" scene outside the ROIs. During a flood, water also covers road and wall outside the ROIs, which pushed the gain to 2.5 and the offset to −248. Dry surfaces then looked like water, worst on dark details such as gauge ticks, and the reading jumped to the next tick. The fix is a robust line fit that follows only unchanged pixels (`robust_photometric`). `--percentile-mapping` restores the old behaviour for comparison.
- **The robust fit needs a stabilised thumbnail.** The fit compares pixel by pixel, so it now runs after shake compensation.
- **Tag re-alignment with tags in one row.** A full homography fitted to tags at one height extrapolates badly down to the water line. Re-alignment now uses a similarity transform, smoothed over 5 frames. The marker fit warns when all tags sit at about the same height.

## Open item found during evaluation (not changed)

**A held level counts as valid data for 120 s, even when the camera has moved.** When the camera is knocked out of position (CAMERA-MOVED, no tags), measuring stops and the last level is held. The data stays `data_valid = true` for `--hold-s` (120 s). In the synthetic test the water kept rising during the hold, and the reported level fell up to 35 cm behind while the status still said SAFE or ALERT. The original program behaves the same way.

A safer option is a short hold for failures that do not fix themselves (camera moved, frozen video, no signal), for example 10 s, while keeping 120 s for glare or darkness. This changes the contingency behaviour you designed, so it is left for you to decide. Until then, `--hold-s 10` gives the short hold for all failures.

## What only you can do (field work)

1. Calibrate the lens of each Dahua camera at the deployment resolution.
2. Print and mount the tags, measure their positions, then press `g`, `b` and `s`.
3. Run a drum or tank test in 5 cm steps by day, at night and in IR, and with the marker at the picture centre and near the edge. Score each run with `tools/evaluate.py`.
4. Record real floods with `--record`. Label a set of frames if you want to train a water segmentation model later.
5. Report field accuracy separately from the synthetic results.
