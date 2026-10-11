"""
Plume Hunter - physics core.

Gaussian plume forward model + grid-search source inversion.
Pure Python standard library only, so it runs in AWS Lambda with no
packages to install.

Conventions
- Positions are lat/lon in degrees. They are converted to local metres
  (east, north) around a reference point; fine for a city-sized area.
- Wind direction is METEOROLOGICAL: the direction the wind blows FROM,
  in degrees clockwise from north. The plume travels towards dir + 180.
- PM2.5 is in micrograms per cubic metre (ug/m3). Source strength Q is
  in grams per second (g/s).
"""

import math

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

EARTH_R = 6371000.0

# Delhi NCT bounding box (lat_min, lat_max, lon_min, lon_max)
DELHI_BBOX = (28.40, 28.90, 76.84, 77.35)

# Known candidate sources used only to LABEL results (approximate positions).
KNOWN_SOURCES = [
    ("Ghazipur landfill", 28.6253, 77.3277),
    ("Bhalswa landfill", 28.7413, 77.1590),
    ("Okhla landfill", 28.5108, 77.2850),
    ("Okhla industrial area", 28.5300, 77.2700),
    ("Wazirpur industrial area", 28.6980, 77.1650),
    ("Mundka industrial area", 28.6820, 77.0300),
    ("Narela-Bawana industrial area", 28.8000, 77.1000),
    ("Anand Vihar bus terminal", 28.6470, 77.3160),
]

# Approximate reference points are used only as map labels, never as evidence.
CITY_SOURCES = {
    "delhi": KNOWN_SOURCES,
    "mumbai": [("Deonar landfill", 19.043, 72.927),
               ("Taloja industrial area", 19.061, 73.116),
               ("Chembur industrial area", 19.051, 72.900)],
    "kolkata": [("Dhapa landfill", 22.540, 88.414),
                ("Dankuni industrial area", 22.680, 88.270),
                ("Garden Reach industrial area", 22.540, 88.300)],
    "hyderabad": [("Jawaharnagar landfill", 17.550, 78.545),
                  ("Jeedimetla industrial area", 17.520, 78.450),
                  ("Patancheru industrial area", 17.530, 78.265)],
}


def to_xy(lat, lon, lat0, lon0):
    """Lat/lon -> metres east/north of (lat0, lon0)."""
    x = math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def to_latlon(x, y, lat0, lon0):
    lat = lat0 + math.degrees(y / EARTH_R)
    lon = lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a)) / 1000.0


# ---------------------------------------------------------------------------
# Atmospheric stability
# ---------------------------------------------------------------------------

def stability_class(wind_ms, is_day, solar_wm2=None, cloud_pct=None):
    """Pasquill-Gifford class A-F from wind speed and sunshine / night cloud.

    Daytime insolation: strong > 600 W/m2, moderate 300-600, slight < 300.
    Night: cloud >= 50% is 'cloudy', otherwise 'clear'.
    """
    u = wind_ms
    if is_day:
        s = solar_wm2 if solar_wm2 is not None else 400.0
        if s > 600:
            table = ["A", "A", "B", "C", "C"]  # u<2, 2-3, 3-5, 5-6, >6
        elif s > 300:
            table = ["A", "B", "B", "C", "D"]
        else:
            table = ["B", "C", "C", "D", "D"]
        idx = 0 if u < 2 else 1 if u < 3 else 2 if u < 5 else 3 if u < 6 else 4
        return table[idx]
    cloud = cloud_pct if cloud_pct is not None else 30.0
    if u < 2:
        return "F"  # calm night; inversion will usually refuse to localise anyway
    if u < 3:
        return "E" if cloud >= 50 else "F"
    if u < 5:
        return "D" if cloud >= 50 else "E"
    return "D"


def briggs_urban_sigmas(x, cls):
    """Briggs (McElroy-Pooler) urban dispersion coefficients, x in metres."""
    if cls in ("A", "B"):
        sy = 0.32 * x / math.sqrt(1 + 0.0004 * x)
        sz = 0.24 * x * math.sqrt(1 + 0.001 * x)
    elif cls == "C":
        sy = 0.22 * x / math.sqrt(1 + 0.0004 * x)
        sz = 0.20 * x
    elif cls == "D":
        sy = 0.16 * x / math.sqrt(1 + 0.0004 * x)
        sz = 0.14 * x / math.sqrt(1 + 0.0003 * x)
    else:  # E, F
        sy = 0.11 * x / math.sqrt(1 + 0.0004 * x)
        sz = 0.08 * x / math.sqrt(1 + 0.0015 * x)
    return sy, sz


# ---------------------------------------------------------------------------
# Forward model
# ---------------------------------------------------------------------------

MIN_DOWNWIND_M = 100.0  # avoid the singularity right at the source

# Hourly-averaged plumes meander: the wind direction wobbles within the hour,
# smearing the plume sideways. Add a direction spread (radians, ~15 deg) to
# the crosswind width, as hourly regulatory models do.
SIGMA_THETA = math.radians(15.0)


def plume_shape(dx, dy, wind_ms, wind_from_deg, cls, H=2.0):
    """Ground-level concentration (ug/m3) per 1 g/s of emission.

    dx, dy: receptor position minus source position, metres (east, north).
    Returns 0 for receptors upwind of the source.
    """
    towards = math.radians(wind_from_deg + 180.0)  # direction plume travels
    ux, uy = math.sin(towards), math.cos(towards)  # unit vector (east, north)
    x = dx * ux + dy * uy                          # downwind distance
    y = -dx * uy + dy * ux                         # crosswind distance
    if x <= 0:
        return 0.0
    x = max(x, MIN_DOWNWIND_M)
    u = max(wind_ms, 0.5)
    sy, sz = briggs_urban_sigmas(x, cls)
    sy = math.sqrt(sy * sy + (x * SIGMA_THETA) ** 2)
    # Ground-level receptor (z=0) with ground reflection:
    c = 1.0 / (math.pi * u * sy * sz)
    c *= math.exp(-(y * y) / (2 * sy * sy)) * math.exp(-(H * H) / (2 * sz * sz))
    return c * 1e6  # g/m3 -> ug/m3


def forward(source_lat, source_lon, Q, stations, met, H=2.0):
    """Predicted excess PM2.5 at each station from one source of Q g/s."""
    lat0, lon0 = source_lat, source_lon
    cls = met["stability"]
    out = []
    for s in stations:
        dx, dy = to_xy(s["lat"], s["lon"], lat0, lon0)
        out.append(Q * plume_shape(dx, dy, met["wind_ms"], met["wind_from"], cls, H))
    return out


# ---------------------------------------------------------------------------
# Inversion
# ---------------------------------------------------------------------------

def percentile(values, p):
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def make_grid(bbox=DELHI_BBOX, step_km=1.0):
    lat_min, lat_max, lon_min, lon_max = bbox
    lat0 = (lat_min + lat_max) / 2
    dlat = step_km / 111.0
    dlon = step_km / (111.0 * math.cos(math.radians(lat0)))
    lats, lons = [], []
    lat = lat_min
    while lat <= lat_max + 1e-9:
        lats.append(round(lat, 5))
        lat += dlat
    lon = lon_min
    while lon <= lon_max + 1e-9:
        lons.append(round(lon, 5))
        lon += dlon
    return lats, lons


def invert(hours, bbox=DELHI_BBOX, step_km=1.0, noise_ug=10.0,
           background_pct=20, min_wind_ms=1.0, H=2.0, sources=None):
    """Find the most likely single ground-level source.

    hours: list of dicts, one per hour, each:
        {"stations": [{"name", "lat", "lon", "pm25"}, ...],
         "met": {"wind_ms", "wind_from", "stability"}}
    Misfits are added across hours (triangulation by changing wind).
    """
    usable = []
    for h in hours:
        st = [s for s in h["stations"] if s.get("pm25") is not None]
        if len(st) < 4:
            continue
        if h["met"]["wind_ms"] < min_wind_ms:
            continue
        bg = percentile([s["pm25"] for s in st], background_pct)
        excess = [max(s["pm25"] - bg, 0.0) for s in st]
        usable.append({"stations": st, "met": h["met"], "bg": bg, "excess": excess})

    base = {"grid": None, "verdict": "cannot_localise", "best": None,
            "top": [], "hours_used": len(usable)}
    if not usable:
        base["reason"] = "Wind too calm (or too few stations) to trace a plume."
        return base

    # Need at least 2 stations clearly above background in some hour,
    # otherwise there is nothing to triangulate (one spike fits anywhere upwind).
    hits = max(sum(e > 3 * noise_ug for e in h["excess"]) for h in usable)
    if hits < 2:
        base["verdict"] = "regional"
        base["reason"] = ("No plume seen at 2 or more stations: either regional haze "
                          "or a source between stations.")
        base["background_ug"] = round(sum(h["bg"] for h in usable) / len(usable), 1)
        return base

    lats, lons = make_grid(bbox, step_km)
    null_misfit = sum(sum(e * e for e in h["excess"]) for h in usable)
    if null_misfit < 1e-6:
        base["verdict"] = "regional"
        base["reason"] = "All stations read close to background: no local hotspot."
        return base

    misfit = [[0.0] * len(lons) for _ in lats]
    qbest = [[0.0] * len(lons) for _ in lats]
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            total, qs = 0.0, []
            for h in usable:
                met = h["met"]
                g = []
                for s in h["stations"]:
                    dx, dy = to_xy(s["lat"], s["lon"], la, lo)
                    g.append(plume_shape(dx, dy, met["wind_ms"], met["wind_from"],
                                         met["stability"], H))
                gg = sum(v * v for v in g)
                ge = sum(a * b for a, b in zip(g, h["excess"]))
                q = max(ge / gg, 0.0) if gg > 0 else 0.0
                total += sum((e - q * v) ** 2 for e, v in zip(h["excess"], g))
                qs.append(q)
            misfit[i][j] = total
            qbest[i][j] = sum(qs) / len(qs)

    # Probability map. Scale noise with number of data points so the map is
    # informative rather than a single spike.
    m_min = min(min(r) for r in misfit)
    s2 = 2 * noise_ug * noise_ug
    prob = [[math.exp(-(m - m_min) / s2) for m in r] for r in misfit]
    tot = sum(sum(r) for r in prob)
    prob = [[p / tot for p in r] for r in prob]

    # Best cell and top-3 separated peaks
    cells = sorted(((prob[i][j], i, j) for i in range(len(lats))
                    for j in range(len(lons))), reverse=True)
    top = []
    for p, i, j in cells:
        la, lo = lats[i], lons[j]
        if all(haversine_km(la, lo, t["lat"], t["lon"]) > 3.0 for t in top):
            top.append({"lat": la, "lon": lo, "prob": p, "q_gps": qbest[i][j],
                        "near": nearest_known(la, lo, sources)})
        if len(top) == 3:
            break

    best = top[0]
    explained = 1.0 - misfit[lats.index(best["lat"])][lons.index(best["lon"])] / null_misfit
    # Spread: radius (km) around best cell containing 50% of probability
    dists = sorted((haversine_km(best["lat"], best["lon"], lats[i], lons[j]), prob[i][j])
                   for i in range(len(lats)) for j in range(len(lons)))
    acc, r50 = 0.0, 0.0
    for d, p in dists:
        acc += p
        r50 = d
        if acc >= 0.5:
            break

    if explained < 0.3:
        verdict, conf = "regional", "low"
    elif explained > 0.7 and r50 < 3:
        verdict, conf = "local", "high"
    elif explained > 0.5 and r50 < 6:
        verdict, conf = "local", "medium"
    else:
        verdict, conf = "local", "low"

    return {
        "verdict": verdict,
        "confidence": conf,
        "explained": round(explained, 3),
        "radius50_km": round(r50, 2),
        "best": best,
        "top": top,
        "hours_used": len(usable),
        "background_ug": round(sum(h["bg"] for h in usable) / len(usable), 1),
        "grid": {"lats": lats, "lons": lons,
                 "prob": [[round(p, 6) for p in r] for r in prob]},
    }


def hourly_grid(hour, bbox=DELHI_BBOX, step_km=1.0, noise_ug=10.0,
                background_pct=20, min_wind_ms=1.0, H=2.0):
    """Cacheable raw error contribution for one hour (no probability normalization)."""
    stations = [s for s in hour["stations"] if s.get("pm25") is not None]
    met = hour["met"]
    if len(stations) < 4 or met["wind_ms"] < min_wind_ms:
        return None
    bg = percentile([s["pm25"] for s in stations], background_pct)
    excess = [max(s["pm25"] - bg, 0.0) for s in stations]
    lats, lons = make_grid(bbox, step_km)
    errors = []
    for la in lats:
        row = []
        for lo in lons:
            g = [plume_shape(*to_xy(s["lat"], s["lon"], la, lo), met["wind_ms"],
                             met["wind_from"], met["stability"], H) for s in stations]
            gg = sum(v * v for v in g)
            q = max(sum(a * b for a, b in zip(g, excess)) / gg, 0.0) if gg else 0.0
            row.append(sum((e - q * v) ** 2 for e, v in zip(excess, g)))
        errors.append(row)
    return {"hour_utc": hour.get("hour_utc"), "lats": lats, "lons": lons,
            "errors": errors, "background_ug": bg, "station_count": len(stations),
            "null_misfit": sum(e * e for e in excess),
            "hits": sum(e > 3 * noise_ug for e in excess)}


def invert_hourly_grids(grids, bbox=DELHI_BBOX, noise_ug=10.0, sources=None,
                        min_hours=24):
    """Combine cached per-hour error grids into a rolling multi-day result."""
    usable = [g for g in grids if g]
    base = {"grid": None, "verdict": "cannot_localise", "best": None,
            "top": [], "hours_used": len(usable), "stations_used": sum(g["station_count"] for g in usable)}
    if len(usable) < min_hours:
        base["reason"] = f"Only {len(usable)} usable hours; at least {min_hours} are needed for the 7-day map."
        return base
    if max(g["hits"] for g in usable) < 2:
        base.update(verdict="regional", reason="No hour showed a plume at 2 or more stations.")
        return base
    lats, lons = usable[0]["lats"], usable[0]["lons"]
    misfit = [[sum(g["errors"][i][j] for g in usable) for j in range(len(lons))]
              for i in range(len(lats))]
    m_min = min(map(min, misfit))
    # Independent hourly sensor noise accumulates; scale likelihood to total observations.
    scale = 2 * noise_ug * noise_ug * sum(g["station_count"] for g in usable)
    prob = [[math.exp(-(v - m_min) / scale) for v in row] for row in misfit]
    total = sum(map(sum, prob))
    prob = [[v / total for v in row] for row in prob]
    cells = sorted(((prob[i][j], i, j) for i in range(len(lats)) for j in range(len(lons))), reverse=True)
    top = []
    for p, i, j in cells:
        la, lo = lats[i], lons[j]
        if all(haversine_km(la, lo, t["lat"], t["lon"]) > 3 for t in top):
            top.append({"lat": la, "lon": lo, "prob": p, "q_gps": 0,
                        "near": nearest_known(la, lo, sources)})
        if len(top) == 3:
            break
    null = sum(g["null_misfit"] for g in usable)
    explained = 0 if null <= 0 else max(0, min(1, 1 - misfit[cells[0][1]][cells[0][2]] / null))
    dists = sorted((haversine_km(top[0]["lat"], top[0]["lon"], lats[i], lons[j]), prob[i][j])
                   for i in range(len(lats)) for j in range(len(lons)))
    acc, r50 = 0.0, 0.0
    for distance, probability in dists:
        acc += probability
        r50 = distance
        if acc >= 0.5:
            break
    if explained < 0.3:
        verdict, confidence = "regional", "low"
    elif explained > 0.7 and r50 < 3:
        verdict, confidence = "local", "high"
    elif explained > 0.5 and r50 < 6:
        verdict, confidence = "local", "medium"
    else:
        verdict, confidence = "local", "low"
    flat = sorted(v for row in prob for v in row)
    base.update(verdict=verdict, confidence=confidence, explained=round(explained, 3),
                radius50_km=round(r50, 2), best=top[0], top=top,
                # best cell vs the median cell; ~1 means a flat map (no source signal)
                peak_ratio=round(cells[0][0] / flat[len(flat) // 2], 2),
                background_ug=round(sum(g["background_ug"] for g in usable) / len(usable), 1),
                grid={"lats": lats, "lons": lons, "prob": [[round(v, 6) for v in row] for row in prob]})
    return base


def corridor(sources, mets, bbox=DELHI_BBOX, step_km=1.0, min_wind_ms=1.0, H=2.0):
    """Time-averaged ground-level smoke footprint of the candidate sources.

    C(x) = (1/N) * sum over hours h, sources k of  p_k * plume_shape(x - s_k, met_h)

    sources: [{"lat", "lon", "prob"}, ...] (e.g. the 7-day top-3). Emission rates
    are not known from the 7-day fit, so each source is weighted by its
    probability only: the result is a relative footprint, not ug/m3.
    mets: hourly met dicts across the window; calm hours are skipped because
    their plume direction is undefined (same rule as the inversion).
    Returns {"lats", "lons", "value"} with values scaled so the maximum is 1.
    """
    mets = [m for m in mets if m and m.get("wind_ms", 0) >= min_wind_ms]
    srcs = [s for s in sources if s.get("prob", 0) > 0]
    if not mets or not srcs:
        return None
    ptot = sum(s["prob"] for s in srcs)
    lats, lons = make_grid(bbox, step_km)
    value = []
    for la in lats:
        row = []
        for lo in lons:
            offsets = [(s["prob"] / ptot, to_xy(la, lo, s["lat"], s["lon"])) for s in srcs]
            row.append(sum(w * plume_shape(dx, dy, m["wind_ms"], m["wind_from"],
                                           m["stability"], H)
                           for m in mets for w, (dx, dy) in offsets))
        value.append(row)
    mx = max(map(max, value))
    if mx <= 0:
        return None
    return {"lats": lats, "lons": lons, "hours": len(mets),
            "value": [[round(v / mx, 4) for v in row] for row in value]}


def weekly_corridor(result, mets, bbox=DELHI_BBOX):
    """Corridor for a 7-day result, only when that fit found a local source.

    In the synthetic 7-day test no-source runs explain ~11% and planted sources
    78-97%, so the same 30% 'local' threshold decides whether candidates are
    trustworthy enough to draw where their smoke went.
    """
    if result.get("verdict") != "local" or not result.get("top"):
        return None
    return corridor(result["top"], mets, bbox)


def nearest_known(lat, lon, sources=None):
    name, d = None, 1e9
    for n, la, lo in (sources if sources is not None else KNOWN_SOURCES):
        dd = haversine_km(lat, lon, la, lo)
        if dd < d:
            name, d = n, dd
    return {"name": name, "km": round(d, 1)}


def headline(result):
    """One-line plain-English verdict."""
    v = result["verdict"]
    if v == "cannot_localise":
        return "Wind too calm to tell where the pollution is coming from."
    if v == "regional" or result.get("best") is None:
        return "Regional haze: no clear local source found."
    b = result["best"]
    where = (f"near {b['near']['name']}" if b["near"]["km"] <= 3
             else f"about {b['near']['km']} km from {b['near']['name']}")
    return (f"Local source likely {where} "
            f"({result['confidence']} confidence, {b['lat']:.3f}, {b['lon']:.3f}).")
