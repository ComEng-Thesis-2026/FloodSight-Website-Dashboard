# FloodSight feed service

The OpenCV flood monitoring program running behind the dashboard's video feeds.

It plays the recorded CDRRMO clips in a loop, one per location, and runs the `floodmon` pipeline from `OpenCV-FloodMonitoring-main/` on every frame. That pipeline is used unchanged, so the measurements match the stand-alone program.

The service does three things for the dashboard:

- streams the clean video and the annotated video
- accepts ROI and marker changes from the Camera Config page
- writes every reading to MongoDB

| Feed id | Location | Video file |
|---|---|---|
| `mastersons` | Masterson's Avenue | `videos/mastersons.mp4` |
| `kauswagan` | Kauswagan Highway | `videos/kauswagan.mp4` |

The feeds are defined in `feeds.json`. Each feed has these fields:

| Field | Meaning |
|---|---|
| `id` | feed id |
| `location` | location name |
| `video` | video file |
| `coordinates` | `lat` and `lng` |
| `max_width` | processing width, default 960 |
| `stream_fps` | stream rate, default 12 |

## Run

1. Copy the two edited clips into `feed-service/videos/` with the names in the table. The folder is git-ignored.
2. Run these from the project root, in two terminals:

```
npm run feeds        # this service, http://127.0.0.1:8765
npm run dev          # the dashboard
```

The service reads `MONGODB_URI`, `MONGODB_DB`, `MONGODB_COLLECTION` and `MONGODB_READINGS_COLLECTION` from the project's `.env`.

The dashboard reaches the service through `/api/feeds/*`, using `FEED_SERVICE_URL`. The default is `http://127.0.0.1:8765`.

Options:

| Option | Effect |
|---|---|
| `--no-db` | no MongoDB writes |
| `--port N` | listen on port N |
| `--fast` | no real-time pacing; for testing |
| `--capture-interval S` | seconds of video between periodic captures (default 10); events are always captured |
| `--no-captures` | no snapshot / metrics captures |

If a video file is missing, the feed shows "Video not found" and the service retries every 10 s. The other feed keeps running.

## First-time setup of a feed (admin)

Open **Camera Config** and choose the feed. The editor shows the live video. Then:

1. Drag the corners of the **flood ROI** (cyan) over the road. Use **+ Point** and **− Point** to change the polygon.
2. Drag the 4 corners of the **marker ROI** (magenta) onto the marker. The bottom edge is the road (0 m) and the top edge is the reference height.
3. Set the **reference height**, then press **Apply ROI**.
4. Press **Restart video** to go back to the dry scene, then **Pause video** to freeze it there and press **Capture dry reference**. While paused the frozen frame is still measured, so the capture completes on it (and no metrics captures are recorded). Press **Play video** to continue.
   - This is the same as the `b` key. The capture is refused while the road is wet or something is moving.
   - Press it again within 5 s to capture anyway.
5. Virtual marker (optional): the same steps as the `x` key.
   1. Press **Lock virtual marker (1/2)** while the physical marker is visible.
   2. Once the clip shows the scene without the marker, and the road is dry and empty, press **convert (2/2)**.
   3. **Unlock marker** is Shift+X.
6. Press **Save OpenCV setup**.

The setup is stored in `setups/<id>/` (`flood_config.json` and the dry-reference PNGs), in the format used by the stand-alone program. It is reused on the next start.

Location names and coordinates are edited and saved on the same page and are stored in MongoDB. Coordinates set in `feeds.json` are applied only while the camera document has none.

## What is written to MongoDB

The camera document in `MONGODB_COLLECTION` has the id `feed-<id>` and is updated every second with:

- `water_level` and `status`. OpenCV's SAFE is stored as NORMAL; ALERT and DANGER are unchanged.
- `timestamp`
- `roi_config`
- `opencv_setup`
- a `feed` object with the OpenCV metrics: coverage, confidence, image quality, marker mode, fps, processing time and service state

The dashboard's `GET /api/cameras` reads this document like any other camera.

The readings collection, `MONGODB_READINGS_COLLECTION` (default `flood_readings`), gets one document per second per calibrated feed, plus one at once for every event: start, status change, obstruction, held data, lighting change. Each document has:

- `timestamp`, `video_t_s`, `loop_index`
- `water_level_m`, `raw_level_m`, `status`
- `flood_coverage_pct`, `confidence`, `data_valid`, `anomaly_reason`
- `marker_mode`, `image_quality`, `event`
- `latency {processing_ms, camera_update_ms, frame_to_write_ms}`

The dashboard trend chart reads this collection through `GET /api/cameras/<id>/readings?minutes=10`.

Each new loop of a clip starts with a fresh Kalman filter and status, so the dry scene reads 0 m again. The dry reference is kept.

## Measurement captures (snapshot + metrics)

While a feed is calibrated and measuring, a capture is taken on every event (start, status change, obstruction, lighting change) and every 10 s of video (`--capture-interval`). The code is in `metrics_capture.py`.

**Saved locally** in `captures/<feed id>/` (git-ignored). The snapshot is never stored in MongoDB.

| File | Content |
|---|---|
| `<feed>_<UTC time>.jpg` | the captured frame, clean |
| `<feed>_<UTC time>_processed.jpg` | the same frame with the OpenCV overlay |
| `<feed>_<UTC time>.json` | the capture's data and metrics (same fields as the MongoDB document) |
| `summary.csv` | one row per capture; the `paper_results/summary.csv` columns plus the feed fields |

**Stored in MongoDB**, one document per capture in `MONGODB_METRICS_COLLECTION` (default `camera_metrics`):

- `camera_id` (`feed-<id>`), `feed_id`, `location_label`, `capture_id`, `timestamp`, `trigger` (`event` / `interval`), `event`
- `water_level_m`, `raw_level_m`, `status` (NORMAL / ALERT / DANGER), `rate_m_per_min`, `above_range`
- `flood_detected`, `flood_coverage_pct`, `confidence`, `data_valid`, `anomaly_reason`, `resolution_cm_per_px`, `image_quality`, `marker_mode`, `fps`
- `pixel_detection_accuracy`, `classification_accuracy` and `accuracy_inputs`
- `latency`:

  | Field | Measures |
  |---|---|
  | `tcapture_ms` | decoding and resizing the frame |
  | `tprocessing_ms` | the whole floodmon `Monitor.process` call |
  | `thsv_classification_ms` | water detection and level (`FloodDetector.process`), part of `tprocessing_ms` |
  | `tkalman_ms` | the Kalman step, part of `tprocessing_ms` |
  | `tsnapshot_ms` | drawing, encoding and saving the two JPEGs |
  | `tsync_ms` | the capture waiting for the recorder thread |
  | `tdb_write_ms` | the MongoDB insert |
  | `troute_ms` | always 0; routing is not part of the feed (as in `cameraTest.py`) |
  | `Tresponse_ms` | from the frame being decoded to the document being written |

- `snapshot`: the local file names only (`file`, `processed_file`, `json_file`), so a document can be matched to its images

PA and CA use the formula of `cameraTest.py`, the camera test used for the thesis experiments: `compute_accuracy(measured, truth) = max(0, 1 − |measured − truth|)`. There is no ground truth for the clips, so the reference is the measured water ratio, as in `cameraTest.py`. As a result:

- PA is always 1.0.
- CA compares the level, as a fraction of the marker's reference height, with the flood ROI coverage. `cameraTest.py` used its 0–3 m range here.
- `accuracy_inputs` keeps the values used, so PA and CA can be recomputed.

The latency stages are timed on the captured frame. `cameraTest.py` instead estimated the HSV share as 60 % and the Kalman share as 10 % of processing time.

If MongoDB is unreachable, or the service runs with `--no-db`, captures are still saved locally with `db_written: false`. `--no-captures` turns captures off.

## HTTP API (local)

| Endpoint | |
|---|---|
| `GET /feeds` | all feeds: state, latest reading, setup, MongoDB status |
| `GET /feeds/<id>/reading` | latest reading |
| `GET /feeds/<id>/stream?overlay=1\|0` | MJPEG stream, with or without the OpenCV overlay |
| `GET /feeds/<id>/frame.jpg?overlay=0\|1` | one frame |
| `GET /feeds/<id>/setup` | ROIs, reference height, marker mode, references |
| `PUT /feeds/<id>/setup` | `{flood_roi?, marker_roi?, ref_height_m?}`; invalid values give a 400 response with `problems` |
| `POST /feeds/<id>/actions` | `{action}`, one of `capture_reference`, `virtual_marker`, `virtual_unlock`, `save`, `restart`, `pause`, `resume` |

## Tests

```
npm run test:feeds
```

The tests render a synthetic flood with `floodmon.synthetic` and run it through the worker and the HTTP server. They do not need MongoDB.
