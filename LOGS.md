Date: 07/18/2026                   Time: 10:40 AM
Changes Made:
- Added MongoDB support and helper to read camera documents (`src/lib/db.server.ts`).
- Added a backend API endpoint `/api/cameras` in `src/server.ts` to return normalized camera feeds.
- Implemented `getCameraFeedsApi()` wrapper in `src/lib/api/camera.functions.ts` to call the DB helper.
- Updated `.env` values to point to the correct database: `MONGODB_DB="CCTVs"` and collection name.
Failures Flag:
- Initial `.env` pointed to the wrong DB/collection (fixed by updating to the cluster that contains `cctv data`).
- MongoDB collection naming varied ("cctv data" vs `cctv_data`) — mapping and detection logic added in `db.server.ts` to find the best collection.

Date: 07/19/2026                   Time: 11:05 AM
Changes Made:
- Fixed a React route bug that caused an infinite "Redirecting to sign in…" loop by patching `src/routes/_auth.tsx`:
  - Added `useLocation()` check and an `isLoginRoute` guard so the layout does not redirect when already on `/login`.
  - Ensured the login route is rendered via `<Outlet />` for unauthenticated users on `/login`.
- Removed an invalid dependency from a `useEffect` in `src/routes/_auth.index.tsx` (removed `fetchCameraFeeds` from the dependency array) to avoid referencing a non-existent identifier during refresh logic.
- Confirmed `/api/cameras` returns valid JSON (verified via local HTTP request).
Failures Flag:
- There was a runtime error from TanStack Start complaining about an "Invalid server function ID" (plugin validation). This was caused by leftover server-function IDs from earlier code-generation; the project was migrated to use a direct `/api/cameras` handler to bypass the broken server function. The error surfaced during dev HMR but the API endpoint itself is reachable.
- Some dev-server / terminal command invocations failed due to PowerShell vs bash quoting differences when running inline Node commands; these are environmental and not code regressions.

Date: 07/19/2026                   Time: 11:40 AM
Changes Made:
- Miscellaneous edits to ensure HMR and route updates pick up correctly (multiple hot-reload cycles while testing the auth fix).
Failures Flag:
- Remaining items to watch:
  - TanStack Start server-function leftover warnings: if you still see `Invalid server function ID` during development, consider cleaning any generated server-fn artifacts or disabling the server-fn CSRF warning as documented by the plugin.
  - Verify final mapping of the Divisoria camera to the intended mock feed once you approve showing live DB data for that camera.

Notes:
- If you want this LOG to include diffs per-file (git-style) or the full chronological transcript of terminal outputs, I can append those sections — tell me which format you prefer.

Date: 07/19/2026                   Time: 11:55 AM
Changes Made:
- Added a Vite dev-only auth fallback to `src/context/AuthContext.jsx` that reads `localStorage.floodsight-state-v1` and simulates an authenticated user for local testing without Google sign-in.
- Ensured the dev fallback user includes a `name` property to avoid avatar rendering errors in `src/components/floodsight/AppShell.tsx`.
- Reworked the DB-to-mock-camera mapping in `src/routes/_auth.index.tsx` to map each DB document to the nearest mock camera by squared distance (with a tighter acceptance threshold) so the Divisoria DB record correctly updates the intended `cam_002` feed.
- Restarted the dev server and verified `/api/cameras` returns the normalized DB document and that the dashboard UI shows the DB-fed water level in the camera carousel and details pane.

Failures Flag:
- The TanStack Start server-function warnings remain in dev HMR logs ("Invalid server function ID"). These are non-blocking for the `/api/cameras` endpoint but should be cleaned separately by removing leftover server-fn artifacts or adjusting plugin settings.

Verification:
- Confirmed via HTTP: `GET /api/cameras` returns the expected JSON document from MongoDB (sample id: "6a5739e3639645ea3345c895").
- Confirmed in running app: Dashboard loads under `http://localhost:8081/` and the Divisoria camera feed displays the DB water level after the mapping change.

Next Steps (optional):
- The Divisoria camera feed is now synchronized and working with the MongoDB payload.
- The remaining two camera feeds are still under development and were not changed in this pass.
- If you prefer deterministic mapping later, add explicit camera ids in the DB documents and match by id instead of spatial proximity.

Date: 07/19/2026                   Time: 11:20 PM
Changes Made:
- Added server-side camera snapshot proxy support in `src/server.ts` with two routes:
  - `POST /api/cameras/:id/snapshot/test` for previewing a new camera IP before saving.
  - `GET /api/cameras/:id/snapshot` for dashboard proxying of persisted camera snapshots.
- Added `StreamConfig` support to camera data and MongoDB persistence in `src/lib/floodsight/types.ts` and `src/lib/db.server.ts`.
- Implemented digest/basic camera auth and image validation in `src/lib/camera-stream.server.ts`.
- Added camera connection settings and snapshot preview to `src/routes/_auth.config.tsx`.
- Updated dashboard snapshot sources in `src/routes/_auth.index.tsx` to use the live proxy when `streamConfig.ipAddress` is configured.
- Fixed SSR module resolution by importing `./lib/camera-stream.server.ts` explicitly in `src/server.ts`.
Failures Flag:
- No failures observed after rebuild; the fix was required because the SSR bundler could not resolve the implicit `.server` module path.
Verification:
- Successfully rebuilt the app with `npm run build` after the server import fix.
- Clean up leftover TanStack server-fn artifacts or silence the CSRF/server-fn warnings per plugin guidance when time allows.

Date: 07/19/2026                   Time: 09:20 PM
Changes Made:
- Replaced the old admin-mode toggle flow with a dedicated admin account rule keyed off the email `langgamen.carlsyker@gmail.com` in `src/context/AuthContext.jsx` and `src/lib/floodsight/store.ts`.
- Removed the profile-page admin-mode switch and made the profile avatar fall back to the signed-in Google profile photo when available in `src/pages/ProfilePage.jsx`.
- Wired the header profile avatar and profile page image rendering to use `user.photoURL` so Google profile pictures appear consistently in the dashboard UI.
- Reworked the User Access page to use the persisted user roster for real account-role management instead of the previous mock-only role view in `src/routes/_auth.users.tsx`.
- Enabled admin camera changes to update the local dashboard state immediately and push the update to the MongoDB-backed API endpoint via `src/routes/_auth.config.tsx`, `src/lib/db.server.ts`, and `src/server.ts`.
- Added store helpers to upsert users from authenticated Google accounts and retain role updates across sessions.
Failures Flag:
- The prior local auth flow relied on a manual admin toggle and local mock roles; those have been removed in favor of email-based role resolution and persisted auth-backed user records.
Verification:
- Built the app successfully with `npm run build` after the changes.

Date: 07/19/2026                   Time: 10:05 PM
Changes Made:
- Added runtime debug logging of `firebaseConfig` and exported it from `src/services/firebase.js` to capture the effective `authDomain` at sign-in time.
- Updated `src/context/AuthContext.jsx` to use the service `loginWithGoogle` helper (which now logs config) instead of calling `signInWithPopup` directly.
- Cleaned `.env` Firebase variables by removing surrounding quotes so Vite injects correct values at build time.
- Rebuilt the project to ensure the client bundle reflects the corrected `authDomain`.
Failures Flag:
- In the VS Code built-in browser the Firebase popup fails: the handler URL resolves to `https://localhost/__/auth/handler?...` and the popup reports `auth/popup-closed-by-user` due to COOP/COEP and origin isolation in the webview (connection refused). This does not occur in external browsers.

Verification:
- DevTools console shows `[firebase] loginWithGoogle error: FirebaseError: Firebase: Error (auth/popup-closed-by-user).` and a Cross-Origin-Opener-Policy warning when running inside the VS Code webview.
- Signing in via an external browser (Chrome/Firefox) succeeds once `localhost` is authorized in the Firebase Console and the dev bundle is uncached.

Next Steps:
- Recommended: use an external browser for interactive Google sign-in during development.
- Optional: implement an automatic fallback to `signInWithRedirect` when popup-based sign-in fails, or show a UI prompt instructing users to open the page externally. Tell me which you prefer and I will implement it.

Date: 08/13/2026                   Time: 03:16 PM
Changes Made:
- Extracted the dashboard's live-camera polling logic into a shared hook (`src/lib/floodsight/useLiveCameras.ts`), fixing a bug where the previous merge logic only ever updated a hardcoded `cam_002` id regardless of the real MongoDB document id; the live `/api/cameras` result is now used directly as the camera list (falling back to local seed cameras only if the fetch fails or is empty).
- Moved the "Map overview" section out of the main dashboard into its own route/tab, `/map-view` (`src/routes/_auth.map-view.tsx`), visible to both admins and regular users via a new "Map View" entry in `src/components/floodsight/AppShell.tsx`.
- Added a `focusId` prop to `src/components/floodsight/CameraMap.tsx` so the map can fly to and open the popup for a specific camera (used for deep-linking from notifications via a `?camera=<id>` search param).
- Extracted the color legend into a shared `src/components/floodsight/DangerLegend.tsx` component.
- Added an interactive notification sidebar (`src/components/floodsight/NotificationSidebar.tsx`) on the right side of the dashboard, listing cameras sorted by severity (Danger/Alert/Normal) with status-colored indicators; persistent column on desktop, collapses to a bell-triggered slide-over (shadcn `Sheet`) on narrower screens. Clicking a notification navigates to the Map View tab and locates that camera.
- Reworked `src/routes/_auth.index.tsx` to use the shared hook and new two-column (content + sidebar) layout.
- Removed the "HSV Thresholds" section from the Camera Configuration tab (`src/routes/_auth.config.tsx`) and adjusted the panel layout (`lg:self-start`) so the now-shorter form column no longer stretches into empty space next to the taller snapshot editor.
- Replaced the Camera Configuration tab's camera picker (21 full-text pills wrapping across 4 rows) with a compact numbered grid, each showing a flood-status-colored dot and the full location as a tooltip, plus a filter/search box and a "Selected: <location>" caption.
Failures Flag:
- None observed; `npx tsc --noEmit` and `eslint` show no new errors beyond pre-existing issues in untouched files (`.jsx` modules without type declarations, `src/server.ts`, `src/lib/db.server.ts`, existing `StreamConfig` typing in `_auth.config.tsx`).
Verification:
- Confirmed `/`, `/map-view`, and `/config` all return HTTP 200 on the local dev server (`http://localhost:8081`).
- Could not click through the authenticated UI end-to-end in this session (Google sign-in required); recommend a manual pass to confirm the Map View tab, sidebar notification-click navigation, the mobile drawer at narrow widths, and the new camera picker grid/filter.

Date: 11/03/2026                   Time: 06:05 PM
Changes Made:
- Fixed the water level staying frozen at its peak during a flood RECESSION (level only came down after the flood had fully disappeared), in both Kalman implementations:
  - `src/FloodMonitoring2.py` (`FloodKalman`): added a sustained-descent "follow" mode. A receding water line keeps moving, so the old steady-confirmation could never accept the fall (readings are never steady at one level while the line slides) and the reported level stayed at the peak until the flood was gone. Now, once raw readings slide continuously downward for `--fall-trend` seconds (new option, default 1.5 s), the filter follows the water line down; occlusions (car/person) only ever RAISE the reading, so a persistent slide is real recession. The filter's rate state is seeded with the observed slide slope for smooth tracking. Also: a steady fall no longer doubles its confirmation time when the road flood coverage does not drop in agreement (the road stays wet long after the line recedes), and the `persist_s` requirement is now based on motion in the recent window instead of the episode-wide `episode_motion` latch (one car at the start of an event used to force 20 s for the whole episode).
  - `FloodMonitoring.py` (`FloodKalman`): the innovation gate now only rejects sudden RISES (occlusions raise the reading, they never lower it); sudden drops are applied immediately, so a fast recession (or a car revealing the true line as it passes) tracks in real time instead of being held as a "sudden drop" anomaly. A persisted anomaly whose held median sits clearly BELOW the level is re-locked even without stationarity.
- Updated both class docstrings and the `--fall-confirm` help text accordingly.
Failures Flag:
- None observed. Synthetic tests cover recession tracking, car-spike rejection, flood-disappearance, and rise confirmation for both filters (all pass).
Verification:
- Synthetic filter test (10 fps simulated readings): gradual recession 0.80 -> 0.05 m now tracks (0.37 m halfway, < 0.10 m near the end) where the old logic held 0.80 m; a 1.5 s car spike to 1.00 m is still rejected in both filters; a raw drop to 0 m reaches 0.13 m within ~4 s (FM2) / ~2 s (FM1) instead of waiting persist_s = 20 s; a steady rise to 0.30 m still confirms within ~4 s.
- `FloodDetection_Video.py` forwards the new `--fall-trend` option via `fm.parse_args(rest)` with no changes needed.
Next Steps:
- Re-run `FloodDetection_Video.py` on the flood_test_2 / flood_test_3 / mastersons_flood footage and compare the new results CSVs against today's earlier runs to confirm the recession tracks on real footage.

Date: 11/10/2026                   Time: 04:40 PM
Changes Made:
- Integrated the OpenCV flood monitoring program (OpenCV-FloodMonitoring-main/floodmon, used unchanged as a library) into the dashboard as two recorded CDRRMO camera feeds: Masterson's Avenue (`mastersons`) and Kauswagan Highway (`kauswagan`).
- New Python feed service `feed-service/` (`npm run feeds`, http://127.0.0.1:8765):
  - `worker.py`: one headless worker per feed. It plays the clip in real time and in a loop, skipping frames if processing falls behind, and runs floodmon's `Monitor` on every frame. It publishes a clean JPEG and an annotated one (`draw_overlay`) plus the payload.
    - Each loop restarts the Kalman filter and status, so the dry scene reads 0 m again.
    - Setup commands are applied between frames: set ROIs / reference height, dry reference (`b`), virtual marker lock/convert (`x`), unlock (Shift+X), save (`s`), restart video.
  - `mongo_sink.py`: updates the camera document `feed-<id>` in `MONGODB_COLLECTION` every second. Fields: `water_level`, `status` (SAFE→NORMAL), `roi_config`, `opencv_setup`, and `feed` metrics.
    - Also inserts one reading per second plus one per event into `flood_readings` (`MONGODB_READINGS_COLLECTION`). Each reading has latency fields (`processing_ms`, `camera_update_ms`, `frame_to_write_ms`).
    - Location and coordinates are only set on insert, so edits on /config are kept.
  - `server.py`: local HTTP API with MJPEG stream (`?overlay=1|0`), frame, reading, setup GET/PUT (validated, 400 with problems) and actions.
- `src/lib/feed-service.server.ts` + `src/server.ts`: `/api/feeds/*` proxies to the feed service, streams included. Added `GET /api/cameras/:id/readings?minutes=10` (`getFeedReadings` in `src/lib/db.server.ts`).
- `src/lib/floodsight/types.ts`: `statusFromLevel` now uses the OpenCV bands (ALERT ≥ 0.30 m, DANGER ≥ 0.50 m; previously 0.5 / 1.5 m). Added `FeedInfo`, `FeedPayload`, `FeedSetup` and `FeedHistoryPoint`; `Camera.feed` is optional.
- `src/lib/db.server.ts`: maps the `feed` sub-document. Feed snapshots come from `/api/feeds/<id>/frame.jpg`. A /config save no longer overwrites a feed camera's live fields. Camera `_id` is typed as `ObjectId | string`.
- Dashboard (`src/routes/_auth.index.tsx`):
  - Feed cards show a still frame and an "OpenCV" badge.
  - The detail panel plays the live stream (`LiveFeedPlayer`) with an "OpenCV overlay" switch; the switch only changes the display.
  - Live OpenCV metrics (`FeedMetrics`, 1 s) and a 10-minute water-level trend from MongoDB (`WaterLevelTrend`).
  - Fixed the empty-state button linking to `/_auth/config` (now `/config`).
- Camera Config (`src/routes/_auth.config.tsx`): feed cameras open `FeedSetupEditor`:
  - draggable flood polygon (add/remove points) and 4-corner marker on the live video
  - reference height, Apply ROI, Capture dry reference, Restart video, Lock/Convert virtual marker, Unlock, Save
  - location/coordinates saved via PATCH as before
- New tokens `--roi-flood` and `--roi-marker` (light and dark) in `src/styles.css`. npm scripts `feeds` and `test:feeds`. `.env.example` and `.gitignore` updated (videos stay local). `SPEC.md` updated.
Failures Flag:
- Overlay leaked into the clean view: `enhance_for_display` can return the same array, and `draw_overlay` draws in place. Fixed by encoding the clean frame first and drawing on a copy; a regression test was added.
- Flaky test found: a missing-video feed changed state before its "Video not found" frame was published. The placeholder is now published first; six repeated runs then passed.
- The two edited CDRRMO clips are not on this PC yet: both feeds show "Video not found" until `feed-service/videos/mastersons.mp4` and `kauswagan.mp4` are added. Map coordinates are still placeholders (dashboard default 8.482, 124.643).
- A synthetic test clip rising 5.6 cm/s was held as a "sudden spike" by floodmon's Kalman filter (by design); the tests use floodmon's standard `quick` scene instead.
- Not changed: pre-existing type errors in `src/server.ts` (Buffer body) and `src/routes/_auth.config.tsx` (StreamConfig, `.jsx` declarations). /api/feeds setup and actions, like the existing PATCH /api/cameras, have no server-side admin check (admin is enforced in the UI only).
Verification:
- `npm run test:feeds`: 13 tests pass. They cover validation, MongoDB document mapping, events, measuring/streaming/looping on a synthetic flood (reaches SAFE→ALERT→DANGER), loop reset, all commands, the missing-video state and the HTTP endpoints including MJPEG.
- Stand-in run with `Downloads/mastersons_flood.mp4`: 30 fps real time, about 18–21 ms per frame, no skipped frames, about 10 stream fps. This went through the Vite proxy, with MongoDB pointed at temporary collections that were dropped afterwards.
  - The camera documents and readings were written. `/api/cameras` returned both feeds; the readings endpoint returned the stored history.
  - The setup PUT, invalid-setup 400, guarded dry-reference capture and restart behaved as expected.
- Playwright screenshots of the dashboard (overlay on/off, metrics, trend), Camera Config editor and mobile width: no console errors.
- `npm run build` succeeds; ESLint is clean on the changed files.
Next Steps:
- Copy the two edited clips into `feed-service/videos/`, add the coordinates, then do the first-time setup of each feed on /config (see `feed-service/README.md`).
