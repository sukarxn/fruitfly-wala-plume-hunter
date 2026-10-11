# Plume Hunter

**City air monitors can measure pollution, but often cannot show where a local plume began.**
Plume Hunter turns a pollution spike at monitoring stations into a map of where the smoke most likely came from, using wind physics.

Track: **Air** · Environmental Hacks (Bharat Builds Tour, WeMakeDevs × AWS)

## How it works

1. **Data.** Every 30 minutes, the Lambda reads Delhi PM2.5 from OpenAQ and, when `DATA_GOV_IN_API_KEY` is configured, the CPCB real-time feed on data.gov.in. The feed is documented as hourly; `pollutant_avg` is used with its `last_update` time. Matching stations within 200 m are merged. Each station reading keeps its own timestamp, and only readings from the same hour are used together with the hourly wind.
2. **Background removed.** The 20th-percentile station is treated as regional haze (stubble smoke, secondary particles); only the excess above it is traced.
3. **Physics.** Every 1 km cell in that city's grid is tested as a ground-level source with the Gaussian plume model:

   C = Q / (π u σy σz) · exp(−y² / 2σy²) · exp(−H² / 2σz²)

   using Briggs urban dispersion coefficients, a Pasquill stability class (A–F) from wind speed and sunshine or night cloud, and a 15° hourly wind-meander term.
4. **Inversion.** For each cell the best source strength Q has a closed form; the misfit between predicted and measured excess is summed over up to 3 hours. As the wind turns, each hour's plume points a different way, and only the true source fits all of them.
5. **Honest output.** A probability heat map, the top 3 candidate areas labelled with the nearest known source (landfills, industrial areas), and a plain verdict: *local source*, *regional haze*, or *wind too calm to tell*. The model refuses to guess when fewer than 2 stations see a plume.
6. **Alert.** Medium or high-confidence local sources trigger an email through Amazon SNS.

## Results (synthetic recovery test)

`python3 synthetic_test.py` plants random fires (150–500 g/s) among 40 stations with 8 µg/m³ sensor noise:

| Hours stacked | Plume reached ≥2 stations | Median error when it did |
| --- | --- | --- |
| 1 | 4 / 40 | 2.5 km |
| 3 | 9 / 40 | **0.9 km** (7 of 8 within 3 km) |

**The finding:** when the plume reaches two or more stations, the physics pins the source to about 1 km. Most of the time it doesn't, because Delhi's station network is sparse compared with plume widths. That is itself useful: it shows where extra low-cost sensors would make enforcement possible.

## Fly Hunter: the real fruit-fly connectome tracks the plume

Once the physics has found the likely source area, a virtual searcher has to get there through patchy, wind-blown smoke. Plume Hunter steers it with the **complete male fruit-fly nervous system** (MaleCNS v1.0: 166,700 neurons, 25.6 million connections, mapped by electron microscopy), run as a spiking network with the [`flybrain`](https://pypi.org/project/flybrain/) package.

1. Smoke concentration at the left and right "antenna" (two sensors 60 m apart) drives the fly's **real olfactory receptor neurons** on that side.
2. The connectome runs for 0.24 s of brain time. Its wiring is never changed.
3. A small **trained readout** on 32,164 central-brain neurons decides "source to my left or right?" This is reservoir computing: the fixed brain does the nonlinear processing, and only the linear readout is trained.
4. The searcher behaves like a real fly in a plume: it **surges upwind** while it smells smoke, steering as the brain decides, and **casts crosswind** in widening zig-zags when it loses the scent.

| Result | Value |
| --- | --- |
| Left/right decoding from central-brain activity, held-out AUC | **0.999** (shuffled-label control: 0.48) |
| Same, from descending neurons (the brain's commands to the body) | 0.68 |
| Reaching the source, 30 searches with wide starting offsets | connectome **17/30**, coin-flip steering 15/30, upwind-only 15/30 |

**Swarm mode.** Twelve connectome-steered flies launch from points spread across the downwind side. The smoke they follow mixes the physics' top-3 candidate areas, weighted by probability, and the map rings the destination where most flies land. In the demo, 6 of 12 flies found the plume and all 6 landed on candidate #1 (Ghazipur); the others never found the plume. The smoke is simulated from the physics' own estimate, so the swarm shows that the brain can navigate the plume, not independent evidence; in the field, this is the step where low-cost drones with real sensors would confirm which candidate is burning.

What this shows: the real wiring carries a near-perfect left/right smell signal into the central brain. As a navigator it gives only a small edge over simple controls in this test, because crosswind casting does most of the work. Pulling more of the steering out of the brain (for example, readouts on motor groups or descending neurons, trained in closed loop) is the obvious next step.

The brain is too big for a Lambda zip (≈260 MB of data), so `fly_hunter.py` runs in **AWS CloudShell**. It reads the physics result from S3 and writes `fly_track.json` back to S3, and the map page draws it in purple.

```bash
pip install flybrain                      # numba CPU build; first run downloads the brain
python3 fly_hunter.py train               # train the readout (~1 min)
python3 fly_hunter.py evaluate            # brain vs. controls (~2 min)
python3 fly_hunter.py run result.json     # track the source in a physics result
python3 fly_hunter.py swarm result.json   # 12 flies from across the downwind side; majority vote
```

## AWS architecture

| Service | Role |
| --- | --- |
| AWS Lambda | Polls and analyzes Delhi every 30 minutes, and serves the map through a Function URL |
| Amazon EventBridge | 30-minute schedule (set in the AWS console) |
| Amazon S3 | Stores the current station snapshot at `results/stations/latest.json` and the analysis at `results/latest.json` |
| Amazon SNS | Email alert when a local source is found |
| AWS Systems Manager Parameter Store (optional) | Holds the OpenAQ API key |
| AWS CloudShell | Runs the fruit-fly connectome (Fly Hunter) and uploads its path to S3 |

No third-party packages: the physics is pure Python, so the Lambda is one small zip.

## Files

| File | What it is |
| --- | --- |
| `plume.py` | Physics: coordinates, stability, Briggs sigmas, plume model, inversion, verdict |
| `data.py` | OpenAQ + Open-Meteo fetchers; run directly to test with live data |
| `lambda_function.py` | Lambda handler (schedule + web page + SNS) |
| `page.html` | Leaflet map page |
| `synthetic_test.py` | Recovery test |
| `fly_hunter.py` | Fruit-fly connectome navigator (train / evaluate / run) |
| `readout.npz` | Trained readout weights (the connectome itself is downloaded by `flybrain`) |

## Run it

```bash
python3 synthetic_test.py                       # physics test, no keys needed
export OPENAQ_API_KEY=your-key && python3 data.py   # live OpenAQ Delhi data, saves stations.json
export DATA_GOV_IN_API_KEY=your-key                 # optional fresh CPCB readings
python3 synthetic_test.py stations.json          # recovery test on the real station layout
```

## Limitations

- Local sources within roughly 1–15 km only. Regional smoke (stubble burning 100+ km away) and chemically formed particles show up as background.
- OpenAQ reporting can be delayed or sparse; the analysis may have fewer than four usable stations and remain regional/unlocalised. All listed stations are retained; OpenAQ calls are paced below 60 per minute, so 500 stations can take about nine minutes. Set Lambda's timeout to 15 minutes.
- The data.gov.in resource describes its granularity as hourly and returns pollutant averages with station update times. The integration uses the reported interval average; CPCB readings are omitted when no API key is configured.
- A 30-minute poll skips inversion and leaves `results/latest.json` untouched when no observation timestamp or value has changed.
- The map reports the newest station timestamp in IST, counts live/offline monitors, and shows each monitor's last reading time. “Live” means a reading within 90 minutes.
- One city-centre wind reading for Delhi; terrain and buildings are ignored.
- Single-source model: two simultaneous fires blur into one estimate.
- Calm wind (< 1 m/s) or fewer than 2 stations seeing the plume: no guess is made.

## Future work

- AI-written alert and complaint draft (Strands Agents on Amazon Bedrock) once model quota is available on the account.
- Add low-cost sensor networks to densify coverage; per-station wind.
- Closed-loop training of the connectome readout so the brain, not the casting rule, does more of the navigation; then put it on a low-cost drone to confirm sources on the ground.

## Credits

- Air quality data: [OpenAQ](https://openaq.org) (government monitoring networks).
- CPCB air quality data: [Open Government Data Platform India](https://www.data.gov.in/catalog/real-time-air-quality-index), when a data.gov.in API key is configured.
- Weather: [Open-Meteo](https://open-meteo.com) (non-commercial use).
- Fruit-fly connectome: MaleCNS v1.0 by FlyEM (HHMI Janelia), University of Cambridge, MRC LMB and Google Research, CC BY 4.0; cite Berg et al. (2026), *Sexual dimorphism in the complete connectome of the Drosophila male central nervous system*, Cell. Simulator: [`flybrain`](https://github.com/alextitonis/fly.ai) (MIT), neuron model after Fly64 by Jessica Paquette.
- Map: [Leaflet](https://leafletjs.com), tiles © Esri (fallback © OpenStreetMap contributors).
- AI tools used: Claude (Anthropic) for planning and code.
