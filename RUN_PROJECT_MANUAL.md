# FloodSight Project Run Manual

This manual covers starting, using, stopping, and troubleshooting the complete FloodSight dashboard and OpenCV flood monitoring system.

## 1. What the project runs

The project consists of two programs:

| Program | Command | Address |
|---|---|---|
| Dashboard | `npm run dev` | http://localhost:8080 |
| OpenCV feed service | `npm run feeds` | http://127.0.0.1:8765 |

Both programs must remain running. Start the feed service first, then start the dashboard.

## 2. Requirements

Install the following before the first run:

- Node.js 22 or newer
- npm 11 or newer
- Python 3.13
- The project virtual environment in `.venv`
- MongoDB Atlas access, if database features are required

The project includes `.venv`, `node_modules`, two recorded video feeds, and saved OpenCV setups. The recommended setup is therefore ready for a normal run.

## 3. Open the project

Open PowerShell and change to the project root:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring"
```

Confirm that the required directories and files exist:

```powershell
Test-Path .\.venv\Scripts\python.exe
Test-Path .\node_modules
Test-Path .\feed-service\videos\mastersons.mp4
Test-Path .\feed-service\videos\kauswagan.mp4
Test-Path .\.env
```

All commands should return `True`.

## 4. First-time installation

If dependencies were not previously installed or a dependency is missing, run:

```powershell
npm install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\OpenCV-FloodMonitoring-main\requirements.txt
.\.venv\Scripts\python.exe -m pip install pymongo
```

Do not replace the existing `.venv` unless dependency repair is necessary.

## 5. Configure environment variables

Copy the example file only if `.env` does not already exist:

```powershell
Copy-Item .env.example .env
```

Configure `.env` with a text editor:

```text
VITE_FIREBASE_API_KEY=
VITE_FIREBASE_AUTH_DOMAIN=
VITE_FIREBASE_PROJECT_ID=
VITE_FIREBASE_STORAGE_BUCKET=
VITE_FIREBASE_MESSAGING_SENDER_ID=
VITE_FIREBASE_APP_ID=

MONGODB_URI=
MONGODB_DB=CCTVs
MONGODB_COLLECTION=cctv data
MONGODB_READINGS_COLLECTION=flood_readings
MONGODB_METRICS_COLLECTION=camera_metrics

FEED_SERVICE_URL=http://127.0.0.1:8765
FEED_SERVICE_PORT=8765
```

Keep real database credentials private. Never commit `.env` or MongoDB credentials to Git.

To run the feed service without MongoDB, use the `--no-db` option described below.

## 6. Start the OpenCV feed service

Open the first PowerShell terminal and run:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring"
npm run feeds
```

Wait until the terminal shows:

- `Feed service on http://127.0.0.1:8765/feeds`
- `playing mastersons.mp4`
- `playing kauswagan.mp4`
- MongoDB status, if database access is enabled

Keep this terminal open.

### Feed service without database writes

```powershell
.\.venv\Scripts\python.exe feed-service\server.py --no-db
```

The two video feeds will still run and their files remain available locally.

### Optional feed service arguments

```powershell
.\.venv\Scripts\python.exe feed-service\server.py --port 8765 --fast --no-captures
```

Available options:

| Option | Purpose |
|---|---|
| `--no-db` | Disable MongoDB reads and writes |
| `--port N` | Change the feed service port |
| `--fast` | Disable real-time video pacing |
| `--capture-interval S` | Set seconds between interval captures |
| `--no-captures` | Disable snapshot and metrics capture |

## 7. Start the dashboard

Open a second PowerShell terminal and run:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring"
npm run dev
```

Wait for Vite to report:

```text
VITE ... ready
Local: http://localhost:8080/
```

Open:

```text
http://localhost:8080/
```

Keep this terminal open while using the dashboard.

## 8. Verify the project

Check the feed service from another PowerShell terminal:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8765/feeds" | ConvertTo-Json -Depth 5
```

Both `mastersons` and `kauswagan` should have a state of `running`.

Check the dashboard:

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8080/" -UseBasicParsing
```

The response status should be `200`.

## 9. Using the dashboard

### Video feeds

The dashboard displays:

- Masterson's Avenue
- Kauswagan Highway

The recorded video feeds loop continuously and display OpenCV flood readings.

### Camera Configuration

Use the Camera Config page to:

1. Select a feed.
2. Adjust the cyan flood ROI over the road.
3. Adjust the magenta marker ROI when using a physical marker.
4. Set the reference height.
5. Apply or save the setup.
6. Capture or restore the dry reference.

For the supplied demo clips, the saved setups and marker memory are loaded automatically at startup.

## 10. OpenCV standalone program

To run the standalone OpenCV window instead of or alongside the dashboard service:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring\OpenCV-FloodMonitoring-main"
..\.venv\Scripts\python.exe FloodMonitoring.py --list-cameras
..\.venv\Scripts\python.exe FloodMonitoring.py --source clip.mp4
```

The standalone program accepts the following common options:

| Option | Purpose |
|---|---|
| `--camera dahua` | Use a Dahua CCTV source |
| `--camera laptop` | Use the laptop camera |
| `--camera iriun` | Use an iPhone/Iriun source |
| `--source clip.mp4` | Use a recorded video |
| `--list-cameras` | List available camera indexes |
| `--fast` | Disable real-time pacing |
| `--no-display` | Run without an OpenCV window |
| `--help` | Show all standalone options |

Keys in the standalone window include:

| Key | Action |
|---|---|
| `f` / `m` | Edit the flood or marker ROI |
| `b` | Capture the dry reference |
| `x` / `Shift+X` | Lock virtual marker / unlock |
| `s` | Save the setup |
| `w` | Toggle the water overlay |
| `v` | Toggle the mask view |
| `e` | Toggle display enhancement |
| `p` | Pause or resume |
| `q` | Quit |

## 11. Run tests

Run the feed service test suite:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring"
npm run test:feeds
```

The suite uses the synthetic flood system and does not require MongoDB.

Run the standalone OpenCV tests:

```powershell
cd "C:\Users\ADMIN\Desktop\Thesis\Website Dashboard\flood-monitoring\OpenCV-FloodMonitoring-main"
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 12. Stop the project

Press `Ctrl+C` in each open terminal.

Stop the dashboard terminal first, then stop the feed service terminal.

## 13. Troubleshooting

### Port 8080 is unavailable

Find the actual Vite URL in the dashboard terminal. Vite may use another free port.

### Port 8765 is unavailable

Another feed service may already be running. Stop the existing process, or start the service on a new port:

```powershell
.\.venv\Scripts\python.exe feed-service\server.py --port 8766
```

If port 8766 is used, update `FEED_SERVICE_URL` in `.env` to:

```text
FEED_SERVICE_URL=http://127.0.0.1:8766
```

### Video not found

Confirm these files exist:

- `feed-service\videos\mastersons.mp4`
- `feed-service\videos\kauswagan.mp4`

The feed service retries missing videos every 10 seconds.

### Missing Python package

Install the standalone requirements into the project environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\OpenCV-FloodMonitoring-main\requirements.txt
```

Install MongoDB support separately if required:

```powershell
.\.venv\Scripts\python.exe -m pip install pymongo
```

### MongoDB connection failure

1. Confirm `MONGODB_URI` is set in `.env`.
2. Confirm the Atlas network access and database credentials.
3. Restart the feed service.

The service can be tested without MongoDB using `--no-db`.

### Dashboard cannot reach the feed service

Confirm both services are running. Then verify:

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/feeds"
```

If `FEED_SERVICE_URL` was changed in `.env`, restart `npm run dev`.

## 14. Verified project state

The project was verified with:

- 25 feed-service tests passing
- Both `mastersons` and `kauswagan` in `running` state
- Feed API available at http://127.0.0.1:8765
- Dashboard available at http://localhost:8080
- Dashboard root response status `200`
