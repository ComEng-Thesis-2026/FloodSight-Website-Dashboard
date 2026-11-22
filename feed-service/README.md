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
| `flood_full_depth_m` | site value: the water depth (m) at which the whole flood ROI is under water, i.e. the height of the highest ground inside it (curb, raised shoulder, road crown) above the road. Default 0.20 |

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
   2. Press **convert (2/2)** while the road is dry and empty. In the demo clips the physical marker is shown only in the dry first sequence, so press it there: the virtual marker measures from the flood ROI (next section), so the marker does not have to be gone from the reference.
   3. **Unlock marker** is Shift+X.
6. Press **Save OpenCV setup**.

The setup is stored in `setups/<id>/` (`flood_config.json` and the dry-reference PNGs), in the format used by the stand-alone program. It is reused on the next start.

Both demo clips come already set up: the marker ROI is fitted to the composited physical marker of the first (dry) sequence, 1.0 m reference height, virtual and locked, with the dry reference taken from that sequence.

| Feed | Marker ROI (960x540; bottom = 0 m, top = 1.0 m) | Scale | Dry / wet / flooded sequences |
|---|---|---|---|
| `kauswagan` | x 282-292, y 269.5 (base) - 233.5 (top) | 36 px/m, 2.8 cm/px | 0-24.7 s / 24.7-49.7 s / 49.7-156.8 s |
| `mastersons` | x 633.5-639.5, y 211.5 (base) - 187.5 (top) | 24 px/m, 4.2 cm/px | 0.7-4.4 s / 4.4-39.3 s / 39.3-127.5 s |

## Virtual marker measured from the flood ROI, and the road condition

The code is in `virtual_flood_marker.py`. floodmon itself is not changed.

floodmon's own virtual marker reads the water line inside the marker ROI by comparing it with a dry reference. The demo clips cut from the dry daytime scene to other footage (a wet road at night or in rain, then the flood). After a cut nothing behind the marker matches any reference, so that comparison read the whole marker as water. In the feed service, once the marker is **virtual**, the level comes from the flood ROI instead, and the background inside the marker ROI is not used:

1. **Flood water**: pixels of the flood ROI that no longer match the dry reference, AND have flood-water colour (Lab: muddy brown, hue 15-70°, chroma ≥ 3.5; or olive / dark green, hue 70-170°, chroma ≥ 3.5 by day and ≥ 7 at night), AND are not glare, black or a moving object, AND form connected areas of at least 2 % of the ROI. At night a weak yellow cast is lamp light on a wet road, not water, which is why olive needs the higher chroma then.
2. **Water share**: the upper quartile of the last 15 s. Traffic, ripples and glare make the per-frame share jump, and a vehicle or glare can only hide water, never add it.
3. **Road condition**:
   - FLOODED: the water share is ≥ 35 %. It stays FLOODED until the share drops below 15 %.
   - WET: not flooded, but the road no longer matches its dry reference (≥ 5 %, back to dry below 3 %).
   - DRY: otherwise.

   A change must hold for 1 s.
4. **Depth** = water share x the feed's `flood_full_depth_m`, and 0 m unless the road is FLOODED. A road is completely covered long before the water is deep: once the water reaches the curb or shoulder, the whole flood ROI is under water at a depth of only that relief, about 0.15-0.20 m. So the water's extent tells how far it has climbed the ROI's relief, not how far up a 1 m marker. (The first version multiplied by the marker's 1.0 m and read 0.3-0.9 m for ankle-deep water.) Water deeper than the full-cover depth cannot be read from the extent alone, so the reading stops there. The remembered marker shows the depth on its scale and colours. Entering or leaving FLOODED restarts the Kalman filter, because the step from the classified 0 m is not a spike.

On the demo clips the road condition follows the cuts 2-7 s late. That delay comes from the 15 s window and the 1 s confirmation.

| Feed | Dry | Wet | Flooded |
|---|---|---|---|
| `kauswagan` | DRY, 0 m | WET, 0 m (flood-coloured share ≤ 22 %: muddy, lamp-lit sidewalk) | FLOODED, about 0.16-0.20 m, SAFE (green band) |
| `mastersons` | DRY, 0 m | WET, 0 m | FLOODED, about 0.09-0.16 m, SAFE (green band) |

These match the depth seen in the footage. At Masterson's, lane markings show through the water, cars' tyres are covered only at the bottom, and riders' feet stay dry: about 10-15 cm. At Kauswagan, the water covers the road and the shoulder up to the base of the striped barrier, but not the lawn beside them: about 15-20 cm. Both feeds use `flood_full_depth_m` 0.20, estimated from the footage; measure the curb or shoulder height on site for a real camera.

In physical-marker mode the marker reading stays floodmon's, and only the road condition is added. floodmon's own dry / wet bookkeeping (automatic references, daylight adaptation, the dry-reference and conversion checks) still sees how much of the road changed. A wet road is therefore never treated as dry.

The reading gets these extra fields:

| Field | Meaning |
|---|---|
| `road_condition` | `DRY`, `WET` or `FLOODED` |
| `level_source` | `flood_roi` or `marker` |
| `flood_water_pct` | the smoothed flood-water share, before the FLOODED gate |
| `road_change_pct` | share of the flood ROI that no longer matches the dry reference |

`classification_accuracy` compares level / marker reference height with coverage (cameraTest.py's formula). With the flood-ROI depth, level / 1.0 m is about 0.2 x coverage, so CA comes out low (about 0.2-0.6) in the flooded sequences even when the depth is right. The formula assumes the level scales with the marker height.

## The remembered physical marker

The code is in `marker_memory.py`. While the physical marker is in view, the feed service reads it through the marker ROI's homography. It keeps:

- its GREEN / YELLOW / RED bands and the height of each boundary;
- each band's real colour, and a photo of the marker;
- the scale it was calibrated with (px per m, cm per px);
- the marker's reading at that moment.

These are stored in `setups/<id>/marker_memory.json` and `marker_memory.png`. floodmon's `flood_config.json` keeps only its own keys.

When it is remembered:

- **Lock virtual marker (1/2)**, which is pressed while the physical marker is in view.
- **Remember physical marker**, on the Camera Config page, at any time. Pause on the dry scene first.
- **Automatically at start**, for a locked or virtual marker that has no memory yet. It is read from the dry reference taken with the physical marker in view (`physical_marker_references` first).
- A memory belongs to the exact marker ROI corners and reference height. Moving the marker ROI clears it.

What it changes once the marker is virtual:

- **Status:** the status is the colour of the remembered band the water reaches. GREEN = SAFE, YELLOW = ALERT, RED = DANGER. It replaces floodmon's fixed 0.30 / 0.50 m, with the same 2 cm hysteresis. The reading also gets `marker_color`, and the setup gets `marker_memory`.
- **Overlay:** the remembered marker is drawn opaque at its calibrated place, with the submerged part shaded, so the flood behind it does not hide it. An enlarged copy shows the remembered photo, the bands, their heights and the reading.
- **Upside-down marker:** a marker composited upside down (red at the road) is remembered as seen, but measured the right way up (`FLIP_UPSIDE_DOWN`). This is reported as `upside_down: true`.

| Feed | Remembered bands (bottom to top) |
|---|---|
| `kauswagan` | GREEN 0.00-0.29 m, YELLOW 0.29-0.51 m, RED 0.51-1.00 m |
| `mastersons` | seen RED 0.00-0.48 / YELLOW 0.48-0.69 / GREEN 0.69-1.00 m (upside down); measured GREEN 0.00-0.31, YELLOW 0.31-0.52, RED 0.52-1.00 m |

A dry-reference capture averages 15 frames. If the clip cuts to another scene during the capture, the capture is cancelled. This matters at Masterson's, where the dry scene lasts only 3.7 s: pause on the dry scene before capturing.

Location names and coordinates are edited and saved on the same page and are stored in MongoDB. Coordinates set in `feeds.json` are applied only while the camera document has none.

## What is written to MongoDB

The camera document in `MONGODB_COLLECTION` has the id `feed-<id>` and is updated every second with:

- `water_level` and `status`. OpenCV's SAFE is stored as NORMAL; ALERT and DANGER are unchanged.
- `timestamp`
- `roi_config`
- `opencv_setup`
- a `feed` object with the OpenCV metrics: coverage, confidence, image quality, marker mode, fps, processing time and service state

The dashboard's `GET /api/cameras` reads this document like any other camera.

The readings collection, `MONGODB_READINGS_COLLECTION` (default `flood_readings`), gets one document per second per calibrated feed, plus one at once for every event: start, status change, obstruction, held data, lighting change, road condition change (`ROAD WET`, ...). Each document has:

- `timestamp`, `video_t_s`, `loop_index`
- `water_level_m`, `raw_level_m`, `status`, `road_condition`
- `flood_coverage_pct`, `confidence`, `data_valid`, `anomaly_reason`
- `marker_mode`, `level_source`, `image_quality`, `event`
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
