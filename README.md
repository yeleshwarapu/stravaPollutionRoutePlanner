# R'Cycle — Health-Aware Cycling Route Planner

> Built from ~5,000 miles of Strava data, riding through Riverside's industrial corridors.

Generates cycling loop routes ranked by **air quality (PM2.5)** and **UV exposure** rather than speed or distance. Standard routing tools ignore the health tradeoffs of where you ride — this one doesn't.

---

## What it does

1. **Downloads the cycling road network** around your origin using OpenStreetMap (via OSMnx)
2. **Generates candidate loop routes** using a spoke-and-return algorithm across 8 directional bearings
3. **Fetches live environmental data** (no API keys required):
   - PM2.5 via [Open-Meteo Air Quality API](https://open-meteo.com/en/docs/air-quality-api)
   - UV index via [Open-Meteo Forecast API](https://open-meteo.com/en/docs)
4. **Scores and ranks routes** on a weighted health + quality metric
5. **Outputs an interactive HTML map** with colour-coded routes and popups

---

## Scoring

| Factor | Weight | Source |
|--------|--------|--------|
| PM2.5 air quality | 45% | Open-Meteo AQ API (sampled along route) |
| UV index | 30% | Open-Meteo Forecast API |
| Loop shape | 15% | % unique nodes (penalises out-and-back) |
| Paved surface | 10% | OSM `highway` tag classification |

Grades: **A** (≥85%) → **F** (<40%)

---

## Install

```bash
# Clone or copy this directory, then:
pip install -r requirements.txt
```

Requires Python 3.10+. Tested on macOS and Linux.

---

## Usage

```bash
# Default: 5/8/12-mile loops from UCR, Riverside CA
python main.py

# Custom origin (e.g., downtown Riverside)
python main.py --lat 33.9806 --lon -117.3755

# Different distances
python main.py --distances 5 10 20

# Include your Strava export for ride stats context
python main.py --strava ~/Downloads/strava_export/activities.csv

# Plan for a specific hour (e.g., 6am for UV)
python main.py --hour 6

# Save to a different file, don't auto-open browser
python main.py --output ~/Desktop/my_routes.html --no-browser

# All options
python main.py --help
```

---

## Strava Export

To load your own ride data:

1. Go to Strava → Settings → My Account → Download or Delete Your Account
2. Request your archive and download the ZIP
3. Unzip — you'll find `activities.csv` inside
4. Pass it with `--strava /path/to/activities.csv`

The planner uses this for ride statistics context. Route generation works without it.

---

## Project Structure

```
rcycle/
├── main.py                  CLI entry point
├── config.py                Scoring weights and API settings
├── requirements.txt
├── data/
│   ├── air_quality.py       PM2.5 from Open-Meteo (no key)
│   ├── uv_data.py           UV index from Open-Meteo (no key)
│   └── strava_loader.py     Parse Strava bulk export CSV
├── routing/
│   ├── network.py           OSMnx road network wrapper
│   ├── loops.py             Loop route generation algorithm
│   └── scorer.py            Health-aware route scoring
└── viz/
    └── mapper.py            Folium interactive HTML map
```

---

## Tuning

Edit `config.py` to adjust scoring weights:

```python
weight_pm25  = 0.45   # air quality — increase if AQ is your primary concern
weight_uv    = 0.30   # UV — increase for high-sun areas
weight_loop  = 0.15   # shape quality
weight_paved = 0.10   # surface quality
```

---

## Roadmap

- [ ] Real-time pollution overlay (raster heatmap on the map)
- [ ] Shade coverage scoring using tree canopy data
- [ ] Historical Strava segment analysis to find personally-validated low-AQ corridors  
- [ ] Time-of-day optimiser (suggests earliest window with good AQ + low UV)
- [ ] Export to GPX for Garmin / Wahoo head units

---

*Built with OSMnx · Open-Meteo · Folium · NetworkX*
"# stravaPollutionRoutePlanner" 
