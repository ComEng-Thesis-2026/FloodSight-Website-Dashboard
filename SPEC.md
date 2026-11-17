---
title: Product Specification
subtitle: FloodSight Flood Monitoring Dashboard
---

# FloodSight Flood Monitoring Dashboard

## 1. Product Overview

FloodSight is a web-based flood monitoring dashboard for Cagayan de Oro City that helps authorized users monitor CCTV-based flood conditions in real time. The platform combines a live camera feed experience, a map-based overview of flood-prone locations, and role-based administrative controls for managing camera configuration and user access.

The current implementation reads camera data from MongoDB. Two camera feeds, Masterson's Avenue and Kauswagan Highway, are recorded clips from CDRRMO's Facebook Live camera feed. The OpenCV feed service plays each clip in a loop and measures it live with the OpenCV flood monitoring program (`feed-service/`). This simulates a live CCTV feed. Other cameras use a snapshot proxy or demo data.

## 2. Project Goal

Build a responsive, modern dashboard that allows users to:

- view the current flood status of multiple monitored locations,
- quickly identify areas that are normal, alert, or in danger,
- inspect camera snapshots with associated water level and timestamp data,
- access the system through authenticated sign-in,
- manage camera configuration and user roles when authorized.

## 3. Primary Users

- Viewer: can log in and view the dashboard.
- Authority: can view monitoring data and act on alerts.
- Admin: can configure cameras, manage users, and control access-related settings.

## 4. Core Functional Requirements

### 4.1 Authentication and Access Control

- The app must support Google sign-in through Firebase Authentication.
- Protected routes must redirect unauthenticated users to the login screen.
- Access to admin-only pages such as Camera Configuration and User Access must be restricted to authenticated admins.
- The app should expose a user profile page where users can review account information and sign out.

### 4.2 Dashboard Experience

- The dashboard must display a header with the FloodSight branding and navigation links for Dashboard, Camera Config, and User Access.
- The dashboard must show a summary of monitored cameras and flood status counts for Normal, Alert, and Danger.
- The dashboard must present a horizontally scrollable camera feed carousel with each camera showing its location, flood status, and water level.
- Users must be able to select a camera to view more detailed information in a focused detail panel.
- The dashboard must include a map overview that marks each monitored location with a color-coded status marker.
- Flood data should refresh automatically on a timed interval to simulate live monitoring updates.

### 4.3 Camera Monitoring Data

- Each camera entry must contain:
  - unique ID,
  - location name,
  - coordinates,
  - water level in meters,
  - flood status (NORMAL, ALERT, DANGER),
  - timestamp,
  - snapshot URL,
  - ROI configuration,
  - HSV thresholds.
- The dashboard should derive flood status from water level thresholds. They are the same bands as the OpenCV program uses on its 1 m marker: NORMAL (OpenCV: SAFE) below 0.30 m, ALERT from 0.30 m to below 0.50 m, DANGER from 0.50 m.
- The UI should display the current water level in centimeters and the most recent update time.

### 4.4 OpenCV Video Feeds

- Each feed camera must play its recorded clip as a live video in the dashboard detail panel.
- An "OpenCV overlay" toggle must show or hide the OpenCV view (ROIs, water tint, gauge, level panel). Measuring continues while the overlay is hidden.
- The detail panel must show the live OpenCV metrics:
  - water level and status
  - road flood coverage and confidence
  - rate and resolution
  - image quality, marker mode and processing speed
- The detail panel must also show a 10-minute water-level trend read from MongoDB.
- Every reading must be stored in MongoDB:
  - the camera document is updated every second
  - the `flood_readings` collection gets one document per second plus one per event, with latency fields
- Each feed must save a measurement capture on every event and every 10 s of video:
  - the snapshot (clean and annotated JPEG) and its data are saved locally in `feed-service/captures/<feed id>/`; the image is not stored in MongoDB
  - the `camera_metrics` collection gets one document per capture: water level, flood status, the OpenCV metrics, pixel and classification accuracy (computed as in `cameraTest.py`) and the latency breakdown
- On the Camera Configuration page, admins must be able to edit a feed's setup the same way as in the stand-alone OpenCV program:
  - flood ROI polygon
  - 4-corner marker ROI
  - reference height
  - dry reference capture
  - virtual marker lock, convert and unlock
  - save and restart video

### 4.5 Camera Configuration Page

- Admin users must be able to open a configuration page for each camera.
- The page must allow editing of:
  - location name,
  - latitude and longitude,
  - ROI coordinates and size,
  - HSV threshold values.
- The page must preview the ROI overlay on the camera snapshot.
- Changes must be saved and reflected in the dashboard state.

### 4.6 User Access Management

- Admin users must be able to add new users by entering a name, email, and role.
- Supported roles are:
  - admin,
  - authority,
  - viewer.
- Admin users must be able to update a user’s role and remove users from the list.
- The app should prevent the current signed-in user from removing their own account.

## 5. Visual and Interaction Requirements

- The interface should follow a clean, modern dashboard style with a water-themed blue and teal visual language.
- The application should be responsive across desktop and mobile screen sizes.
- Navigation should remain accessible through both desktop and mobile layouts.
- UI states should provide clear feedback for loading, errors, and successful actions.
- Status indicators should be visually distinct for Normal, Alert, and Danger conditions.

## 6. Technical Stack

The project should be built using the following stack:

- React 19 with TypeScript
- Vite as the build tool
- TanStack Router for application routing
- TanStack Query for async state handling where applicable
- Tailwind CSS and shadcn-style UI primitives
- Firebase Authentication for sign-in and user identity
- Leaflet for interactive map rendering
- Lucide React for icons
- Local storage-backed demo state for current prototype behavior
- MongoDB (Node `mongodb` driver) for camera data and stored readings
- Python OpenCV feed service (`feed-service/`, Python 3.13, OpenCV, NumPy, pymongo). It uses the `floodmon` package from `OpenCV-FloodMonitoring-main/` unchanged and is proxied as `/api/feeds/*`.

## 7. Data Model Notes

The current implementation uses a structured in-browser store with seeded camera and user records. Future integration should preserve the same data shape even if the storage layer is replaced with a real backend.

### Camera object

- id: string
- location: string
- coordinates: { lat: number; lng: number }
- waterLevel: number
- floodStatus: NORMAL | ALERT | DANGER
- timestamp: string
- snapshotUrl: string
- roiConfig: { x: number; y: number; width: number; height: number }
- hsvThresholds: { h_min; h_max; s_min; s_max; v_min; v_max }

### User object

- id: string
- name: string
- email: string
- role: admin | authority | viewer
- avatarUrl?: string
- addedAt: string

## 8. Constraints and Implementation Guidelines

- The current prototype should remain functional without requiring a production backend.
- New features should preserve the existing routing structure and app shell layout.
- Authentication and role-based access should be implemented in a way that supports future Firebase custom claims integration.
- Do not remove the existing dashboard narrative of real-time flood monitoring, even if the underlying data is still simulated.
- Keep the UI polished and consistent with the existing FloodSight branding.

## 9. Acceptance Criteria

- [ ] Users can sign in with Google and access the protected dashboard.
- [ ] Unauthenticated users are redirected to the login page.
- [ ] The dashboard shows camera cards, flood summary statistics, and a map overview.
- [ ] Selecting a camera updates the detail panel and map focus behavior.
- [ ] Flood data refreshes automatically without a manual page reload.
- [ ] Admin users can edit camera ROI and HSV configuration values and save them successfully.
- [ ] Admin users can add, update, and remove users with different roles.
- [ ] The profile page displays the signed-in user’s information and allows sign-out.
- [ ] The interface remains usable on mobile and desktop screens.

## 10. Out of Scope for This Version

- Live integration with real CCTV streams or external camera APIs. The OpenCV feeds play recorded CDRRMO clips in a loop to simulate live feeds.
- Real-time sensor ingestion from physical flood monitoring devices
- Full backend/database persistence beyond the existing prototype state
- Payment, reporting, or public-facing citizen portal features
- Multi-language support

