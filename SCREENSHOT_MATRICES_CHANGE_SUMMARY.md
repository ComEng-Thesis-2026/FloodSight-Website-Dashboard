# Screenshot With Matrices Feature

Date: November 22, 2026

## What Changed

- Added a **Save Screenshot With Matrices** action to the Camera Configuration page for live OpenCV feeds.
- The action captures both a clean screenshot and an annotated screenshot at the same moment.
- Clean and annotated JPEG images are saved locally in `Snapshots/<feed-id>/`.
- Capture matrix data is written to the MongoDB `camera_metrics` collection.
- Added a manual-capture test that verifies the `Snapshots/` output location and capture metadata.
- Updated Camera Config labels and descriptions to use the project capitalization rule.
- Documented the feature in `SPEC.md` and `LOGS.md`.

## What It Saves

Each manual capture includes:

- Timestamp and capture ID
- Camera ID and location
- Water level and raw water level
- Flood status
- Flood coverage and confidence
- Flood ROI and marker ROI setup
- Reference height and virtual marker level
- Marker mode
- OpenCV image-quality metrics
- Pixel and classification accuracy
- Processing and database latency
- Paths to the clean and annotated JPEG files

Image bytes are not stored in MongoDB. MongoDB stores the matrix document and local image paths.

## How To Use

1. Start the dashboard: `npm run dev`.
2. Start the OpenCV feed service: `npm run feeds`.
3. Open Camera Configuration and select a running feed.
4. Adjust the flood ROI or marker ROI if needed.
5. Click **Save Screenshot With Matrices**.

The files appear under:

```text
Snapshots/<feed-id>/<capture-id>.jpg
Snapshots/<feed-id>/<capture-id>_processed.jpg
Snapshots/<feed-id>/<capture-id>.json
Snapshots/<feed-id>/summary.csv
```

The matching matrix document is inserted into MongoDB collection `camera_metrics`.

## Important Notes

- The feed service must be restarted after Python source changes.
- Use port `8765` for the feed service.
- The feed must be running and calibrated before manual captures are available.
- Automatic captures continue to save under `feed-service/captures/`.
- `Snapshots/` is ignored by Git so teammate machines do not receive generated screenshots.

## Verification

- Feed service tests: passed, including the manual `Snapshots/` recorder test.
- Production build: passed.
