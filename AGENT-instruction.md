# AGENTS.md — Plume Hunter

Context for an AI coding agent working on this repo. Read this before changing anything.

## What this is

**Plume Hunter** is a hackathon entry for *Environmental Hacks* (WeMakeDevs × AWS, Bharat Builds Tour, Air track, Oct 8–11 2026). It runs a city-scale plume analysis for Delhi.

1. **Physics (core):** same-hour PM2.5 readings from OpenAQ and CPCB stations + Delhi hourly wind → subtract regional background → test every 1 km cell in Delhi as a ground-level source with a **Gaussian plume model** → probability heat map, top-3 candidate areas, plain verdict (`local` / `regional` / `cannot_localise`).
2. **Fruit-fly brain (Fly Hunter):** the real **MaleCNS v1.0 Drosophila connectome** (166,700 neurons, 25.6 M connections, via the `flybrain` package) steers a virtual searcher through the simulated plume to the source. The connectome is **fixed (never trained)**; a small linear **readout** (`readout.npz`) on 32,164 central-brain neurons is trained to decide "source left or right" from smoke at the left/right olfactory receptor neurons. Searcher surges upwind on smell and casts crosswind when it loses it.
3. **AWS:** Lambda runs fetch + physics, writes JSON to S3, emails an SNS alert, and serves the map page through a Function URL.

## Hard rules

- **Be honest in code, README and UI.** Demo results carry `demo: true` and show a DEMO badge; generated map points use real Delhi place names and their popup identifies synthetic readings. Never present the fly-brain path on a result it wasn't computed for (page checks the fly track ends within 5 km of `best`). Don't claim the connectome is trained.
- **Lambda code is pure Python standard library + boto3.** No numpy/scipy in `plume.py`, `data.py`, `lambda_function.py` — the Lambda is a plain zip with no layers. `fly_hunter.py` is the only file that needs `flybrain`/numpy, and it does NOT run in Lambda.
- **Hackathon rules:** repo history must match event dates; demo video < 3 min must show AWS; list AI tools used (Claude) in the README; credit all data/code licences. Don't add features that can't be shown working.
- Never commit secrets (`OPENAQ_API_KEY`, `DATA_GOV_IN_API_KEY`). `.gitignore` already excludes `.env`, `result.json`, `fly_track.json`, `preview.html`, `stations.json`, `hours.json`.

## Files

| File | Role |
| --- | --- |
| `plume.py` | Physics: lat/lon→metres, Pasquill stability, Briggs urban σy/σz (+15° hourly meander), `plume_shape`, `forward`, `invert`, `headline`, `KNOWN_SOURCES` |
| `data.py` | OpenAQ v3 paged India locations grouped within 30 km of Delhi, CPCB data.gov.in hourly PM2.5 (optional key), and Open-Meteo hourly weather. Nearby sources within 200 m are merged; station measurement timestamps are retained. stdlib `urllib` only. `python3 data.py` = live OpenAQ test |
| `lambda_function.py` | Lambda handler. Schedule/console event → `run()`; Function URL GET → HTML page. Query: `?run=1` live run, `?demo=1` synthetic fire, `?format=json` |
| `page.html` | Leaflet map (Esri light-grey tiles, OSM fallback). Placeholders `__DATA__`, `__SOURCES__`, `__FLY__` are replaced server-side |
| `fly_hunter.py` | Connectome navigator: `train` (→ `readout.npz`), `evaluate` (brain vs coin-flip vs upwind-only), `run result.json` (→ `fly_track.json`, 5 searches) |
| `readout.npz` | Trained readout weights (~7.8 MB) |
| `synthetic_test.py` | Plants random fires, reports detectability and localisation error |
| `local_run.py` | Runs everything locally and writes/opens `preview.html` (no AWS) |
| `README.md` | Submission writeup — keep numbers in sync with tests |

## Conventions that bite

- **Wind direction is meteorological** (direction wind blows FROM, degrees clockwise from north). Plume travels toward `wind_from + 180`. Getting this backwards puts sources on the wrong side.
- **Compass headings increase clockwise**, so "turn left" is `heading − angle` (a sign bug here once made the fly brain worse than a coin flip).
- `plume_shape(dx, dy, ...)`: dx, dy = receptor minus source, metres (east, north). Returns µg/m³ per 1 g/s.
- `invert()` refuses to localise unless ≥2 stations exceed 3×noise above background in some hour, and if wind < 1 m/s.
- The live OpenAQ network for Delhi currently returns only ~7 stations (4 reporting per hour) → live verdict is usually `regional`. This is a real finding, not a bug. Demo adds 40 synthetic stations when live < 15 (local_run uses 60 with no API key).

## Current results (keep README in sync)

- Synthetic test, 40 stations, 8 µg/m³ noise, 3 hours stacked: plume reached ≥2 stations in 9/40 trials; median error when detectable **0.9 km** (7/8 within 3 km). 1 hour: 4/40, 2.5 km.
- Connectome readout, held-out AUC: **0.999** (shuffled control 0.48); from descending neurons only 0.68.
- Navigation, 30 searches with wide starts: connectome 17/30, coin flip 15/30, upwind-only 15/30 (small edge — report honestly).
- Demo (Ghazipur synthetic fire): 4/5 brain-guided searches reach the source.

## AWS deployment (sandbox account — constraints matter)

- Account `434504869111`, **region `ap-southeast-2` (Sydney) only.** An organisation SCP explicitly denies S3/SNS/Lambda in every other region tested (ap-south-1, us-east-1, us-west-2). **Bedrock is unusable** (0 token quota) — don't add Bedrock/Strands features.
- S3 bucket: `plume-hunter-434504869111-ap-southeast-2` (analysis in `results/latest.json`, latest per-station snapshot in `results/stations/latest.json`, and `results/fly_track.json`).
- SNS topic: `arn:aws:sns:ap-southeast-2:434504869111:plume-hunter-alerts` (email subscription).
- Lambda `plume-hunter`: Python 3.12, 512 MB, **15 min timeout required** because all Delhi PM2.5 sensors are queried under OpenAQ's 60 requests/minute limit (500 sensors can take about 9 minutes). Env vars: `BUCKET`, `TOPIC_ARN`, `OPENAQ_API_KEY`, optional `DATA_GOV_IN_API_KEY`, `ALWAYS_ALERT=1`. Function URL (auth NONE): `https://rauscx3dnhatkr2zv2euphdjem0gndzh.lambda-url.ap-southeast-2.on.aws/`.
- EventBridge trigger: `rate(30 minutes)` (set in the AWS console). If station values and timestamps have not changed, the Lambda skips inversion and does not overwrite `results/latest.json`.
- **Deploy Lambda:** `zip lambda.zip lambda_function.py plume.py data.py page.html` → console Code → Upload from → .zip (auto-deploys). There is no IAM user / local AWS credentials setup; the human deploys through the console and CloudShell.
- **Fly brain runs in AWS CloudShell, manually** (brain data ~260 MB is too big for a Lambda zip). CloudShell's Python is a venv: use `pip install flybrain` (NOT `--user`). Shell variables reset when CloudShell restarts — re-set `BUCKET`.
  ```
  cd ~/plume-hunter
  BUCKET=plume-hunter-434504869111-ap-southeast-2
  aws s3 cp s3://$BUCKET/results/latest.json result.json   # must be a 'local' result (e.g. after ?demo=1)
  python3 fly_hunter.py run result.json
  aws s3 cp fly_track.json s3://$BUCKET/results/fly_track.json
  ```
- The user's network blocks CARTO map tiles (`basemap.cartocdn.com`); Esri and OpenStreetMap work. Don't switch back to CARTO.

## Local development

```
python3 -m venv .venv && source .venv/bin/activate
pip install flybrain                 # only needed for fly_hunter.py; first FlyBrain() downloads ~260 MB to ~/fly-data
python3 synthetic_test.py            # physics regression check
python3 local_run.py --demo          # writes + opens preview.html
python3 local_run.py --demo --fly    # also runs the connectome
OPENAQ_API_KEY=... python3 local_run.py   # live data
python3 fly_hunter.py train          # retrain readout (~1 min); then evaluate
```

Before deploying, always: run `synthetic_test.py`, run `local_run.py --demo` and look at `preview.html`, and syntax-check the page JS if you edited `page.html`.

## Known limitations / next steps

- Sparse live station network → add India's CPCB feed (data.gov.in, ~40 Delhi stations) or low-cost sensors.
- One city-centre wind for Delhi; single-source model; ignores terrain/buildings and chemistry.
- The optional CPCB connector requires `DATA_GOV_IN_API_KEY`; without it the analysis uses OpenAQ only. The data.gov.in resource is documented as hourly and reports `pollutant_avg` with `last_update`.
- Automate Fly Hunter (container-image Lambda or ECS job) if the account allows; currently manual in CloudShell.
- Closed-loop training of a readout on descending/motor neurons so the brain does more of the navigation.
- Optional AI alert/complaint drafting (Strands Agents + Bedrock) once Bedrock quota exists.
