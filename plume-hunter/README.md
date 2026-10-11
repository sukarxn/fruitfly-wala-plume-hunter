# Plume Hunter

**Delhi can measure how bad its air is, but not who is making it bad.**
Plume Hunter turns a pollution spike at monitoring stations into a map of where the smoke most likely came from, using wind physics.

Track: **Air** · Environmental Hacks (Bharat Builds Tour, WeMakeDevs × AWS)

## How it works

1. **Data.** Every hour, the Lambda pulls hourly PM2.5 from Delhi's monitoring stations (OpenAQ API v3) and hourly wind, cloud and sunshine (Open-Meteo).
2. **Background removed.** The 20th-percentile station is treated as regional haze (stubble smoke, secondary particles); only the excess above it is traced.
3. **Physics.** Every 1 km cell in Delhi is tested as a ground-level source with the Gaussian plume model:

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

## AWS architecture

| Service | Role |
| --- | --- |
| AWS Lambda | Runs the fetch + physics every hour, and serves the map page through a Function URL |
| Amazon EventBridge | Hourly schedule |
| Amazon S3 | Stores every result as JSON (`results/latest.json` + hourly history) |
| Amazon SNS | Email alert when a local source is found |
| AWS Systems Manager Parameter Store (optional) | Holds the OpenAQ API key |

No third-party packages: the physics is pure Python, so the Lambda is one small zip.

## Files

| File | What it is |
| --- | --- |
| `plume.py` | Physics: coordinates, stability, Briggs sigmas, plume model, inversion, verdict |
| `data.py` | OpenAQ + Open-Meteo fetchers; run directly to test with live data |
| `lambda_function.py` | Lambda handler (schedule + web page + SNS) |
| `page.html` | Leaflet map page |
| `synthetic_test.py` | Recovery test |

## Run it

```bash
python3 synthetic_test.py                       # physics test, no keys needed
export OPENAQ_API_KEY=your-key && python3 data.py   # live Delhi data, saves stations.json
python3 synthetic_test.py stations.json          # recovery test on the real station layout
```

## Limitations

- Local sources within roughly 1–15 km only. Regional smoke (stubble burning 100+ km away) and chemically formed particles show up as background.
- One city-centre wind reading for all of Delhi; terrain and buildings are ignored.
- Single-source model: two simultaneous fires blur into one estimate.
- Calm wind (< 1 m/s) or fewer than 2 stations seeing the plume: no guess is made.

## Future work

- AI-written alert and complaint draft (Strands Agents on Amazon Bedrock) once model quota is available on the account.
- Add low-cost sensor networks to densify coverage; per-station wind.
- A fruit-fly-inspired surge-and-cast navigator (after Drosophila plume tracking) for a low-cost drone that confirms the source on the ground.

## Credits

- Air quality data: [OpenAQ](https://openaq.org) (government monitoring networks).
- Weather: [Open-Meteo](https://open-meteo.com) (non-commercial use).
- Map: [Leaflet](https://leafletjs.com), tiles © OpenStreetMap contributors, © CARTO.
- AI tools used: Claude (Anthropic) for planning and code.
