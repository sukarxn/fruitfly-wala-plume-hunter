"""
Run Plume Hunter on your own computer, no AWS needed, and preview the map.

    python3 local_run.py --demo          # synthetic Ghazipur fire (no API key needed)
    OPENAQ_API_KEY=xxx python3 local_run.py   # live Delhi data
    python3 local_run.py --demo --fly    # also run the fruit-fly brain (needs: pip install flybrain)
    python3 local_run.py --demo --swarm  # 12-fly swarm, majority-vote destination

Writes result.json and preview.html; open preview.html in your browser.
Same physics and page as the Lambda, so what you see here is what you deploy.
"""

import datetime as dt
import json
import os
import random
import sys
import webbrowser

import plume
import data


def demo_hours(sensors):
    """Same synthetic fire as lambda_function._demo_hours."""
    import math
    src = next((la, lo) for n, la, lo in plume.KNOWN_SOURCES if n.startswith("Ghazipur"))
    rnd = random.Random(7)
    if len(sensors) < 15:
        sensors = list(sensors) + [
            {"name": name, "lat": lat, "lon": lon}
            for name, lat, lon in data.DEMO_PLACES]
    near = sorted(sensors, key=lambda s: plume.haversine_km(src[0], src[1], s["lat"], s["lon"]))[:3]
    hours = []
    for h, s in enumerate(near):
        dx, dy = plume.to_xy(s["lat"], s["lon"], src[0], src[1])
        wind_from = (math.degrees(math.atan2(dx, dy)) + 180) % 360
        met = {"wind_ms": 2.5, "wind_from": wind_from, "stability": "D",
               "is_day": True, "cloud_pct": 10, "solar_wm2": 450}
        pred = plume.forward(src[0], src[1], 300, sensors, met)
        st = [dict(name=x["name"], lat=x["lat"], lon=x["lon"],
                   pm25=round(80 + p + rnd.gauss(0, 8), 1)) for x, p in zip(sensors, pred)]
        hours.append({"hour_utc": f"demo-{h}", "stations": st, "met": met})
    return hours


def main():
    demo = "--demo" in sys.argv
    key = os.environ.get("OPENAQ_API_KEY")
    sensors = []
    cpcb_status = None
    if key:
        if demo:
            sensors = data.pm25_sensors(key)
        else:
            # Same fallback as the Lambda: if CPCB is unreachable, carry on with
            # OpenAQ; CPCB stations stay on the map greyed out.
            try:
                cpcb = data.cpcb_pm25("Delhi")
                cpcb_status = ("live" if cpcb else "no recent rows"
                               if os.environ.get("DATA_GOV_IN_API_KEY") else "not configured")
            except Exception as e:
                cpcb, cpcb_status = [], "unavailable"
                print(f"CPCB feed unavailable ({type(e).__name__}); using OpenAQ only.")
            sensors, hours = data.build_hours(key, cpcb_readings=cpcb)
    elif not demo:
        raise SystemExit("Set OPENAQ_API_KEY for live data, or use --demo.")
    if demo:
        hours = demo_hours(sensors)

    res = plume.invert(hours)
    res["headline"] = plume.headline(res)
    res["demo"] = demo
    res["cpcb_status"] = cpcb_status
    res["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    res["hours"] = hours
    if demo and hours:
        res["stations"] = [dict(s, last_pm25=s["pm25"],
                                 last_reading_utc=res["generated_utc"],
                                 reading_source="Synthetic demo readings")
                            for s in hours[0]["stations"]]
    else:
        res["stations"] = sensors
    with open("result.json", "w") as f:
        json.dump(res, f)
    print("VERDICT:", res["headline"])

    fly = None
    if "--swarm" in sys.argv and res.get("best"):
        import fly_hunter
        fly = fly_hunter.swarm("result.json")
    elif "--fly" in sys.argv and res.get("best"):
        import fly_hunter
        fly = fly_hunter.run("result.json")
    elif os.path.exists("fly_track.json"):
        with open("fly_track.json") as f:
            fly = json.load(f)

    with open("page.html", encoding="utf-8") as f:
        page = f.read()
    page = (page.replace("__DATA__", json.dumps(res).replace("</", "<\\/"))
                .replace("__SOURCES__", json.dumps(plume.KNOWN_SOURCES))
                .replace("__FLY__", json.dumps(fly))
                .replace("__CITY_INDEX__", json.dumps({"cities": [
                    {"id": "delhi", "name": "Delhi", "centre": [28.63, 77.15],
                     "hours_used": res.get("hours_used", 0)}]}))
                .replace("__CITY__", "delhi")
                .replace("__STATIONS__", json.dumps(res["stations"])))
    with open("preview.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote preview.html")
    webbrowser.open("file://" + os.path.abspath("preview.html"))


if __name__ == "__main__":
    main()
