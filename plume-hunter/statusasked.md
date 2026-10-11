# Plume Hunter: project status

**As of:** 2026-10-11 21:53 IST (16:23 UTC)
**Branch:** `main`. Last commit `ce4b811 commit project`. Most of the work below is **not committed yet** (9 files changed, ~980 lines added).

## In one paragraph

Plume Hunter takes PM2.5 readings from Delhi's monitoring stations and the hourly wind. It removes the regional background haze, then uses a Gaussian plume model to test every 1 km cell in Delhi as a possible ground-level source. The output is a probability heat map, the top 3 candidate areas (labelled with the nearest known landfill or industrial area), and a plain verdict: *local source*, *regional haze*, or *wind too calm to tell*. When there is a local source, a virtual searcher steered by the real fruit-fly connectome follows the simulated plume to it. Results go to S3, alerts go out by SNS email, and a Leaflet map page is served from the Lambda.

## What the project does right now

### 1. Data collection (`data.py`)
| Source | Status | Notes |
| --- | --- | --- |
| OpenAQ v3 (`OPENAQ_API_KEY`) | Working | ~100 PM2.5 stations listed within 30 km of Delhi. Only **6 are reporting currently** (AirGradient low-cost sensors). |
| CPCB via OpenAQ | **Stale** | OpenAQ's CPCB feed stopped around **7 Oct 2026, 14:30 UTC**. |
| CPCB via data.gov.in (`DATA_GOV_IN_API_KEY`) | **Unreachable** | Key is in `.env`, but connections to `api.data.gov.in` are refused from this machine. The Lambda (Sydney) may be refused too; not tested. Falls back to OpenAQ only (`cpcb_status: "unavailable"`). |
| Open-Meteo weather | Working | Hourly wind, cloud and sunshine at Delhi's centre. No key needed. |

- **Cities:** `CITIES` now contains **Delhi only**.
- **Last-known CPCB values:** CPCB stations with no current reading (seen in the last 30 days) contribute their newest value from the last 10 days to **every hour of the analysis**. These entries are marked `stale: True` and keep their real timestamp.
  - Last live run: 6 current + 28 last-known stations. 27 values from 7 Oct 14:00 UTC, 1 from 7 Oct 11:00 UTC.
- **Faster fetching:** sensors silent for more than 24 h are no longer asked for recent hours.
- **Run time:** a Delhi live run takes about **2 minutes** locally, mostly because OpenAQ requests are paced at 1 per second.

### 2. Physics (`plume.py`)
- Gaussian plume forward model with Briggs urban dispersion, Pasquill stability classes A–F, and a 15° hourly wind-meander term.
- Grid inversion over Delhi's bounding box. Up to 3 hours are combined, and the verdict requires at least 2 stations above 3× the noise level.
- **Latest live verdict:** *Regional haze*. The best fit explained 25% of the readings (30% is needed for a local verdict). The top candidate was at the edge of the grid, near Anand Vihar, 17.7 km away.
- **Synthetic regression test** (`synthetic_test.py`, passing):
  - 3 hours stacked: plume reached at least 2 stations in 9/40 trials; median error 0.9 km; 7/8 within 3 km.
  - 1 hour: 4/40 trials; median error 2.5 km.

### 3. Fly Hunter (`fly_hunter.py`)
- Uses the MaleCNS v1.0 connectome (166,700 neurons) through the `flybrain` package. The wiring is fixed.
- The only trained part is the readout `readout.npz`. It was trained once and does not need retraining unless the brain, the watched neurons or the simulation settings change.
- Modes: `train`, `evaluate`, `run result.json` (5 searches), `swarm result.json` (12 flies).
- Runs only when the verdict is *local*. With live Delhi data it currently doesn't run; it runs on `--demo`.
- **Locally:** `flybrain` is installed in `../.venv`. The brain data (~260 MB) has **not been downloaded yet** (`~/fly-data` is missing); the first run will download it.
- **On AWS:** runs by hand in CloudShell. Its output `fly_track.json` is uploaded to S3.
- Reported results: held-out AUC 0.999 (shuffled control 0.48); 17/30 searches reached the source (coin flip 15/30, upwind-only 15/30).

### 4. AWS (`lambda_function.py`)
- **Lambda `plume-hunter`** (ap-southeast-2):
  - fetches data, runs the physics, writes results to S3 (`results/<city>/latest.json`, `results/latest.json`, `results/index.json`, plus a station snapshot)
  - sends an SNS alert
  - serves the map page
- It skips the physics run when no station values have changed.
- Alert emails now include a note listing the last-known CPCB values used and their dates.
- **Not yet confirmed in the AWS console:**
  - the latest code has been deployed
  - the timeout is raised from 3 min (Delhi alone takes ~2 min; `AGENTS.md` says 15 min)
  - the EventBridge schedule is set to `rate(30 minutes)`
  - `DATA_GOV_IN_API_KEY` has been added

### 5. Map page (`page.html`)
- Leaflet map (Esri tiles, OpenStreetMap fallback).
- Stations are coloured by PM2.5, or grey when there's no current reading.
- Clicking a station opens a panel with:
  - the reading
  - its status (*Live reading* / *Last known reading · not current* / *Offline*)
  - the timestamp, source and network
- Header: update time, refresh note, live/offline counts, and CPCB status.
- When last-known values are used, the verdict box says how many CPCB stations and which dates, and the Stations box shows "N current + M last-known".
- Demo mode (`?demo=1`): a synthetic Ghazipur fire with a DEMO badge. Demo points now use real Delhi place names (`DEMO_PLACES`), and their popups say the readings are synthetic.

### 6. Local setup
- `.env` contains `OPENAQ_API_KEY` and `DATA_GOV_IN_API_KEY`. It is ignored by git.
- Load it with `set -a; source .env; set +a`, then run:
  - `python3 local_run.py` for live data
  - `--demo` for the synthetic fire
  - `--fly` or `--swarm` to add the fly brain
- The run writes `result.json` and `preview.html`, both ignored by git.

## Known issues and risks

1. **Old data with current wind.** The 28 CPCB values from 7 Oct are analysed with today's wind. Differences between stations from 7 Oct can show up as a fake "local source" near a real named site. This is labelled on the page and in alerts, but it can still produce misleading verdicts, and it gets worse each day. Consider an age limit (for example 3 days).
2. **Few current stations.** Only 6 low-cost sensors are reporting now, all in South Delhi.
3. **data.gov.in is unreachable.** The cause is unknown: this machine's network sandbox, or blocking by data.gov.in.
4. **Lambda timeout.** If it's still 3 minutes, a Delhi run (~2 min plus S3 calls) has little headroom.
5. **Docs out of sync.**
   - `README.md` and `AGENTS.md` don't mention last-known values yet.
   - `CLAUDE.md` used to be a copy of `AGENTS.md` and now differs from it.
   - `README.md` still says the CPCB feed is hourly; this hasn't been checked against real data.
6. **Duplicate station names.** Stations are keyed by name, and OpenAQ lists some CPCB stations twice (e.g. "Shadipur, Delhi - CPCB"), so one can overwrite the other.
7. **Keys exposed in chat.** The OpenAQ and data.gov.in keys were shared in a chat session. Rotate them after the hackathon.

## Suggested next steps

1. Commit the current work. Hackathon rules require the repo history to fall within the event dates (8–11 Oct).
2. Deploy the zip (`lambda_function.py plume.py data.py page.html`), raise the timeout, set the 30-minute schedule, and add `DATA_GOV_IN_API_KEY`.
3. Decide on an age limit for last-known values.
4. Update `README.md` and `AGENTS.md`, and bring `CLAUDE.md` back in line.
5. Record the demo video (under 3 minutes, showing AWS).
