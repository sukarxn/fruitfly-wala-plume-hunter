"""
Plume Hunter - data fetching (standard library only).

- PM2.5 per station per hour from OpenAQ API v3 (needs a free API key).
- Wind, cloud and sunshine per hour from Open-Meteo (no key).

Run locally / in CloudShell to test:
    export OPENAQ_API_KEY=your-key
    python3 data.py            # prints stations + last hours, saves stations.json
"""

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

import plume

OPENAQ = "https://api.openaq.org/v3"
DELHI_CENTRE = (28.63, 77.15)
RADIUS_M = 25000          # OpenAQ's maximum search radius
PM25_PARAMETER_ID = 2     # OpenAQ parameter id for pm25
MAX_STATIONS = 55         # keep within OpenAQ's 60 requests/minute
DEBUG = os.environ.get("DEBUG") == "1"


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _openaq(path, params, key):
    url = f"{OPENAQ}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            return _get(url, {"X-API-Key": key, "Accept": "application/json"})
        except urllib.error.HTTPError as e:
            if e.code == 429:            # rate limited: wait and retry
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("OpenAQ kept rate-limiting; try again in a minute.")


def _utc(obj):
    """Pull a UTC timestamp out of OpenAQ's nested datetime objects."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "utc" in obj:
            return obj["utc"]
        for k in ("datetimeTo", "datetime", "datetimeFrom"):
            if k in obj:
                return _utc(obj[k])
    return None


def _hour_key(ts):
    """'2026-10-11T07:00:00Z' -> '2026-10-11T07' (hour ENDING at this time)."""
    return ts[:13] if ts else None


def pm25_sensors(key):
    """List PM2.5 sensors around Delhi: [{name, lat, lon, sensor_id}]."""
    data = _openaq("/locations", {
        "coordinates": f"{DELHI_CENTRE[0]},{DELHI_CENTRE[1]}",
        "radius": RADIUS_M,
        "parameters_id": PM25_PARAMETER_ID,
        "limit": 1000,
    }, key)
    if DEBUG:
        print(json.dumps(data.get("results", [])[:1], indent=1)[:1500])
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for loc in data.get("results", []):
        coords = loc.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is None or lon is None:
            continue
        last = _utc(loc.get("datetimeLast"))
        if last:  # skip stations silent for more than a day
            try:
                t = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (now - t).total_seconds() > 86400:
                    continue
            except ValueError:
                pass
        for s in loc.get("sensors", []):
            p = s.get("parameter") or {}
            if p.get("id") == PM25_PARAMETER_ID or p.get("name") == "pm25":
                out.append({"name": loc.get("name") or f"loc{loc.get('id')}",
                            "lat": lat, "lon": lon, "sensor_id": s.get("id")})
                break
    # Prefer the stations nearest the centre if there are too many
    out.sort(key=lambda s: plume.haversine_km(s["lat"], s["lon"], *DELHI_CENTRE))
    return out[:MAX_STATIONS]


def pm25_hours(sensors, key, n_hours=3):
    """Hourly PM2.5 per sensor for the last n_hours. Returns {hour_key: {name: value}}."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(hours=n_hours + 3)  # data often lags 1-2 h
    table = {}
    for i, s in enumerate(sensors):
        try:
            data = _openaq(f"/sensors/{s['sensor_id']}/hours", {
                "datetime_from": start.isoformat().replace("+00:00", "Z"),
                "datetime_to": now.isoformat().replace("+00:00", "Z"),
                "limit": 24,
            }, key)
        except Exception as e:  # one bad station shouldn't kill the run
            if DEBUG:
                print("skip", s["name"], e)
            continue
        if DEBUG and i == 0:
            print(json.dumps(data.get("results", [])[:1], indent=1)[:1500])
        for r in data.get("results", []):
            v = r.get("value")
            hk = _hour_key(_utc(r.get("period")) or _utc(r.get("datetime")))
            if v is None or hk is None or v < 0 or v > 2000:
                continue
            table.setdefault(hk, {})[s["name"]] = float(v)
        time.sleep(1.05)  # stay under 60 requests/minute
    return table


def weather_hours():
    """Hourly wind/cloud/sun for Delhi from Open-Meteo. Returns {hour_key: met}."""
    q = urllib.parse.urlencode({
        "latitude": DELHI_CENTRE[0], "longitude": DELHI_CENTRE[1],
        "hourly": "wind_speed_10m,wind_direction_10m,cloud_cover,shortwave_radiation,is_day",
        "wind_speed_unit": "ms", "timezone": "UTC", "past_days": 1, "forecast_days": 1,
    })
    d = _get(f"https://api.open-meteo.com/v1/forecast?{q}")["hourly"]
    out = {}
    for i, t in enumerate(d["time"]):  # '2026-10-11T07:00'
        u = d["wind_speed_10m"][i]
        if u is None:
            continue
        is_day = bool(d["is_day"][i])
        out[t[:13]] = {
            "wind_ms": u,
            "wind_from": d["wind_direction_10m"][i],
            "cloud_pct": d["cloud_cover"][i],
            "solar_wm2": d["shortwave_radiation"][i],
            "is_day": is_day,
            "stability": plume.stability_class(u, is_day, d["shortwave_radiation"][i],
                                               d["cloud_cover"][i]),
        }
    return out


def build_hours(key, n_hours=3):
    """Everything plume.invert() needs, newest hours first."""
    sensors = pm25_sensors(key)
    pm = pm25_hours(sensors, key, n_hours)
    met = weather_hours()
    hours = []
    for hk in sorted(pm.keys(), reverse=True):
        if hk not in met:
            continue
        st = [dict(name=s["name"], lat=s["lat"], lon=s["lon"], pm25=pm[hk].get(s["name"]))
              for s in sensors if s["name"] in pm[hk]]
        if len(st) >= 4:
            hours.append({"hour_utc": hk, "stations": st, "met": met[hk]})
        if len(hours) == n_hours:
            break
    return sensors, hours


if __name__ == "__main__":
    key = os.environ["OPENAQ_API_KEY"]
    sensors, hours = build_hours(key)
    print(f"{len(sensors)} PM2.5 stations found")
    with open("stations.json", "w") as f:
        json.dump(sensors, f, indent=1)
    for h in hours:
        vals = sorted(s["pm25"] for s in h["stations"])
        m = h["met"]
        print(f"{h['hour_utc']}Z: {len(vals)} stations, PM2.5 min {vals[0]:.0f} "
              f"median {vals[len(vals) // 2]:.0f} max {vals[-1]:.0f}; wind from "
              f"{m['wind_from']:.0f} deg at {m['wind_ms']:.1f} m/s, class {m['stability']}")
    with open("hours.json", "w") as f:
        json.dump(hours, f)
    res = plume.invert(hours)
    print("\nVERDICT:", plume.headline(res))
    for t in res.get("top", []):
        print(f"  {t['lat']:.3f}, {t['lon']:.3f}  p={t['prob']:.2f}  near {t['near']['name']} "
              f"({t['near']['km']} km)")
