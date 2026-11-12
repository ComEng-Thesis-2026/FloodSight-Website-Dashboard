"""
Accuracy evaluation: program readings vs ground truth.

Readings: the CSV written with --log-csv (columns t_s, water_level_m, raw_level_m,
status, data_valid ...). Ground truth: a CSV with t_s and level_m (and optional
status / car / shake columns), e.g. from a synthetic scene or from tape-measured
levels typed in during a field test. Rows are matched by video time t_s; a
ground-truth file with only a few rows (manual readings) is interpolated.

Reported (for the thesis results chapter):
  * MAE, RMSE, bias, P95 and max absolute error of the filtered level (and raw)
  * the same per true flood band (GREEN / YELLOW / RED)
  * status accuracy + confusion matrix
  * warning delay: time from the true crossing of 0.30 m / 0.50 m to the first
    ALERT / DANGER reading
  * share of frames with valid data, and errors during events (car, shake)
"""

from __future__ import annotations

import csv
import math

import numpy as np

from .bands import FLOOD_BANDS, STATUS_ORDER, band_boundaries, classify_level


def _f(v, default=math.nan):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def align(readings: list[dict], truth: list[dict], tol_s: float | None = None) -> dict:
    """Match each reading to the ground truth at the same video time."""
    t_r = np.array([_f(r.get("t_s")) for r in readings])
    t_g = np.array([_f(g.get("t_s")) for g in truth])
    lv_g = np.array([_f(g.get("level_m")) for g in truth])
    order = np.argsort(t_g)
    t_g, lv_g = t_g[order], lv_g[order]
    truth = [truth[i] for i in order]
    dense = len(t_g) > 1 and np.median(np.diff(t_g)) < 1.0
    if tol_s is None:
        tol_s = float(np.median(np.diff(t_g))) * 0.6 if dense else math.inf
    out = {k: [] for k in ("t", "true", "level", "raw", "status", "valid", "car", "shake")}
    for r, t in zip(readings, t_r):
        if not math.isfinite(t) or t < t_g[0] - tol_s or t > t_g[-1] + tol_s:
            continue
        lvl = _f(r.get("water_level_m"))
        if not math.isfinite(lvl):
            continue
        if dense:
            i = int(np.clip(np.searchsorted(t_g, t), 1, len(t_g) - 1))
            i = i if abs(t_g[i] - t) < abs(t_g[i - 1] - t) else i - 1
            if abs(t_g[i] - t) > tol_s:
                continue
            true, g = lv_g[i], truth[i]
        else:                                        # sparse manual readings: interpolate
            true, g = float(np.interp(t, t_g, lv_g)), {}
        out["t"].append(t)
        out["true"].append(true)
        out["level"].append(lvl)
        out["raw"].append(_f(r.get("raw_level_m"), lvl))
        out["status"].append(r.get("status") or classify_level(lvl))
        out["valid"].append(str(r.get("data_valid", "1")).strip() not in ("0", "False", "false"))
        out["car"].append(str(g.get("car", "0")) in ("1", "True", "true"))
        out["shake"].append(str(g.get("shake", "0")) in ("1", "True", "true"))
    return {k: np.array(v) for k, v in out.items()}


def error_stats(pred, true) -> dict:
    if len(pred) == 0:
        return dict(n=0)
    e = np.asarray(pred, float) - np.asarray(true, float)
    a = np.abs(e)
    return dict(n=int(len(e)), mae_cm=round(100 * float(a.mean()), 2),
                rmse_cm=round(100 * float(np.sqrt((e * e).mean())), 2),
                bias_cm=round(100 * float(e.mean()), 2),
                p95_cm=round(100 * float(np.percentile(a, 95)), 2),
                max_cm=round(100 * float(a.max()), 2))


def warning_delays(t, true, status) -> dict:
    """Seconds from the true (rising) crossing of each band boundary to the first reading at
    that status or higher. Negative = early warning (false alarm side)."""
    res = {}
    for b in FLOOD_BANDS[1:]:
        idx_true = np.flatnonzero(true >= b.from_m)
        key = f"{b.status}_at_{b.from_m:.2f}m"
        if idx_true.size == 0:
            res[key] = None
            continue
        t_true = float(t[idx_true[0]])
        rank = STATUS_ORDER.index(b.status)
        hit = [i for i, s in enumerate(status) if s in STATUS_ORDER and STATUS_ORDER.index(s) >= rank]
        res[key] = round(float(t[hit[0]]) - t_true, 2) if hit else None
    return res


def evaluate(readings: list[dict], truth: list[dict], skip_s: float = 0.0) -> dict:
    d = align(readings, truth)
    if len(d["t"]) == 0:
        raise ValueError("No readings could be matched to the ground truth (check the t_s columns).")
    keep = d["t"] >= skip_s
    d = {k: v[keep] for k, v in d.items()}
    valid = d["valid"]
    normal = valid & ~d["car"] & ~d["shake"]
    true_status = np.array([classify_level(v) for v in d["true"]])
    rep = dict(
        frames=int(len(d["t"])),
        valid_pct=round(100 * float(valid.mean()), 1),
        filtered=error_stats(d["level"][valid], d["true"][valid]),
        raw=error_stats(d["raw"][valid], d["true"][valid]),
        normal_frames=error_stats(d["level"][normal], d["true"][normal]),
        during_car=error_stats(d["level"][valid & d["car"]], d["true"][valid & d["car"]]),
        during_shake=error_stats(d["level"][valid & d["shake"]], d["true"][valid & d["shake"]]),
        per_band={},
        status_accuracy_pct=round(100 * float((true_status[valid] == d["status"][valid]).mean()), 1)
        if valid.any() else None,
        confusion={},
        warning_delay_s=warning_delays(d["t"], d["true"], d["status"]),
        boundaries_m=band_boundaries(),
    )
    for b in FLOOD_BANDS:
        sel = valid & (true_status == b.status)
        rep["per_band"][f"{b.color_name} {b.from_m:.2f}-{b.to_m:.2f} m"] = error_stats(d["level"][sel], d["true"][sel])
    for ts in STATUS_ORDER:
        row = {}
        for ps in STATUS_ORDER:
            row[ps] = int((valid & (true_status == ts) & (d["status"] == ps)).sum())
        rep["confusion"][ts] = row
    return rep


def format_report(rep: dict, title: str = "") -> str:
    def stats(s):
        if not s or s.get("n", 0) == 0:
            return "n=0"
        return (f"n={s['n']:5d}  MAE {s['mae_cm']:6.2f} cm  RMSE {s['rmse_cm']:6.2f}  bias {s['bias_cm']:+6.2f}"
                f"  P95 {s['p95_cm']:6.2f}  max {s['max_cm']:6.2f}")

    lines = [f"== {title}" if title else "== evaluation",
             f"frames {rep['frames']}   valid data {rep['valid_pct']}%",
             f"filtered level   {stats(rep['filtered'])}",
             f"raw reading      {stats(rep['raw'])}",
             f"normal frames    {stats(rep['normal_frames'])}",
             f"during car       {stats(rep['during_car'])}",
             f"during shake     {stats(rep['during_shake'])}"]
    for k, s in rep["per_band"].items():
        lines.append(f"  {k:22s} {stats(s)}")
    lines.append(f"status accuracy  {rep['status_accuracy_pct']}%")
    lines.append("confusion (rows = true, cols = reported): " + "  ".join(STATUS_ORDER))
    for ts, row in rep["confusion"].items():
        lines.append(f"  {ts:7s} " + "  ".join(f"{row[ps]:6d}" for ps in STATUS_ORDER))
    lines.append("warning delay (s, + = late): "
                 + ", ".join(f"{k} {v if v is not None else 'n/a'}" for k, v in rep["warning_delay_s"].items()))
    return "\n".join(lines)
