#!/usr/bin/env python3
"""
Score program readings against ground truth.

    python tools/evaluate.py READINGS.csv GROUND_TRUTH.csv [--skip 10] [--json report.json]

READINGS.csv     : written by FloodMonitoring.py --log-csv
GROUND_TRUTH.csv : columns t_s, level_m (optional car, shake). For a field
                   test, type the tape-measured levels with the video time of
                   each reading; sparse rows are interpolated.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from floodmon.evaluation import evaluate, format_report, load_csv  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("readings")
    p.add_argument("ground_truth")
    p.add_argument("--skip", type=float, default=0.0, help="ignore the first N seconds (start-up)")
    p.add_argument("--json", help="also write the report as JSON")
    p.add_argument("--title", default=None)
    a = p.parse_args(argv)
    rep = evaluate(load_csv(a.readings), load_csv(a.ground_truth), skip_s=a.skip)
    print(format_report(rep, a.title or os.path.basename(os.path.dirname(os.path.abspath(a.readings)))))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)


if __name__ == "__main__":
    main()
