---
title: R'Cycle Co-Op
emoji: 🚴
colorFrom: yellow
colorTo: green
sdk: docker
pinned: false
short_description: Cycling routes ranked by air quality, UV & shade
---

# R'Cycle — Health-Aware Cycling Route Planner

> Built from ~5,000 miles of Strava data, riding through Riverside's industrial corridors.

Generates cycling loop routes ranked by **air quality (PM2.5 & ozone)**, **UV exposure**, **tree cover**, and **surface quality** rather than speed or distance. Standard routing tools ignore the health tradeoffs of where you ride — this one doesn't.

---

## What it does

1. **Downloads the cycling road network** around your origin using OpenStreetMap (via OSMnx)
2. **Generates candidate loop routes** using a spoke-and-return algorithm across configurable directional bearings
3. **Fetches live environmental data** (no API keys required):
   - PM2.5 via [Open-Meteo Air Quality API](https://open-meteo.com/en/docs/air-quality-api)
   - UV index via [Open-Meteo Forecast API](https://open-meteo.com/en/docs)
4. **Fetches tree cover and shade features** from OSM (forests, parks, tree rows, tunnels)
5. **Scores and ranks routes** on a weighted health + quality metric
6. **Outputs an interactive HTML map** with colour-coded routes, click-to-focus, and animated route drawing
7. **Exports GPX files** per route with descriptive filenames for direct import into Strava

---

## Scoring

| Factor | Weight | Source |
|--------|--------|--------|
| PM2.5 air quality | 20% | Open-Meteo AQ API (sampled at 25% point along route) |
| Ozone air quality | 10% | Open-Meteo AQ API (sampled at 25% point along route) |
| UV index | 20% | Open-Meteo Forecast API |
| Tree cover / shade | 25% | OSM natural=wood, landuse=forest, leisure=park, tree rows |
| Loop shape | 15% | % unique nodes (penalises out-and-back routes) |
| Paved surface | 10% | OSM `surface` tag + `highway` type classification |

Grades: **A** (≥85%) · **B** (≥70%) · **C** (≥55%) · **D** (≥40%) · **F** (<40%)

Routing strongly avoids unpaved roads (8× length penalty on dirt/gravel/tracks) so the paved % shown is accurate.

---

## Web App

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Then open [http://localhost:8000](http://localhost:8000)

Features:
- **Address autocomplete** — type any address and select from live Nominatim suggestions
- **Distance slider** — drag from 5–100 miles
- **Elevation filter** — Easy / Medium / Hard / Any
- **Route cards** — click any card to highlight and animate that route on the map
- **GPX export** — downloads the file and opens Strava's route builder simultaneously
- **Spokes setting** — number of directional bearings explored (more = more variety, slower)

---

## Screenshots

### Web Interface Overview
![Web Interface](screenshots/web_interface.png)
*The main web interface showing the address input, distance slider, and route cards with health scores.*

### Interactive Map with Routes
![Interactive Map](screenshots/interactive_map.png)
*Color-coded routes on the map. Green routes indicate healthier options, red routes show areas with higher pollution or UV exposure.*

### Route Details Card
![Route Details](screenshots/route_details.png)
*Detailed breakdown of route health factors including PM2.5, ozone, UV index, and surface quality.*

### GPX Export in Strava
![GPX Export](screenshots/gpx_export.png)
*Exported GPX file opened in Strava's route builder, ready for navigation.*

**To add screenshots:** Place PNG/JPG images in the `screenshots/` directory and update the image paths above.

---

## GPX Filenames

Exported files are named by location, distance, direction, date, and time:

```
riversideCA_25mi_NE_2026_0309_1432.gpx
riversideCA_25mi_SW_2026_0309_1432.gpx
```

---

## Install

```bash
git clone https://github.com/yeleshwarapu/stravaPollutionRoutePlanner.git
cd stravaPollutionRoutePlanner
pip install -r requirements.txt
```

Requires Python 3.10+. Tested on macOS, Linux, and Windows.

---

## CLI Usage

```bash
# Default origin
python main.py

# Custom origin
python main.py --lat 33.9806 --lon -117.3755

# Different distances
python main.py --distances 5 10 20

# Include your Strava export
python main.py --strava ~/Downloads/strava_export/activities.csv

# All options
python main.py --help
```

---

## Project Structure

```
rcycle/
├── app.py                   FastAPI web server
├── main.py                  CLI entry point
├── config.py                Scoring weights and thresholds
├── requirements.txt
├── README.md
├── screenshots/             Screenshots for documentation
├── data/
│   ├── air_quality.py       PM2.5 & ozone from Open-Meteo (no key required)
│   ├── uv_data.py           UV index + best riding window
│   └── strava_loader.py     Parse Strava bulk export CSV
├── routing/
│   ├── network.py           OSMnx wrapper, paved/shade detection
│   ├── loops.py             Spoke-and-return loop generation
│   └── scorer.py            Health-aware route scoring
├── templates/
│   └── index.html           Web UI
└── viz/
    ├── mapper.py            Leaflet interactive HTML map
    └── gpx_export.py        GPX export with road-accurate geometry
```

---

## Tuning

Edit `config.py` to adjust scoring weights (must sum to 1.0):

```python
weight_pm25  = 0.20   # PM2.5 air quality
weight_ozone = 0.10   # ozone air quality
weight_uv    = 0.20   # UV exposure
weight_shade = 0.25   # tree cover
weight_loop  = 0.15   # loop shape quality
weight_paved = 0.10   # surface quality
```

---

## Strava Export

To load your own ride data:

1. Strava → Settings → My Account → Download or Delete Your Account
2. Request your archive and download the ZIP
3. Unzip — find `activities.csv` inside
4. Pass it with `--strava /path/to/activities.csv`

Route generation works without it.

---

## Roadmap

- [x] Health-scored loop generation (PM2.5 + UV)
- [x] Ozone air quality scoring
- [x] Paved surface detection using OSM `surface` tag
- [x] Tree cover / shade scoring
- [x] Road-accurate GPX geometry (follows curves, not straight lines)
- [x] Web UI with autocomplete, distance slider, interactive map
- [x] Per-route GPX export with descriptive filenames
- [ ] Strava OAuth upload (activity import)
- [ ] Time-of-day optimiser (best window for AQ + UV + shade)
- [ ] Heatmap overlay showing pollution/UV/shade across the map
- [ ] Home base auto-detection from Strava GPS history

---

*Built with OSMnx · Open-Meteo · Leaflet · NetworkX · FastAPI*