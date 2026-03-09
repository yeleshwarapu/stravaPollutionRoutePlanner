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
    origin_lat: float = 28.6139
    origin_lon: float = 77.2090

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
    weight_pm25:   float = 0.45   # air quality — highest priority
    weight_uv:     float = 0.30   # UV exposure
    weight_loop:   float = 0.15   # how "loopy" vs out-and-back
    weight_paved:  float = 0.10   # proportion paved/bike-friendly

    # ── Air Quality thresholds (EPA PM2.5 μg/m³) ────────────
    pm25_good:        float = 12.0   # AQI 0–50
    pm25_moderate:    float = 35.4   # AQI 51–100
    pm25_unhealthy:   float = 55.4   # AQI 101–150
    pm25_very_bad:    float = 150.4  # AQI 151+

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
