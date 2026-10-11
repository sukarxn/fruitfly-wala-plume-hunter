"""Fetch a one-time local OpenAQ history seed for the rolling 7-day map.

Requires OPENAQ_API_KEY in the environment or .env. Writes history_seed.json;
it does not upload anything to AWS.
"""

import datetime as dt
import json
import os
from pathlib import Path

import data
import plume


ROOT = Path(__file__).resolve().parent


def _env_file_key():
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("OPENAQ_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def main():
    key = os.environ.get("OPENAQ_API_KEY") or _env_file_key()
    if not key:
        raise SystemExit("OPENAQ_API_KEY is missing from the environment and .env")
    result_path = ROOT / "result.json"
    if not result_path.exists():
        raise SystemExit("result.json is needed for the latest station IDs and coordinates")
    latest = json.loads(result_path.read_text())
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(hours=168)
    # Skip sensors that have not reported in the recent period.
    sensors = [s for s in latest.get("stations", []) if s.get("sensor_id")
               and data._age_hours(s.get("last_seen")) is not None
               and data._age_hours(s.get("last_seen")) <= 14 * 24]
    print(f"Fetching 168 hours for {len(sensors)} recently reporting OpenAQ sensors.", flush=True)
    readings = data.history_hours(sensors, key, start, now)
    met = data.weather_hours("delhi", past_days=7)
    hours = data.assemble_history(sensors, readings, met)
    docs = []
    grids = []
    for hour in hours:
        grid = plume.hourly_grid(hour, bbox=data.CITIES["delhi"]["bbox"])
        doc = {"hour_utc": hour["hour_utc"], "met": hour["met"],
               "stations": hour["stations"], "grid": grid}
        docs.append(doc)
        if grid:
            grids.append(grid)
    history = plume.invert_hourly_grids(grids, bbox=data.CITIES["delhi"]["bbox"],
                                        sources=plume.CITY_SOURCES["delhi"])
    history.update({"label": "Likely source areas, last 7 days",
                    "start_utc": start.isoformat(), "end_utc": now.isoformat(),
                    "hours_available": len(hours), "window_hours": 168,
                    "headline": plume.headline(history)})
    history["winds"] = [dict(h["met"], hour_utc=h["hour_utc"]) for h in docs if h.get("met")]
    history["corridor"] = plume.weekly_corridor(history,
                                         [h for k, h in met.items() if start.strftime("%Y-%m-%dT%H") <= k
                                          < now.strftime("%Y-%m-%dT%H")],
                                         bbox=data.CITIES["delhi"]["bbox"])
    output = {"source": "OpenAQ sensor hours + Open-Meteo hourly wind",
              "attribution": "Air-quality values: OpenAQ contributing networks. Wind: Open-Meteo.",
              "created_utc": now.isoformat(), "history": history, "hours": docs}
    out = ROOT / "history_seed.json"
    out.write_text(json.dumps(output, separators=(",", ":")))
    print(f"Saved {len(docs)} station-hours ({history['hours_used']} usable grids) to {out.name}.", flush=True)
    print(f"Weekly map status: {history.get('reason') or history.get('headline')}", flush=True)


if __name__ == "__main__":
    main()
