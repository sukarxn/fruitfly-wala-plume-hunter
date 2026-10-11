"""
Synthetic recovery test: plant a fake source, generate noisy readings at
station positions, run the inversion, and measure how far off it lands.

Usage:
    python3 synthetic_test.py                 # 40 random stations in Delhi's core
    python3 synthetic_test.py stations.json   # real layout saved by data.py

Reports two honest numbers:
  1. how often the plume actually reaches at least 2 stations ("detectable")
  2. for detectable cases, the median localisation error in km
Optionally saves synthetic_test.png if matplotlib is installed.
"""

import json
import random
import statistics
import sys

import plume

random.seed(42)
NOISE = 8.0        # ug/m3 sensor noise
BACKGROUND = 80.0  # ug/m3 regional haze


def station_layout():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            st = json.load(f)
        return [{"name": s["name"], "lat": s["lat"], "lon": s["lon"]} for s in st]
    return [{"name": f"S{i}", "lat": random.uniform(28.45, 28.85),
             "lon": random.uniform(76.95, 77.32)} for i in range(40)]


def make_hour(stations, src, Q, wind_ms, wind_from, cls="D"):
    met = {"wind_ms": wind_ms, "wind_from": wind_from, "stability": cls}
    pred = plume.forward(src[0], src[1], Q, stations, met)
    st = [dict(s, pm25=max(BACKGROUND + p + random.gauss(0, NOISE), 0.0))
          for s, p in zip(stations, pred)]
    return {"stations": st, "met": met}, pred


def trial(stations, n_hours):
    src = (random.uniform(28.55, 28.75), random.uniform(77.05, 77.25))
    Q = random.uniform(150, 500)        # g/s: a large landfill / open-burning fire
    wind0 = random.uniform(0, 360)
    hours, hits = [], 0
    for h in range(n_hours):            # wind veers ~30 degrees per hour
        hour, pred = make_hour(stations, src, Q, random.uniform(2, 4), (wind0 + 30 * h) % 360)
        hours.append(hour)
        hits = max(hits, sum(p > 3 * NOISE for p in pred))
    if n_hours >= 24:
        grids = [plume.hourly_grid(h) for h in hours]
        res = plume.invert_hourly_grids(grids, min_hours=24)
    else:
        res = plume.invert(hours)
    err = plume.haversine_km(src[0], src[1], res["best"]["lat"], res["best"]["lon"]) \
        if res["best"] else None
    return {"detectable": hits >= 2, "err": err, "res": res}


def main():
    stations = station_layout()
    print(f"{len(stations)} stations, noise {NOISE} ug/m3\n")
    last = None
    for n_hours, trials in ((1, 40), (3, 40), (168, 3)):
        runs = [trial(stations, n_hours) for _ in range(trials)]
        det = [r for r in runs if r["detectable"]]
        errs = [r["err"] for r in det if r["err"] is not None]
        false_alarm = sum(1 for r in runs if not r["detectable"]
                          and r["res"]["verdict"] == "local" and r["res"]["confidence"] != "low")
        print(f"hours stacked = {n_hours}")
        print(f"  plume reached >=2 stations: {len(det)}/{len(runs)} trials")
        if errs:
            print(f"  median error when detectable: {statistics.median(errs):.1f} km; "
                  f"within 3 km: {sum(e <= 3 for e in errs)}/{len(errs)}")
            last = det[-1]["res"]
        print(f"  confident 'local' calls when undetectable (false alarms): {false_alarm}\n")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        g = last["grid"]
        plt.figure(figsize=(6, 6))
        plt.pcolormesh(g["lons"], g["lats"], g["prob"], shading="auto", cmap="magma_r")
        plt.scatter([s["lon"] for s in stations], [s["lat"] for s in stations],
                    c="tab:blue", s=12, label="stations")
        plt.scatter([last["best"]["lon"]], [last["best"]["lat"]], marker="x",
                    c="tab:green", s=80, label="best estimate")
        plt.legend()
        plt.title("Source probability (a detectable trial)")
        plt.savefig("synthetic_test.png", dpi=120, bbox_inches="tight")
        print("Saved synthetic_test.png")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
