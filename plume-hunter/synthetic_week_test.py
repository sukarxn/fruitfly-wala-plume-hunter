"""
Synthetic 7-day recovery test for the rolling source map.

Uses the real station layout and real hourly wind stored in history_seed.json
(written by manual_backfill.py), replaces the readings with synthetic ones,
and runs the same combination as the Lambda (hourly_grid + invert_hourly_grids):

  - source trials: a persistent ground-level source of Q g/s is planted
  - null trials:   no source, only regional haze and sensor noise

Usage:
    python3 synthetic_week_test.py [n_sources] [n_null]     # default 6 and 3, ~1.5 min

Reports the localisation error, how peaked the map is, the fit, and whether
smoke corridors would be drawn (they should be for sources, never for nulls).
"""

import json
import random
import statistics
import sys

import data
import plume

random.seed(3)
NOISE = 8.0                       # ug/m3 sensor noise
BBOX = data.CITIES["delhi"]["bbox"]


def load_hours():
    with open("history_seed.json", encoding="utf-8") as f:
        seed = json.load(f)
    return [h for h in seed["hours"] if h.get("grid")]


def synthetic_grids(hours, src, Q):
    grids = []
    for h in hours:
        bg = random.uniform(50, 150)              # regional haze changes hour to hour
        pred = plume.forward(src[0], src[1], Q, h["stations"], h["met"]) if src \
            else [0.0] * len(h["stations"])
        st = [dict(s, pm25=max(bg + p + random.gauss(0, NOISE), 0.0))
              for s, p in zip(h["stations"], pred)]
        grids.append(plume.hourly_grid({"hour_utc": h["hour_utc"], "stations": st,
                                        "met": h["met"]}, bbox=BBOX))
    return grids


def trial(hours, src, Q):
    res = plume.invert_hourly_grids(synthetic_grids(hours, src, Q), bbox=BBOX)
    err = plume.haversine_km(src[0], src[1], res["best"]["lat"], res["best"]["lon"]) \
        if src and res.get("best") else None
    cor = plume.weekly_corridor(res, [h["met"] for h in hours], BBOX)
    return res, err, cor is not None


def main():
    n_src = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    n_null = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    hours = load_hours()
    print(f"{len(hours)} real hours (station layout + wind), noise {NOISE} ug/m3\n")
    errs, drawn = [], 0
    for _ in range(n_src):
        src = (random.uniform(28.50, 28.80), random.uniform(77.00, 77.28))
        Q = random.uniform(100, 400)
        res, err, cor = trial(hours, src, Q)
        errs.append(err)
        drawn += cor
        print(f"  source Q={Q:3.0f} g/s: error {err:4.1f} km, peak {res.get('peak_ratio', 0):8.1f}x, "
              f"fit {res.get('explained', 0):.2f}, corridor {'drawn' if cor else 'hidden'}")
    false_alarms = 0
    for _ in range(n_null):
        res, _, cor = trial(hours, None, 0)
        false_alarms += cor
        print(f"  no source:      peak {res.get('peak_ratio', 0):8.1f}x, fit {res.get('explained', 0):.2f}, "
              f"verdict {res['verdict']}, corridor {'drawn' if cor else 'hidden'}")
    print(f"\nsources: median error {statistics.median(errs):.1f} km; "
          f"within 3 km: {sum(e < 3 for e in errs)}/{n_src}; corridors drawn {drawn}/{n_src}")
    print(f"no source: corridors drawn (false alarms) {false_alarms}/{n_null}")


if __name__ == "__main__":
    main()
