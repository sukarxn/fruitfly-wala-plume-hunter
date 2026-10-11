"""
Fly Hunter: the real fruit-fly connectome tracks the smoke plume to its source.

The brain is the complete male Drosophila central nervous system (MaleCNS v1.0:
166,700 neurons, 25.6 million connections), run as a spiking network with the
`flybrain` package. Its wiring is never changed. What we train is a small linear
readout on top of it (reservoir computing):

  smoke at left/right "antenna"  ->  the fly's real olfactory receptor neurons
  (ORNs, left and right)         ->  the connectome runs for 0.24 s
  central-brain activity         ->  trained readout: "source is to my LEFT/RIGHT?"

The navigator then does what real flies do in a plume: surge upwind while it
smells smoke, steering left/right as the brain decides, and cast crosswind in
widening zig-zags when it loses the scent.

Usage (CloudShell or any machine with Python 3.10+):
    pip install flybrain               # first FlyBrain() downloads ~260 MB of brain data
    python3 fly_hunter.py train        # trains readout.npz, prints held-out accuracy
    python3 fly_hunter.py evaluate     # brain vs. coin-flip vs. no-steering controls
    python3 fly_hunter.py run result.json   # track the source found by the physics
    python3 fly_hunter.py swarm result.json # 12 flies from many start points; majority vote
        [N] [--steer brain|coin|upwind] [--target current|history] [--seed S]
        [--spread downwind|city|smoke] [--winds latest|history]
        (no --seed: a new seed each run, so swarms can be accumulated with fly_density.py)
"""

import json
import math
import sys
import time

import numpy as np
from flybrain import FlyBrain, Readout, Trace

import plume

STEPS_PER_DECISION = 12      # 12 x 20 ms = 0.24 s of brain time per decision
ANTENNA_GAP_M = 60.0         # left/right sensor spacing (drone-scale "antennae")
STEP_M = 60.0                # distance moved per decision
CATCH_M = 200.0              # "found it" radius
MAX_DECISIONS = 160
READOUT_FILE = "readout.npz"


class FlyNose:
    """The connectome as a left/right smell comparator."""

    def __init__(self, seed=64):
        self.brain = FlyBrain(sensory_input=False, seed=seed)
        ct = self.brain.cell_type.astype(str)
        orn_types = sorted({c for c in set(ct) if c.startswith("ORN")})
        self.orn_L = self.brain.cells(orn_types, side="L")
        self.orn_R = self.brain.cells(orn_types, side="R")
        self.watch = self.brain.cells(["cb_intrinsic"])   # central-brain neurons
        self.readout = None
        self.episode = 0

    def respond(self, drive_L, drive_R):
        """Run the brain with these ORN drives (0..0.6); return central-brain activity."""
        self.episode += 1
        self.brain.reset(self.episode)
        tr = Trace(self.brain, idx=self.watch)
        for _ in range(STEPS_PER_DECISION):
            fired = self.brain.step(inject=[(self.orn_L, drive_L), (self.orn_R, drive_R)])
            tr.observe(fired)
        return tr.features()

    def p_left(self, drive_L, drive_R):
        return float(self.readout.predict(self.respond(drive_L, drive_R)))


def encode(c, c50):
    """Concentration (ug/m3 above background) -> ORN drive, saturating like real receptors."""
    return 0.6 * c / (c + c50) if c > 0 else 0.0


# ---------------------------------------------------------------------------
# Training the readout (the connectome itself is fixed)
# ---------------------------------------------------------------------------

def train(n=400, seed=1):
    nose = FlyNose()
    rng = np.random.default_rng(seed)
    X, y = [], []
    t0 = time.time()
    for _ in range(n):
        total = rng.uniform(0.1, 0.6)
        frac = rng.uniform(0.1, 0.9)            # share of smell on the left antenna
        if abs(frac - 0.5) < 0.03:
            continue
        X.append(nose.respond(total * frac, total * (1 - frac)))
        y.append(int(frac > 0.5))
    X, y = np.array(X), np.array(y)
    print(f"simulated {len(X)} smell episodes on the connectome in {time.time() - t0:.0f}s")
    r = Readout.fit(X, y, kind="logistic", components=(5, 20, 60), verbose=True)
    control = Readout.fit(X, rng.permutation(y), kind="logistic", components=(5, 20, 60))
    print(f"held-out AUC: brain readout {r.cv_score:.3f} vs shuffled-label control "
          f"{control.cv_score:.3f} (0.5 = chance)")
    r.save(READOUT_FILE)
    print(f"saved {READOUT_FILE}")
    return r


# ---------------------------------------------------------------------------
# Navigation in a simulated plume
# ---------------------------------------------------------------------------

class PlumeField:
    """Gaussian plume around a source, with on/off puffs (real plumes are patchy)."""

    def __init__(self, Q, met, rng):
        self.Q, self.met, self.rng = Q, met, rng
        # typical concentration 1 km downwind sets the receptor saturation scale
        self.c50 = max(Q * plume.plume_shape(0, -1000, met["wind_ms"], 0.0, met["stability"]), 1.0)

    def conc(self, x, y):
        """x,y metres from the source (east, north)."""
        c = self.Q * plume.plume_shape(x, y, self.met["wind_ms"], self.met["wind_from"],
                                       self.met["stability"])
        p_puff = min(1.0, 0.25 + c / self.c50)          # weaker smell = more gaps
        return c if self.rng.random() < p_puff else 0.0


def navigate(field, start_xy, steer, rng, targets=((0.0, 0.0),), max_decisions=MAX_DECISIONS):
    """steer(cL, cR) -> probability that the source is to the LEFT."""
    up = math.radians(field.met["wind_from"])         # heading pointing upwind
    x, y = start_xy
    heading = up + rng.uniform(-0.6, 0.6)
    path, lost, cast_dir, cast_len = [(x, y)], 0, 1, 2
    for k in range(max_decisions):
        ex, ey = math.sin(heading), math.cos(heading)                 # forward
        lx, ly = -ey, ex                                              # left
        g = ANTENNA_GAP_M / 2
        cL = field.conc(x + lx * g, y + ly * g)
        cR = field.conc(x - lx * g, y - ly * g)
        if cL + cR > 0:                                   # smell: surge upwind, brain steers
            lost, cast_len = 0, 2
            pl = steer(cL, cR)
            heading = up - (pl - 0.5) * 2 * math.radians(60)   # compass: left = minus
        else:                                             # lost it: cast crosswind
            lost += 1
            if lost % cast_len == 0:
                cast_dir, cast_len = -cast_dir, cast_len + 1
            heading = up + cast_dir * math.radians(80)
        x += math.sin(heading) * STEP_M
        y += math.cos(heading) * STEP_M
        path.append((x, y))
        for ti, (tx, ty) in enumerate(targets):
            if math.hypot(x - tx, y - ty) < CATCH_M:
                navigate.reached = ti
                return True, path, k + 1
    navigate.reached = None
    return False, path, max_decisions


def start_point(met, rng, dist=2500):
    """Start downwind of the source, offset sideways."""
    down = math.radians(met["wind_from"] + 180)
    side = rng.uniform(-900, 900)
    return (math.sin(down) * dist + math.cos(down) * side,
            math.cos(down) * dist - math.sin(down) * side)


def evaluate(trials=30, seed=5):
    nose = FlyNose()
    nose.readout = Readout.load(READOUT_FILE)
    rng = np.random.default_rng(seed)
    met = {"wind_ms": 3.0, "wind_from": 0.0, "stability": "D"}
    field = PlumeField(250, met, rng)
    controls = {
        "connectome readout": lambda cL, cR: nose.p_left(encode(cL, field.c50), encode(cR, field.c50)),
        "coin flip": lambda cL, cR: rng.random(),
        "no steering (upwind only)": lambda cL, cR: 0.5,
    }
    out = {}
    for name, steer in controls.items():
        wins, steps = 0, []
        r2 = np.random.default_rng(seed)   # same starts for every controller
        for _ in range(trials):
            field.rng = r2
            ok, _, k = navigate(field, start_point(met, r2), steer, r2)
            wins += ok
            if ok:
                steps.append(k)
        out[name] = (wins, trials, float(np.median(steps)) if steps else None)
        print(f"{name:28s} found the source {wins}/{trials}"
              + (f", median {np.median(steps):.0f} decisions" if steps else ""))
    return out


def run(result_path, out_path="fly_track.json", attempts=5, seed=11):
    """Track the source the physics found (result.json from the Lambda / S3).
    Runs several independent searches; saves the success rate and one path
    (the first successful one, else the last), clearly labelled."""
    with open(result_path) as f:
        res = json.load(f)
    if not res.get("best"):
        raise SystemExit("No local source in this result; nothing to track.")
    src = res["best"]
    met = dict(res["hours"][0]["met"])
    Q = max(src.get("q_gps", 200), 50)
    nose = FlyNose()
    nose.readout = Readout.load(READOUT_FILE)
    wins, shown = 0, None
    for a in range(attempts):
        rng = np.random.default_rng(seed + a)
        field = PlumeField(Q, met, rng)
        ok, path, k = navigate(field, start_point(met, rng),
                               lambda cL, cR: nose.p_left(encode(cL, field.c50),
                                                          encode(cR, field.c50)), rng)
        print(f"  search {a + 1}: {'reached the source' if ok else 'gave up'} after {k} decisions")
        wins += ok
        if ok and (shown is None or not shown[0]):
            shown = (ok, path, k)
        elif shown is None and a == attempts - 1:
            shown = (ok, path, k)
    ok, path, k = shown
    track = [plume.to_latlon(x, y, src["lat"], src["lon"]) for x, y in path]
    out = {"found": ok, "decisions": k, "successes": wins, "attempts": attempts,
           "brain_neurons": int(nose.brain.n),
           "track": [[round(a, 5), round(b, 5)] for a, b in track]}
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"{wins}/{attempts} searches reached the source; saved {out_path}")
    return out


class MixtureField(PlumeField):
    """Smoke from several candidate sources at once, each weighted by how likely
    the physics thinks it is. Coordinates are metres from the best candidate."""

    def __init__(self, sources, met, rng):
        # sources: [(x, y, Q), ...]
        super().__init__(max(q for _, _, q in sources), met, rng)
        self.sources = sources

    def conc(self, x, y):
        c = sum(q * plume.plume_shape(x - sx, y - sy, self.met["wind_ms"],
                                       self.met["wind_from"], self.met["stability"])
                for sx, sy, q in self.sources)
        p_puff = min(1.0, 0.25 + c / self.c50)
        return c if self.rng.random() < p_puff else 0.0


SMOKE_START = 0.05          # "smoke" spread: start only where smoke is >= 5% of the c50 scale
SMOKE_DECISIONS = 400       # ~24 km of travel, so flies far down the plume can still get back


def _recorded_winds(res, path="history_seed.json"):
    """Hourly winds of the last 7 days: from the 7-day result, else the local history seed."""
    winds = (res.get("history") or {}).get("winds")
    if not winds:
        try:
            with open(path) as f:
                winds = [dict(h["met"], hour_utc=h["hour_utc"]) for h in json.load(f)["hours"]
                         if h.get("met")]
        except (OSError, KeyError, ValueError):
            winds = []
    return [w for w in winds if w.get("wind_ms", 0) >= 1.0]


def swarm(result_path, out_path="fly_track.json", n_flies=12, seed=None, steer="brain",
          target="current", spread="downwind", winds="latest"):
    """Launch a swarm of flies from points spread across the downwind side; the
    smoke mixes the physics' top candidates (weighted by probability). The
    destination where most flies land wins the vote.

    seed:   None = new seed every run, so repeated swarms can be accumulated
            (fly_density.py); pass a number for a reproducible demo.
    steer:  "brain" (connectome readout), or the controls "coin" / "upwind".
    target: "current" (3-hour analysis) or "history" (7-day map, local only),
            with the wind of the most recent hour either way.
    spread: "downwind" (a line 2-3.5 km downwind of the best candidate),
            "city" (random points anywhere in Delhi; most never meet the smoke), or
            "smoke" (random points anywhere in Delhi that are inside the modelled smoke).
    winds:  "latest" (most recent hour) or "history" (a random recorded hour of the
            last 7 days per run, so repeated swarms see the smoke in different directions).
    """
    with open(result_path) as f:
        res = json.load(f)
    goal = res.get("history") if target == "history" else res
    if not goal or goal.get("verdict") != "local" or not goal.get("best"):
        raise SystemExit(f"No local source in the {target} result; nothing to track.")
    if not res.get("hours"):
        raise SystemExit("Result has no hourly wind to drive the plume.")
    seed = int(time.time() * 1000) % 2**31 if seed is None else seed
    rng = np.random.default_rng(seed)
    best, met = goal["best"], dict(res["hours"][0]["met"])
    met.setdefault("hour_utc", res["hours"][0].get("hour_utc"))
    if winds == "history":
        recorded = _recorded_winds(res)
        if not recorded:
            raise SystemExit("No recorded 7-day winds found (history result or history_seed.json).")
        met = dict(recorded[int(rng.integers(len(recorded)))])
    res = goal
    cands = res.get("top") or [best]
    ptot = sum(c["prob"] for c in cands) or 1.0
    sources, targets = [], []
    for c in cands:
        x, y = plume.to_xy(c["lat"], c["lon"], best["lat"], best["lon"])
        q = max(c.get("q_gps", 200), 50) * c["prob"] / ptot
        sources.append((x, y, max(q, 1e-3)))
        targets.append((x, y))

    field = MixtureField(sources, met, rng)
    nose = None
    if steer == "brain":
        nose = FlyNose()
        nose.readout = Readout.load(READOUT_FILE)
        steer_fn = lambda cL, cR: nose.p_left(encode(cL, field.c50), encode(cR, field.c50))
    elif steer == "coin":
        steer_fn = lambda cL, cR: rng.random()
    elif steer == "upwind":
        steer_fn = lambda cL, cR: 0.5
    else:
        raise SystemExit(f"Unknown steer mode: {steer}")
    down = math.radians(met["wind_from"] + 180)
    flies, votes = [], [0] * len(cands)
    lat_min, lat_max, lon_min, lon_max = plume.DELHI_BBOX
    steps = SMOKE_DECISIONS if spread == "smoke" else MAX_DECISIONS
    smoky = []
    if spread == "smoke":
        # Rejection sampling: uniform over Delhi, kept only where the (puff-free) smoke is noticeable.
        mean_conc = lambda x, y: sum(q * plume.plume_shape(x - sx, y - sy, met["wind_ms"],
                                                           met["wind_from"], met["stability"])
                                     for sx, sy, q in sources)
        for _ in range(400000):
            xy = plume.to_xy(rng.uniform(lat_min, lat_max), rng.uniform(lon_min, lon_max),
                             best["lat"], best["lon"])
            if mean_conc(*xy) >= SMOKE_START * field.c50:
                smoky.append(xy)
                if len(smoky) == n_flies:
                    break
        if not smoky:
            raise SystemExit("The modelled smoke does not reach any point in Delhi for this wind.")
        n_flies = len(smoky)
    for i in range(n_flies):
        if spread == "smoke":
            start = smoky[i]
        elif spread == "city":
            start = plume.to_xy(rng.uniform(lat_min, lat_max), rng.uniform(lon_min, lon_max),
                                best["lat"], best["lon"])
        else:
            dist = rng.uniform(2000, 3500)
            side = -1800 + 3600 * i / max(n_flies - 1, 1)      # spread across the plume
            side += rng.uniform(-150, 150)                      # jitter: runs differ
            start = (math.sin(down) * dist + math.cos(down) * side,
                     math.cos(down) * dist - math.sin(down) * side)
        ok, path, k = navigate(field, start, steer_fn, rng, targets, steps)
        hit = navigate.reached
        if hit is not None:
            votes[hit] += 1
        print(f"  fly {i + 1:2d}: " + (f"landed on candidate #{hit + 1}" if ok else "gave up")
              + f" after {k} decisions")
        flies.append({"landed": hit, "decisions": k,
                      "track": [[round(a, 5), round(b, 5)] for a, b in
                                (plume.to_latlon(x, y, best["lat"], best["lon"]) for x, y in path)]})
    win = max(range(len(cands)), key=lambda j: votes[j])
    out = {"swarm": True, "steer": steer, "seed": seed, "target": target, "spread": spread,
           "wind": {k: met.get(k) for k in ("hour_utc", "wind_from", "wind_ms", "stability")},
           "max_decisions": steps,
           "demo": bool(res.get("demo")) if target == "current" else False,
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "result_generated_utc": res.get("generated_utc") or res.get("end_utc"),
           "brain_neurons": int(nose.brain.n) if nose else 0, "n_flies": n_flies,
           "landed": sum(votes), "votes": votes,
           "candidates": [{"lat": c["lat"], "lon": c["lon"], "prob": c["prob"]} for c in cands],
           "winner": {"index": win, "lat": cands[win]["lat"], "lon": cands[win]["lon"],
                      "votes": votes[win]},
           "flies": flies,
           # keep single-track fields so older pages still work
           "found": votes[win] > 0, "decisions": next((f["decisions"] for f in flies
                                                       if f["landed"] == win), steps),
           "successes": sum(votes), "attempts": n_flies,
           "track": next((f["track"] for f in flies if f["landed"] == win), flies[0]["track"])}
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"{sum(votes)}/{n_flies} flies landed; most ({votes[win]}) on candidate #{win + 1} "
          f"at {cands[win]['lat']:.3f}, {cands[win]['lon']:.3f}; saved {out_path}")
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "train":
        train()
    elif cmd == "evaluate":
        evaluate()
    elif cmd == "run":
        run(sys.argv[2])
    elif cmd == "swarm":
        args = sys.argv[3:]
        opt = lambda name, default: args[args.index(name) + 1] if name in args else default
        n = int(args[0]) if args and args[0].isdigit() else 12
        swarm(sys.argv[2], n_flies=n, steer=opt("--steer", "brain"),
              target=opt("--target", "current"), spread=opt("--spread", "downwind"),
              winds=opt("--winds", "latest"),
              seed=int(opt("--seed", -1)) if "--seed" in args else None)
    else:
        print(__doc__)
