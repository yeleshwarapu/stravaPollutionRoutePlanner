"""
rcycle/config.py
Central configuration for R'Cycle route planner.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # ── Origin ──────────────────────────────────────────────
    # GrandMarc at University Village, 3549 Iowa Ave, Riverside CA 92507
    origin_lat: float = 33.9533
    origin_lon: float = -117.3961

    # ── Route generation ────────────────────────────────────
    # Target loop distances in miles
    target_distances_miles: list = field(default_factory=lambda: [5, 8, 12, 16])

    # How many candidate spokes to try per distance target
    # (evenly spaced bearings: 8 = every 45°)
    num_spokes: int = 8

    # Max candidates to score before picking top N
    max_candidates: int = 24

    # Number of top routes to return per distance target
    top_routes_per_distance: int = 3

    # OSMnx network type: 'bike' uses cycleway/road network
    network_type: str = "bike"

    # Buffer around route when sampling AQ points (degrees lat/lon)
    aq_sample_spacing_miles: float = 0.5

    # ── Scoring weights (must sum to 1.0) ───────────────────
    weight_pm25:   float = 0.20   # PM2.5 air quality
    weight_ozone:  float = 0.10   # ozone air quality
    weight_uv:     float = 0.20   # UV exposure
    weight_loop:   float = 0.15   # how "loopy" vs out-and-back
    weight_paved:  float = 0.10   # proportion paved/bike-friendly
    weight_shade:  float = 0.25   # tree cover / shade along route (elevated: shade directly offsets UV in sunny climates)

    # ── Air Quality thresholds (EPA μg/m³) ────────────
    pm25_good:        float = 12.0   # AQI 0–50
    pm25_moderate:    float = 35.4   # AQI 51–100
    pm25_unhealthy:   float = 55.4   # AQI 101–150
    pm25_very_bad:    float = 150.4  # AQI 151+

    ozone_good:       float = 106.0  # ~54 ppb, AQI 0–50
    ozone_moderate:   float = 137.0  # ~70 ppb, AQI 51–100
    ozone_unhealthy:  float = 167.0  # ~85 ppb, AQI 101–150
    ozone_very_bad:   float = 392.0  # ~200 ppb, AQI 151+

    # ── UV thresholds ────────────────────────────────────────
    uv_low:           float = 2.0
    uv_moderate:      float = 5.0
    uv_high:          float = 7.0
    uv_very_high:     float = 10.0

    # ── Strava export ────────────────────────────────────────
    strava_activities_csv: Optional[str] = None   # path to activities.csv

    # ── Output ───────────────────────────────────────────────
    output_dir: str = "output"
    open_browser: bool = True


# Singleton
DEFAULT = Config()