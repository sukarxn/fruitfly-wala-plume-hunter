"""
Accumulate fly-swarm paths across runs into a path-density map that sharpens
as more swarms run (standard library only).

For every fly path f in a run, weight  w_f = s_f   (1 if it landed, FAILED_WEIGHT if not).
Each 250 m cell is counted at most once per path (o_f(c) in {0, 1}), so flies
that cast back and forth in one place do not dominate. A path also covers the
cells next to it (TOLERANCE_CELLS), so nearby but non-overlapping paths add up.

State kept between runs (decayed with half-life T, age dt since last update):
    lam = 2 ** (-dt / T)
    D(c) <- lam   * D(c) + sum_f w_f * o_f(c)      weighted path count per cell
    W    <- lam   * W    + sum_f w_f               total weight
    S2   <- lam^2 * S2   + sum_f w_f^2             for the effective sample size
Read-out:
    P(c)  = D(c) / W                               share of (weighted) flies through c
    n_eff = W^2 / S2
    SE(c) = sqrt(P (1 - P) / n_eff)
    a cell is shown only if  P - 1.96 SE > TAU     (one-off paths fade, corridors firm up)
Every path is also retained (with its run time) and drawn faintly, its weight
decaying with the same half-life, so overlaps darken where flies repeat.
Most-followed route per candidate: Dijkstra over the grid with cost -log(P + eps),
from the candidate's cell to the far end of the confidently-used area, i.e. the
darkest corridor leading away from the source.

The smoke the flies follow is simulated from the physics' own estimate, so this
map shows how reliably the searchers trace the modelled plume; it is not
independent evidence of where real smoke went.

Usage:
    python3 fly_density.py update fly_track.json   # adds a swarm; writes fly_density[_demo].json
    python3 fly_density.py show fly_density.json   # prints runs, n_eff, cells shown, routes
"""

import datetime as dt
import heapq
import json
import math
import os
import sys

import plume

CELL_M = 250.0
HALF_LIFE_H = 84.0       # 3.5 days, matching the 7-day source map
FAILED_WEIGHT = 0.2      # flies that never found the plume count a little
TAU = 0.10               # minimum share of flies (lower 95% bound) for a cell to be shown
TOLERANCE_CELLS = 1      # a path also counts in neighbouring cells (~ +/-250 m), so paths
                         # that run close together reinforce each other without overlapping
MIN_ROUTE_P = 0.05       # routes through cells almost no fly used are not drawn
MIN_ROUTE_PROB = 0.05    # candidates the physics gives less probability than this get no route
MIN_ROUTE_CELLS = 4      # shorter routes (< ~1 km) say nothing and are not drawn
MIN_PATH_W = 0.02        # retained paths are dropped once their decayed weight is below this
EPS = 1e-3               # keeps -log(P + EPS) finite for unvisited cells
BBOX = plume.DELHI_BBOX


def _geometry(bbox=BBOX, cell_m=CELL_M):
    lat_min, lat_max, lon_min, lon_max = bbox
    lat0 = (lat_min + lat_max) / 2
    dlat = cell_m / 111000.0
    dlon = cell_m / (111000.0 * math.cos(math.radians(lat0)))
    rows = int((lat_max - lat_min) / dlat) + 1
    cols = int((lon_max - lon_min) / dlon) + 1
    return {"lat_min": lat_min, "lon_min": lon_min, "dlat": dlat, "dlon": dlon,
            "rows": rows, "cols": cols}


def _cell(g, lat, lon):
    i = int((lat - g["lat_min"]) / g["dlat"])
    j = int((lon - g["lon_min"]) / g["dlon"])
    return (i, j) if 0 <= i < g["rows"] and 0 <= j < g["cols"] else None


def _centre(g, i, j):
    return (g["lat_min"] + (i + 0.5) * g["dlat"], g["lon_min"] + (j + 0.5) * g["dlon"])


def path_cells(g, track):
    """Cells a lat/lon path passes through (each once), sampled every half cell."""
    cells = set()
    for (la1, lo1), (la2, lo2) in zip(track, track[1:] or track):
        span = max(abs(la2 - la1) / g["dlat"], abs(lo2 - lo1) / g["dlon"])
        n = max(1, int(math.ceil(span * 2)))
        for k in range(n + 1):
            c = _cell(g, la1 + (la2 - la1) * k / n, lo1 + (lo2 - lo1) * k / n)
            if c:
                cells.add(c)
    t = TOLERANCE_CELLS
    return {(i + di, j + dj) for i, j in cells
            for di in range(-t, t + 1) for dj in range(-t, t + 1)
            if 0 <= i + di < g["rows"] and 0 <= j + dj < g["cols"]}


def new_state():
    return {"version": 1, "cell_m": CELL_M, "half_life_h": HALF_LIFE_H, "bbox": list(BBOX),
            "runs": 0, "W": 0.0, "S2": 0.0, "D": {}, "starts": {}, "updated_utc": None,
            "candidates": [], "steer": None, "demo": None, "paths": []}


def update(state, swarm_out, when=None):
    """Fold one swarm (fly_hunter.swarm output) into the state, in place."""
    if not swarm_out.get("swarm") or not swarm_out.get("flies"):
        raise ValueError("Not a swarm result (run: fly_hunter.py swarm ...)")
    if state["steer"] not in (None, swarm_out.get("steer", "brain")):
        raise ValueError("State holds a different steering mode; use a separate state file")
    when = when or dt.datetime.now(dt.timezone.utc)
    if state["updated_utc"]:
        age_h = (when - dt.datetime.fromisoformat(state["updated_utc"])).total_seconds() / 3600
        lam = 2 ** (-max(age_h, 0.0) / state["half_life_h"])
    else:
        lam = 1.0
    state["D"] = {k: v * lam for k, v in state["D"].items() if v * lam > 1e-4}
    state["starts"] = {k: v * lam for k, v in state["starts"].items() if v * lam > 1e-4}
    state["W"] *= lam
    state["S2"] *= lam * lam
    g = _geometry(tuple(state["bbox"]), state["cell_m"])
    for f in swarm_out["flies"]:
        w = 1.0 if f.get("landed") is not None else FAILED_WEIGHT
        state.setdefault("paths", []).append(
            {"t": when.isoformat(), "w": w, "line": [[round(a, 4), round(b, 4)] for a, b in f["track"]]})
        for i, j in path_cells(g, f["track"]):
            key = f"{i},{j}"
            state["D"][key] = state["D"].get(key, 0.0) + w
        start = _cell(g, *f["track"][0])
        if start:
            key = f"{start[0]},{start[1]}"
            state["starts"][key] = state["starts"].get(key, 0.0) + w
        state["W"] += w
        state["S2"] += w * w
    state["runs"] += 1
    state["updated_utc"] = when.isoformat()
    half = state["half_life_h"]
    state["paths"] = [p for p in state.get("paths", [])
                      if p["w"] * 2 ** (-_age_h(when, p["t"]) / half) >= MIN_PATH_W]
    state["candidates"] = swarm_out.get("candidates", [])
    if swarm_out.get("wind"):
        state.setdefault("run_winds", []).append(swarm_out["wind"])
    state["steer"] = swarm_out.get("steer", "brain")
    state["demo"] = bool(swarm_out.get("demo"))
    state["summary"] = summarise(state)
    return state


def _age_h(now, iso):
    return max((now - dt.datetime.fromisoformat(iso)).total_seconds() / 3600, 0.0)


def _route(g, P, src, goal_cells):
    """Cheapest path (cost -log(P + EPS) per step, scaled by step length) to any goal cell."""
    rows, cols = g["rows"], g["cols"]
    dist = {src: 0.0}
    prev = {}
    heap = [(0.0, src)]
    while heap:
        d, (i, j) = heapq.heappop(heap)
        if (i, j) in goal_cells:
            path = [(i, j)]
            while path[-1] in prev:
                path.append(prev[path[-1]])
            return path[::-1], d
        if d > dist.get((i, j), math.inf):
            continue
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if not (di or dj):
                    continue
                n = (i + di, j + dj)
                if not (0 <= n[0] < rows and 0 <= n[1] < cols):
                    continue
                step = math.hypot(di, dj) * -math.log(P.get(n, 0.0) + EPS)
                nd = d + step
                if nd < dist.get(n, math.inf):
                    dist[n] = nd
                    prev[n] = (i, j)
                    heapq.heappush(heap, (nd, n))
    return None, math.inf


def summarise(state):
    """What the page draws: confident cells and one most-followed route per candidate."""
    g = _geometry(tuple(state["bbox"]), state["cell_m"])
    W, S2 = state["W"], state["S2"]
    if W <= 0:
        return None
    n_eff = W * W / S2 if S2 > 0 else 0.0
    P = {}
    cells = []
    confident = set()
    for key, d in state["D"].items():
        i, j = map(int, key.split(","))
        p = min(d / W, 1.0)
        P[(i, j)] = p
        se = math.sqrt(p * (1 - p) / n_eff) if n_eff > 0 else 1.0
        if p - 1.96 * se > TAU:
            confident.add((i, j))
            la, lo = _centre(g, i, j)
            cells.append([round(la, 5), round(lo, 5), round(p, 3)])
    # Route from each likely candidate to the far end of the confidently-used area.
    routes = []
    for k, c in enumerate(state["candidates"]):
        src = _cell(g, c["lat"], c["lon"])
        if not src or c.get("prob", 0) < MIN_ROUTE_PROB or not confident:
            continue
        far = max(confident, key=lambda ij: math.hypot(ij[0] - src[0], ij[1] - src[1]))
        if math.hypot(far[0] - src[0], far[1] - src[1]) < MIN_ROUTE_CELLS:
            continue
        path, cost = _route(g, P, src, {far})
        if not path or len(path) < MIN_ROUTE_CELLS:
            continue
        ps = [P.get(c_, 0.0) for c_ in path]
        if sorted(ps)[len(ps) // 2] < MIN_ROUTE_P:
            continue
        routes.append({"candidate": k,
                       "median_p": round(sorted(ps)[len(ps) // 2], 3),
                       "line": [[round(a, 5), round(b, 5)] for a, b in
                                (_centre(g, i, j) for i, j in path)]})
    now = dt.datetime.fromisoformat(state["updated_utc"])
    paths = [{"w": round(p["w"] * 2 ** (-_age_h(now, p["t"]) / state["half_life_h"]), 3),
              "line": p["line"]} for p in state.get("paths", [])]
    return {"runs": state["runs"], "n_eff": round(n_eff, 1), "W": round(W, 2),
            "p_max": round(max(P.values()), 3) if P else 0, "paths": paths,
            "run_winds": state.get("run_winds", [])[-50:],
            "tau": TAU, "half_life_h": state["half_life_h"], "cell_m": state["cell_m"],
            "updated_utc": state["updated_utc"], "steer": state["steer"],
            "demo": state["demo"], "candidates": state["candidates"],
            "cells": cells, "routes": routes}


def state_path_for(swarm_out):
    name = "fly_density"
    if swarm_out.get("steer", "brain") != "brain":
        name += "_" + swarm_out["steer"]
    if swarm_out.get("demo"):
        name += "_demo"
    return name + ".json"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "update" and len(sys.argv) > 2:
        with open(sys.argv[2]) as f:
            swarm_out = json.load(f)
        path = sys.argv[3] if len(sys.argv) > 3 else state_path_for(swarm_out)
        state = new_state()
        if os.path.exists(path):
            with open(path) as f:
                state = json.load(f)
        update(state, swarm_out)
        with open(path, "w") as f:
            json.dump(state, f)
        s = state["summary"]
        print(f"{path}: {s['runs']} runs, n_eff {s['n_eff']}, {len(s['cells'])} cells shown, "
              f"{len(s['routes'])} routes")
    elif cmd == "show" and len(sys.argv) > 2:
        with open(sys.argv[2]) as f:
            s = json.load(f)["summary"]
        print(f"runs {s['runs']}, n_eff {s['n_eff']}, cells shown {len(s['cells'])} (tau {s['tau']})")
        for r in s["routes"]:
            print(f"  route from candidate #{r['candidate'] + 1}: {len(r['line'])} cells, "
                  f"median P {r['median_p']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
