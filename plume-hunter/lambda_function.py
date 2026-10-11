"""
Plume Hunter - AWS Lambda entry point (Python 3.11+, no extra packages).

Two ways this function is called:
1. On a schedule (EventBridge) or the console "Test" button:
   fetch data -> run the inversion -> save JSON to S3 -> SNS alert.
2. Through its Function URL from a browser:
   returns the map page for the latest saved result.
     ?run=1   poll sources and run a fresh analysis (may take several minutes)
     ?demo=1  plant a synthetic fire at Ghazipur to demonstrate the method

Environment variables:
   BUCKET          S3 bucket for results (required)
   TOPIC_ARN       SNS topic for alerts (optional)
   OPENAQ_API_KEY  OpenAQ key (or store it in SSM at OPENAQ_PARAM)
   OPENAQ_PARAM    default /plume-hunter/openaq-key
   ALWAYS_ALERT    "1" to email every run (handy for the demo video)
"""

import datetime as dt
import hashlib
import json
import os
import random

import boto3

import data
import plume

s3 = boto3.client("s3")
BUCKET = os.environ.get("BUCKET")
LATEST = "results/latest.json"
STATION_SNAPSHOT = "results/stations/latest.json"


def _api_key():
    if os.environ.get("OPENAQ_API_KEY"):
        return os.environ["OPENAQ_API_KEY"]
    ssm = boto3.client("ssm")
    name = os.environ.get("OPENAQ_PARAM", "/plume-hunter/openaq-key")
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _demo_hours(sensors, city):
    """Synthetic: a 300 g/s fire at a city's reference source over 3 hours, with the wind
    blowing the smoke towards the nearest stations (so the demo is visible)."""
    import math
    sources = plume.CITY_SOURCES[city]
    src = sources[0][1:]
    rnd = random.Random(7)
    if len(sensors) < 15:  # live network too sparse: add a labelled synthetic network
        sensors = list(sensors) + [
            {"name": name, "lat": lat, "lon": lon}
            for name, lat, lon in data.DEMO_PLACES]
    near = sorted(sensors, key=lambda s: plume.haversine_km(src[0], src[1], s["lat"], s["lon"]))[:3]
    winds = []
    for s in near:  # wind FROM = opposite of the bearing source -> station
        dx, dy = plume.to_xy(s["lat"], s["lon"], src[0], src[1])
        winds.append((math.degrees(math.atan2(dx, dy)) + 180) % 360)
    hours = []
    for h, wind_from in enumerate(winds):
        met = {"wind_ms": 2.5, "wind_from": wind_from, "stability": "D",
               "is_day": True, "cloud_pct": 10, "solar_wm2": 450}
        pred = plume.forward(src[0], src[1], 300, sensors, met)
        st = [dict(name=s["name"], lat=s["lat"], lon=s["lon"],
                   pm25=round(80 + p + rnd.gauss(0, 8), 1)) for s, p in zip(sensors, pred)]
        hours.append({"hour_utc": f"demo-{h}", "stations": st, "met": met})
    return hours


def _run_city(city, key, demo=False, locations=None, cpcb_readings=None):
    if demo:
        sensors = data.pm25_sensors(key, city, locations)
        hours = _demo_hours(sensors, city)
    else:
        sensors, hours = data.build_hours(key, city=city, locations=locations,
                                          cpcb_readings=cpcb_readings)
    cfg = data.CITIES[city]
    if demo and hours:
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        sensors = [dict(s, last_pm25=s["pm25"], last_reading_utc=timestamp,
                        reading_source="Synthetic demo readings")
                   for s in hours[0]["stations"]]
    return {"city": city, "city_name": cfg["name"], "demo": demo,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
            "stations": sensors, "hours": hours}


def run(demo=False):
    key = _api_key()
    locations = data.india_locations(key)
    cpcb_error = None
    try:
        cpcb = data.cpcb_pm25("Delhi")
    except Exception as e:
        cpcb, cpcb_error = [], type(e).__name__
    results = {}
    changed = False
    index = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
             "cities": []}
    for city, cfg in data.CITIES.items():
        try:
            res = _run_city(city, key, demo, locations, cpcb)
        except Exception as e:
            res = {"city": city, "city_name": cfg["name"], "verdict": "cannot_localise",
                   "headline": f"{cfg['name']}: data unavailable ({type(e).__name__}).",
                   "reason": str(e), "best": None, "top": [], "hours": [],
                   "hours_used": 0, "demo": demo, "city_error": True,
                   "generated_utc": index["generated_utc"]}
        old_snapshot = _get_json(STATION_SNAPSHOT) or {}
        previous = {s.get("station_key"): s for s in old_snapshot.get("stations", [])}
        current = res.get("stations", [])
        for station in current:
            station["station_key"] = f"{station['lat']:.5f},{station['lon']:.5f}"
            prior = previous.get(station["station_key"], {})
            if station.get("last_pm25") is None and prior.get("last_pm25") is not None:
                station["last_pm25"] = prior["last_pm25"]
                station["last_reading_utc"] = prior.get("last_reading_utc")
                station["reading_source"] = prior.get("reading_source")
        current_keys = {s["station_key"] for s in current}
        current.extend({**s, "offline": True} for key_, s in previous.items()
                       if key_ and key_ not in current_keys)
        res["stations"] = current
        res["cpcb_status"] = ("unavailable" if cpcb_error else
                              "live" if cpcb else
                              "not configured" if not os.environ.get("DATA_GOV_IN_API_KEY") else
                              "no recent rows")
        if cpcb_error:
            res["cpcb_error"] = cpcb_error
        signature_data = sorted((s["lat"], s["lon"], s.get("last_pm25"),
                                 s.get("last_reading_utc"))
                                for s in current if s.get("last_pm25") is not None)
        signature = (hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
                     if signature_data else None)
        if res.get("city_error"):
            cached_result = _get_json(f"results/{city}/latest.json")
            if cached_result:
                results[city] = cached_result
                index["cities"].append({"id": city, "name": cfg["name"],
                                        "centre": cfg["centre"],
                                        "verdict": cached_result["verdict"],
                                        "hours_used": cached_result["hours_used"]})
                continue
        cached_result = _get_json(f"results/{city}/latest.json")
        no_new_observations = (not demo and (
            (signature and signature == old_snapshot.get("data_signature")) or
            (not signature and cached_result is not None)))
        if no_new_observations:
            results[city] = cached_result
            index["cities"].append({"id": city, "name": cfg["name"],
                                    "centre": cfg["centre"], "verdict": cached_result["verdict"],
                                    "hours_used": cached_result["hours_used"]})
            continue
        if not res.get("city_error"):
            calculated = plume.invert(res.get("hours", []), bbox=cfg["bbox"],
                                      sources=plume.CITY_SOURCES[city])
            calculated.update({"city": city, "city_name": cfg["name"],
                               "headline": plume.headline(calculated), "demo": demo,
                               "generated_utc": res["generated_utc"],
                               "cpcb_status": res.get("cpcb_status"),
                               "cpcb_error": res.get("cpcb_error"),
                               "stations": current,
                               "hours": [{"hour_utc": h["hour_utc"], "met": h["met"],
                                          "stations": h["stations"]}
                                         for h in res.get("hours", [])]})
            res = calculated
        changed = True
        res["data_signature"] = signature
        results[city] = res
        body = json.dumps(res).encode()
        s3.put_object(Bucket=BUCKET, Key=f"results/{city}/latest.json", Body=body,
                      ContentType="application/json")
        stamp = res["generated_utc"][:13] + ("-demo" if demo else "")
        s3.put_object(Bucket=BUCKET, Key=f"results/{city}/{stamp}.json", Body=body,
                      ContentType="application/json")
        index["cities"].append({"id": city, "name": cfg["name"],
                                "centre": cfg["centre"], "verdict": res["verdict"],
                                "hours_used": res["hours_used"],
                                "city_error": bool(res.get("city_error"))})
        _alert(res)
    # Keep the old latest object as the default city result for existing consumers.
    default = results["delhi"]
    if changed:
        s3.put_object(Bucket=BUCKET, Key=LATEST, Body=json.dumps(default).encode(),
                      ContentType="application/json")
        s3.put_object(Bucket=BUCKET, Key="results/index.json", Body=json.dumps(index).encode(),
                      ContentType="application/json")
    if "delhi" in results and not results["delhi"].get("city_error"):
        station_data = results["delhi"].get("stations", [])
        s3.put_object(Bucket=BUCKET, Key=STATION_SNAPSHOT,
                      Body=json.dumps({"saved_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                       "data_signature": results["delhi"].get("data_signature"),
                                       "stations": station_data}).encode(),
                      ContentType="application/json")
    return {"results": results, "index": index}


def _alert(res):
    topic = os.environ.get("TOPIC_ARN")
    if not topic:
        return
    worth_it = res["verdict"] == "local" and res.get("confidence") in ("medium", "high")
    if not (worth_it or os.environ.get("ALWAYS_ALERT") == "1"):
        return
    h = res["hours"][0] if res["hours"] else None
    lines = [("[DEMO - synthetic fire] " if res["demo"] else "") + res["headline"], ""]
    if h:
        m = h["met"]
        lines.append(f"Wind from {m['wind_from']:.0f} deg at {m['wind_ms']:.1f} m/s "
                     f"(stability {m['stability']}); {len(h['stations'])} stations; "
                     f"background PM2.5 {res.get('background_ug', '?')} ug/m3.")
        stale = [s for s in h["stations"] if s.get("stale")]
        if stale:
            dates = sorted(s["last_reading_utc"][:10] for s in stale)
            lines.append(f"Note: {len(stale)} CPCB stations had no current reading; their last "
                         f"known values ({dates[0]} to {dates[-1]}) were used in this analysis.")
    for i, t in enumerate(res.get("top", []), 1):
        lines.append(f"{i}. {t['lat']:.3f}, {t['lon']:.3f} - p={t['prob']:.2f}, "
                     f"near {t['near']['name']} ({t['near']['km']} km), "
                     f"est. {t['q_gps']:.0f} g/s PM2.5")
    if os.environ.get("PAGE_URL"):
        lines += ["", f"Map: {os.environ['PAGE_URL']}"]
    lines += ["", f"Plume Hunter - Gaussian plume source finder for {res.get('city_name', res.get('city', 'India'))}."]
    boto3.client("sns").publish(TopicArn=os.environ["TOPIC_ARN"],
                                Subject="Plume Hunter: possible pollution source",
                                Message="\n".join(lines))


def _get_json(key):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _latest():
    return _get_json(LATEST)


def lambda_handler(event, context):
    http = (event or {}).get("requestContext", {}).get("http")
    if not http:  # schedule or console test
        demo = bool((event or {}).get("demo"))
        res = run(demo=demo)
        return {"cities": [{"city": city, "headline": r["headline"],
                            "verdict": r["verdict"], "hours_used": r["hours_used"]}
                           for city, r in res["results"].items()]}

    q = event.get("queryStringParameters") or {}
    if q.get("run") == "1" or q.get("demo") == "1":
        run(demo=q.get("demo") == "1")
    city = q.get("city", "delhi")
    if city not in data.CITIES:
        city = "delhi"
    res = _get_json(f"results/{city}/latest.json") or _latest()
    city_index = _get_json("results/index.json") or {"cities": []}
    if q.get("format") == "json":
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(res)}
    fly = _get_json("results/fly_track.json")
    station_snapshot = _get_json(STATION_SNAPSHOT) or {"stations": []}
    html = PAGE.replace("__DATA__", json.dumps(res).replace("</", "<\\/"))
    html = html.replace("__FLY__", json.dumps(fly))
    html = html.replace("__STATIONS__", json.dumps(station_snapshot.get("stations", [])))
    html = html.replace("__SOURCES__", json.dumps(plume.CITY_SOURCES.get(city, [])))
    html = html.replace("__CITY_INDEX__", json.dumps(city_index))
    html = html.replace("__CITY__", city)
    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": html}


with open(os.path.join(os.path.dirname(__file__), "page.html"), encoding="utf-8") as f:
    PAGE = f.read()
