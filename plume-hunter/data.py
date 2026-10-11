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
RADIUS_M = 30000          # city grouping radius
PM25_PARAMETER_ID = 2     # OpenAQ parameter id for pm25
DEBUG = os.environ.get("DEBUG") == "1"

# City bounds are deliberately city-scale: the inversion is only meaningful
# for sources within roughly 1–15 km of the reporting stations.
CITIES = {
    "delhi": {"name": "Delhi", "centre": (28.63, 77.15),
              "bbox": (28.40, 28.90, 76.84, 77.35)},
}
# Real Delhi monitoring locations used as the map geometry for generated demo
# readings. These labels describe places, not live stations or live data.
DEMO_PLACES = [
    ("Narela", 28.823, 77.102), ("DTU", 28.749, 77.120),
    ("Jahangirpuri", 28.733, 77.171), ("Rohini", 28.733, 77.120),
    ("Burari Crossing", 28.726, 77.201), ("Sonia Vihar", 28.711, 77.249),
    ("Wazirpur", 28.700, 77.165), ("Ashok Vihar", 28.695, 77.182),
    ("IHBAS Dilshad Garden", 28.681, 77.303), ("Punjabi Bagh", 28.674, 77.131),
    ("Vivek Vihar", 28.672, 77.315), ("North Campus DU", 28.657, 77.159),
    ("Shadipur", 28.651, 77.147), ("Anand Vihar", 28.647, 77.316),
    ("Pusa", 28.638, 77.169), ("Mandir Marg", 28.636, 77.201),
    ("ITO", 28.629, 77.241), ("Ghazipur", 28.6253, 77.3277),
    ("Okhla Phase II", 28.530, 77.270), ("CRRI Mathura Road", 28.551, 77.273),
    ("Dwarka Sector 8", 28.571, 77.072), ("NSIT Dwarka", 28.610, 77.038),
    ("Aya Nagar", 28.470, 77.109), ("R K Puram", 28.563, 77.186),
    ("Lodhi Road", 28.592, 77.227), ("Najafgarh", 28.570, 76.933),
    ("Mundka", 28.682, 77.030), ("Bawana", 28.800, 77.100),
    ("Chandni Chowk", 28.656, 77.230), ("Karni Singh Shooting Range", 28.498, 77.301),
    ("JLN Stadium", 28.580, 77.234), ("Ramesh Nagar", 28.650, 77.134),
    ("Sri Aurobindo Marg", 28.531, 77.190), ("Siri Fort", 28.550, 77.219),
    ("Pitam Pura", 28.703, 77.133),
]
CPCB_RESOURCE = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
DATA_GOV = "https://api.data.gov.in/resource"


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


def india_locations(key):
    """Page through all OpenAQ location records for India."""
    # OpenAQ country results are paginated; never assume one page covers India.
    results, page = [], 1
    while True:
        data = _openaq("/locations", {
            "iso": "IN", "parameters_id": PM25_PARAMETER_ID,
            "limit": 1000, "page": page,
        }, key)
        batch = data.get("results", [])
        results.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
    if DEBUG:
        print(json.dumps(results[:1], indent=1)[:1500])
    return results


def pm25_sensors(key, city="delhi", locations=None):
    """List PM2.5 sensors assigned to a configured city within 30 km."""
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}")
    locations = locations if locations is not None else india_locations(key)
    centre = CITIES[city]["centre"]
    out = []
    for loc in locations:
        coords = loc.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is None or lon is None:
            continue
        if plume.haversine_km(lat, lon, *centre) > RADIUS_M / 1000:
            continue
        for s in loc.get("sensors", []):
            p = s.get("parameter") or {}
            if p.get("id") == PM25_PARAMETER_ID or p.get("name") == "pm25":
                out.append({"name": loc.get("name") or f"loc{loc.get('id')}",
                            "lat": lat, "lon": lon, "sensor_id": s.get("id"),
                            "source": "OpenAQ", "last_seen": _utc(loc.get("datetimeLast")),
                            # Operating network (e.g. CPCB, AirGradient), so stale CPCB
                            # stations can be shown as such on the map.
                            "network": (loc.get("provider") or {}).get("name")})
                break
    out.sort(key=lambda s: plume.haversine_km(s["lat"], s["lon"], *centre))
    return out


def cpcb_pm25(city="Delhi", key=None):
    """Fetch CPCB PM2.5 observations. The published resource is hourly; values
    are pollutant_avg for each station's reported interval, timestamped by last_update.
    """
    key = key or os.environ.get("DATA_GOV_IN_API_KEY")
    if not key:
        return []
    params = {"api-key": key, "format": "json", "limit": 1000,
              "filters[city]": city, "filters[pollutant_id]": "PM2.5"}
    url = f"{DATA_GOV}/{CPCB_RESOURCE}?{urllib.parse.urlencode(params)}"
    response = _get(url)
    observations = []
    for row in response.get("records", []):
        try:
            value = float(row.get("pollutant_avg"))
            lat, lon = float(row["latitude"]), float(row["longitude"])
            ts = dt.datetime.strptime(row["last_update"], "%d-%m-%Y %H:%M:%S")
            ts = ts.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= value <= 2000 and plume.haversine_km(lat, lon, *CITIES["delhi"]["centre"]) <= RADIUS_M / 1000:
            observations.append({"name": row.get("station", "CPCB station"),
                                 "lat": lat, "lon": lon, "pm25": value,
                                 "timestamp": ts.astimezone(dt.timezone.utc).isoformat(),
                                 "source": "CPCB"})
    return observations


def _age_hours(ts):
    """Hours since an ISO timestamp, or None if missing/unparseable."""
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def pm25_hours(sensors, key, n_hours=3):
    """Hourly PM2.5 per sensor, retaining the measurement time."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(hours=n_hours + 3)  # data often lags 1-2 h
    table = {}
    for i, s in enumerate(sensors):
        if not s.get("sensor_id"):
            continue
        age = _age_hours(s.get("last_seen"))
        if age is not None and age > 24:  # silent for a day: no recent hours to fetch
            continue
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
            table.setdefault(hk, {})[s["name"]] = {
                "value": float(v), "timestamp": _utc(r.get("period") or r.get("datetime")),
                "source": s.get("source", "OpenAQ")}
        time.sleep(1.05)  # stay under 60 requests/minute
    return table


def last_known_pm25(sensor, key, days=10):
    """Newest hourly PM2.5 for a sensor within the last `days`: {value, timestamp} or None."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    try:
        data = _openaq(f"/sensors/{sensor['sensor_id']}/hours", {
            "datetime_from": (now - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z"),
            "datetime_to": now.isoformat().replace("+00:00", "Z"),
            "limit": days * 24 + 24,
        }, key)
    except Exception as e:
        if DEBUG:
            print("skip last-known", sensor["name"], e)
        return None
    best = None
    for r in data.get("results", []):
        v, ts = r.get("value"), _utc(r.get("period") or r.get("datetime"))
        if v is None or ts is None or v < 0 or v > 2000:
            continue
        if best is None or ts > best["timestamp"]:
            best = {"value": float(v), "timestamp": ts}
    return best


def weather_hours(city="delhi"):
    """Hourly wind/cloud/sun at a city's centre. Returns {hour_key: met}."""
    centre = CITIES[city]["centre"]
    q = urllib.parse.urlencode({
        "latitude": centre[0], "longitude": centre[1],
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


def build_hours(key, n_hours=3, city="delhi", locations=None, cpcb_readings=None,
                use_last_known=True):
    """Everything plume.invert() needs, newest hours first.

    use_last_known: CPCB stations with no recent reading contribute their last
    known value (up to 10 days old) to every hour. Those entries carry
    stale=True and their real timestamp so the page and alerts can say so.
    """
    sensors = pm25_sensors(key, city, locations)
    cpcb_readings = cpcb_readings or []
    cpcb_station_keys = {}
    for reading in cpcb_readings:
        matched = next((s for s in sensors if plume.haversine_km(
            s["lat"], s["lon"], reading["lat"], reading["lon"]) <= 0.2), None)
        if matched:
            cpcb_station_keys[reading["name"]] = matched["name"]
        else:
            sensor = {k: reading[k] for k in ("name", "lat", "lon")}
            sensor.update({"sensor_id": None, "source": "CPCB", "network": "CPCB",
                           "last_seen": reading["timestamp"]})
            sensors.append(sensor)
            cpcb_station_keys[reading["name"]] = sensor["name"]
    pm = pm25_hours(sensors, key, n_hours)
    for reading in cpcb_readings:
        ts = dt.datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00"))
        hk = ts.strftime("%Y-%m-%dT%H")
        station = cpcb_station_keys[reading["name"]]
        old = pm.setdefault(hk, {}).get(station)
        if old:
            pm[hk][station] = {"value": (old["value"] + reading["pm25"]) / 2,
                                "timestamp": reading["timestamp"],
                                "source": "OpenAQ + CPCB"}
        else:
            pm[hk][station] = {"value": reading["pm25"],
                               "timestamp": reading["timestamp"], "source": "CPCB"}
    stale = []
    if use_last_known:
        reporting = {name for hour in pm.values() for name in hour}
        for s in sensors:
            age = _age_hours(s.get("last_seen"))
            if (s.get("network") != "CPCB" or not s.get("sensor_id")
                    or s["name"] in reporting or age is None or age > 30 * 24):
                continue
            last = last_known_pm25(s, key)
            time.sleep(1.05)  # stay under 60 requests/minute
            if last:
                s.update(last_pm25=last["value"], last_reading_utc=last["timestamp"],
                         reading_source="CPCB via OpenAQ (last known)", stale=True)
                stale.append(dict(name=s["name"], lat=s["lat"], lon=s["lon"],
                                  pm25=last["value"], last_reading_utc=last["timestamp"],
                                  source="CPCB via OpenAQ (last known)", stale=True))
    met = weather_hours(city)
    hours = []
    for hk in sorted(pm.keys(), reverse=True):
        if hk not in met:
            continue
        st = [dict(name=s["name"], lat=s["lat"], lon=s["lon"],
                   pm25=pm[hk][s["name"]]["value"],
                   last_reading_utc=pm[hk][s["name"]]["timestamp"],
                   source=pm[hk][s["name"]]["source"])
              for s in sensors if s["name"] in pm[hk]]
        st += [dict(x) for x in stale]
        for station in st:
            sensor = next(s for s in sensors if s["name"] == station["name"])
            if "last_pm25" not in sensor:
                sensor["last_reading_utc"] = station["last_reading_utc"]
                sensor["last_pm25"] = station["pm25"]
                sensor["reading_source"] = station["source"]
        if len(st) >= 4:
            hours.append({"hour_utc": hk, "stations": st, "met": met[hk]})
        if len(hours) == n_hours:
            break
    for sensor in sensors:
        sensor.setdefault("last_reading_utc", sensor.get("last_seen"))
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
