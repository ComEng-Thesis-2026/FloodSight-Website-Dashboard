#!/usr/bin/env python3
"""
Render a synthetic flood video with exact ground truth.

    python tools/make_synthetic_video.py physical out/synth_physical
    python tools/make_synthetic_video.py virtual  out/synth_virtual
    python tools/make_synthetic_video.py tags     out/synth_tags

Each output folder gets:  scene.mp4, ground_truth.csv, reference_dry_day.png,
flood_config.json (ROIs fitted exactly) and scene.json (scene settings).
Then run the monitor on it and score it:

    python FloodMonitoring.py --source out/synth_physical/scene.mp4 \
        --config out/synth_physical/flood_config.json --fast --no-display \
        --log-csv out/synth_physical/readings.csv
    python tools/evaluate.py out/synth_physical/readings.csv out/synth_physical/ground_truth.csv
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from floodmon.synthetic import PRESETS, preset, write_scene  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("preset", choices=sorted(PRESETS))
    p.add_argument("out_dir")
    p.add_argument("--duration", type=float, help="clip length (s)")
    p.add_argument("--noise", type=float, help="sensor noise std (grey levels)")
    p.add_argument("--seed", type=int)
    a = p.parse_args(argv)
    over = {k: v for k, v in (("duration_s", a.duration), ("noise_std", a.noise), ("seed", a.seed)) if v is not None}
    paths = write_scene(preset(a.preset, **over), a.out_dir)
    for k, v in paths.items():
        print(f"{k:13s} {v}")


if __name__ == "__main__":
    main()
