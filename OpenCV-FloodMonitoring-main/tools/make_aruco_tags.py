#!/usr/bin/env python3
"""
Printable ArUco anchor tags + a layout template.

    python tools/make_aruco_tags.py --ids 0 1 2 3 --size 0.12 --out tags/

Writes tags/tag_<id>.png (print at 100 %, 300 dpi: the black square is exactly
--size wide) and tags/tag_layout.json. Then:

1. Fix the tags on the SAME flat surface as the flood marker:
     * at least two ABOVE the highest expected water (keep the camera aligned)
     * one or two near the road beside the marker (accurate setup fit; they
       may be flooded later, they are only needed while fitting on a dry day)
   Spread them out sideways; they must face the camera and stay clean.
2. Measure each tag CENTRE with a tape:
     x_m = across from the marker's LEFT edge (right = positive, left = negative)
     h_m = height above the road surface
   Measure the printed black square too and correct size_m if needed.
3. Type the values into tag_layout.json and start the program with
       python FloodMonitoring.py --tags tags/tag_layout.json --marker-width 0.15
   then press g (fit the marker ROI from the tags), b (dry reference), s (save).
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ids", type=int, nargs="+", default=[0, 1, 2, 3])
    p.add_argument("--size", type=float, default=0.12, help="black square side (m)")
    p.add_argument("--dictionary", default="DICT_4X4_50")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", default="tags")
    a = p.parse_args(argv)
    if not hasattr(cv2.aruco, a.dictionary):
        sys.exit(f"Unknown dictionary {a.dictionary}")
    os.makedirs(a.out, exist_ok=True)
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, a.dictionary))
    side = int(round(a.size / 0.0254 * a.dpi))
    margin = max(side // 5, 40)
    for i in a.ids:
        tag = cv2.aruco.generateImageMarker(d, i, side)
        page = np.full((side + 3 * margin, side + 2 * margin), 255, np.uint8)
        page[margin:margin + side, margin:margin + side] = tag
        cv2.putText(page, f"ID {i}  {a.dictionary}  black square {a.size * 100:.1f} cm  (print 100%, {a.dpi} dpi)",
                    (margin, side + 2 * margin), cv2.FONT_HERSHEY_SIMPLEX, side / 1400, 0, max(1, side // 500))
        path = os.path.join(a.out, f"tag_{i}.png")
        cv2.imwrite(path, page)
        print("wrote", path)
    layout = {"dictionary": a.dictionary, "size_m": a.size,
              "tags": {str(i): {"x_m": 0.0, "h_m": 0.0} for i in a.ids},
              "_help": "x_m: tag centre across from the marker's LEFT edge (m); h_m: tag centre above the road (m)"}
    path = os.path.join(a.out, "tag_layout.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2)
    print("wrote", path, "- fill in the measured x_m / h_m of every tag centre")


if __name__ == "__main__":
    main()
