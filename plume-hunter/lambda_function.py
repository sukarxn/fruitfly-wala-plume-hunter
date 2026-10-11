"""
Plume Hunter - AWS Lambda entry point (Python 3.11+, no extra packages).

Two ways this function is called:
1. On a schedule (EventBridge) or the console "Test" button:
   fetch data -> run the inversion -> save JSON to S3 -> SNS alert.
2. Through its Function URL from a browser:
   returns the map page for the latest saved result.
     ?run=1   run a fresh analysis first (takes ~1 minute)
     ?demo=1  plant a synthetic fire at Ghazipur to demonstrate the method

Environment variables:
   BUCKET          S3 bucket for results (required)
   TOPIC_ARN       SNS topic for alerts (optional)
   OPENAQ_API_KEY  OpenAQ key (or store it in SSM at OPENAQ_PARAM)
   OPENAQ_PARAM    default /plume-hunter/openaq-key
   ALWAYS_ALERT    "1" to email every run (handy for the demo video)
"""

import datetime as dt
import json
import os
import random

import boto3

import data
import plume

s3 = boto3.client("s3")
BUCKET = os.environ.get("BUCKET")
LATEST = "results/latest.json"


def _api_key():
    if os.environ.get("OPENAQ_API_KEY"):
        return os.environ["OPENAQ_API_KEY"]
    ssm = boto3.client("ssm")
    name = os.environ.get("OPENAQ_PARAM", "/plume-hunter/openaq-key")
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _demo_hours(sensors):
    """Synthetic: a 300 g/s fire at Ghazipur over 3 hours, with the wind
    blowing the smoke towards the nearest stations (so the demo is visible)."""
    import math
    src = next((la, lo) for n, la, lo in plume.KNOWN_SOURCES if n.startswith("Ghazipur"))
    rnd = random.Random(7)
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


def run(demo=False):
    key = _api_key()
    sensors, hours = data.build_hours(key) if not demo else (data.pm25_sensors(key), None)
    if demo:
        hours = _demo_hours(sensors)
    res = plume.invert(hours)
    res["headline"] = plume.headline(res)
    res["demo"] = demo
    res["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    res["hours"] = [{"hour_utc": h["hour_utc"], "met": h["met"],
                     "stations": h["stations"]} for h in hours]

    body = json.dumps(res).encode()
    s3.put_object(Bucket=BUCKET, Key=LATEST, Body=body, ContentType="application/json")
    stamp = res["generated_utc"][:13] + ("-demo" if demo else "")
    s3.put_object(Bucket=BUCKET, Key=f"results/{stamp}.json", Body=body,
                  ContentType="application/json")
    _alert(res)
    return res


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
    for i, t in enumerate(res.get("top", []), 1):
        lines.append(f"{i}. {t['lat']:.3f}, {t['lon']:.3f} - p={t['prob']:.2f}, "
                     f"near {t['near']['name']} ({t['near']['km']} km), "
                     f"est. {t['q_gps']:.0f} g/s PM2.5")
    if os.environ.get("PAGE_URL"):
        lines += ["", f"Map: {os.environ['PAGE_URL']}"]
    lines += ["", "Plume Hunter - Gaussian plume source finder for Delhi."]
    boto3.client("sns").publish(TopicArn=os.environ["TOPIC_ARN"],
                                Subject="Plume Hunter: possible pollution source",
                                Message="\n".join(lines))


def _latest():
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=LATEST)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def lambda_handler(event, context):
    http = (event or {}).get("requestContext", {}).get("http")
    if not http:  # schedule or console test
        demo = bool((event or {}).get("demo"))
        res = run(demo=demo)
        return {"headline": res["headline"], "verdict": res["verdict"],
                "hours_used": res["hours_used"]}

    q = event.get("queryStringParameters") or {}
    if q.get("run") == "1" or q.get("demo") == "1":
        res = run(demo=q.get("demo") == "1")
    else:
        res = _latest()
    if q.get("format") == "json":
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(res)}
    html = PAGE.replace("__DATA__", json.dumps(res).replace("</", "<\\/"))
    html = html.replace("__SOURCES__", json.dumps(plume.KNOWN_SOURCES))
    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": html}


with open(os.path.join(os.path.dirname(__file__), "page.html"), encoding="utf-8") as f:
    PAGE = f.read()
