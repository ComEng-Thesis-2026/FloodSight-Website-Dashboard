#!/usr/bin/env python3
"""
FloodMonitoring.py  -  CCTV-based flood monitoring (entry point)
================================================================
Thesis: "Development of a CCTV-Based Flood Monitoring and Routing Advisory
         System Using Computer Vision"

Run:            python FloodMonitoring.py
Other camera:   --camera laptop | iriun | dahua | file     or  --source clip.mp4
Find cameras:   python FloodMonitoring.py --list-cameras
All options:    python FloodMonitoring.py --help

Flood marker levels (defined once in floodmon/bands.py):
    GREEN   0.00 - 0.30 m   SAFE
    YELLOW  0.30 - 0.50 m   ALERT
    RED     0.50 - 1.00 m   DANGER

Two marker modes:
    PHYSICAL  a real gauge is in view; its 4 corners are fitted by hand or from
              ArUco anchor tags ('g'); the water line is read on the gauge.
    VIRTUAL   the physical gauge is used once to lock the geometry ('x'), then
              removed; the water line is read on the surface behind it.

The program is in the  floodmon/  package (see README.md for the module map).
Keys in the video window: see floodmon/app.py or README.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from floodmon.app import main  # noqa: E402
from floodmon.video import CameraSettings  # noqa: E402

# ==========================================================================
#  CAMERA SETTINGS  -  SWITCH THE CAMERA HERE
# ==========================================================================
#  CAMERA_MODE options:
#    "laptop" : built-in laptop webcam                   (testing)
#    "iriun"  : phone camera through the Iriun Webcam app (testing)
#    "dahua"  : Dahua CCTV camera over RTSP              (deployment)
#    "file"   : recorded video file (VIDEO_FILE)         (testing)
#  The command line can override it, e.g.  --camera iriun
#  Run  --list-cameras  to see which index each camera uses on this PC.
CAMERA_MODE = "laptop"  # default camera mode (can be overridden on command line)

LAPTOP_CAMERA_INDEX = 0     # laptop webcam ("USB2.0 VGA UVC WebCam" on this PC)
IRIUN_CAMERA_INDEX = 1      # "Iriun Webcam" virtual camera (open the Iriun app on PC + phone first)
DEVICE_RESOLUTION = None    # e.g. (1280, 720) to request a resolution; None = camera default

DAHUA_IP = "192.168.1.108"  # Dahua camera / NVR address
DAHUA_USER = "admin"
DAHUA_PASSWORD = ""         # leave empty and set env var DAHUA_PASSWORD instead (safer)
DAHUA_PORT = 554
DAHUA_CHANNEL = 1
DAHUA_SUBTYPE = 0           # 0 = zzzzmain stream, 1 = sub stream (lighter)

VIDEO_FILE = ""             # used when CAMERA_MODE = "file"

# Horizontal field of view of the camera lens, ONLY used for the camera
# tilt / height / distance estimates shown on screen (the water level itself
# does not depend on it). Laptop webcam ~ 60-70 deg, Dahua 2.8 mm ~ 100 deg.
CAMERA_HFOV_DEG = 70.0
# ==========================================================================

CAMERA = CameraSettings(
    mode=CAMERA_MODE, laptop_index=LAPTOP_CAMERA_INDEX, iriun_index=IRIUN_CAMERA_INDEX,
    device_resolution=DEVICE_RESOLUTION, dahua_ip=DAHUA_IP, dahua_user=DAHUA_USER,
    dahua_password=DAHUA_PASSWORD, dahua_port=DAHUA_PORT, dahua_channel=DAHUA_CHANNEL,
    dahua_subtype=DAHUA_SUBTYPE, video_file=VIDEO_FILE, hfov_deg=CAMERA_HFOV_DEG,
)

if __name__ == "__main__":
    main(cam=CAMERA)
