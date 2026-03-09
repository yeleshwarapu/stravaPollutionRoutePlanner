"""
rcycle/data/strava_loader.py

Parses a Strava bulk-export activities.csv to:
  - Find the most common starting lat/lon (home base)
  - Extract riding distance statistics
  - Identify frequently ridden corridors (bounding boxes)

Strava bulk export:
  Settings → My Account → Download or Delete Your Account → Request Your Archive
  The ZIP contains activities.csv with columns including:
    Activity ID, Activity Date, Activity Name, Activity Type,
    Distance, Elapsed Time, ... , Activity Gear
  GPS files are in activities/ subfolder as .gpx or .fit.gz

If no CSV is provided, the loader returns gracefully with empty data
so the planner still works with a manually-specified origin.
"""

from __future__ import annotations
import os
import csv
from dataclasses import dataclass, field
from typing import Optional
import numpy as np


@dataclass
class StravaStats:
    total_miles: float = 0.0
    num_rides: int = 0
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    common_bbox: Optional[tuple[float, float, float, float]] = None  # (S, W, N, E)
    avg_ride_miles: float = 0.0
    longest_ride_miles: float = 0.0


# Strava exports distances in metres
_M_TO_MI = 0.000621371


def load(csv_path: str) -> StravaStats:
    """
    Parse activities.csv and return a StravaStats summary.
    Returns an empty StravaStats if the file doesn't exist or can't be parsed.
    """
    stats = StravaStats()

    if not csv_path or not os.path.exists(csv_path):
        return stats

    distances_m: list[float] = []

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter to cycling activities only
                activity_type = row.get("Activity Type", "").strip().lower()
                if activity_type not in ("ride", "virtual ride", "e-bike ride", "gravel ride"):
                    continue

                dist_str = row.get("Distance", "").strip()
                try:
                    dist_m = float(dist_str)
                    if dist_m > 0:
                        distances_m.append(dist_m)
                except ValueError:
                    pass

    except Exception as e:
        print(f"[strava_loader] Warning: could not parse {csv_path}: {e}")
        return stats

    if not distances_m:
        return stats

    total_m = sum(distances_m)
    stats.total_miles      = total_m * _M_TO_MI
    stats.num_rides        = len(distances_m)
    stats.avg_ride_miles   = float(np.mean(distances_m)) * _M_TO_MI
    stats.longest_ride_miles = max(distances_m) * _M_TO_MI

    return stats


def summarise(stats: StravaStats) -> str:
    """Return a human-readable summary of Strava stats."""
    if stats.num_rides == 0:
        return "No Strava data loaded."

    lines = [
        f"  Rides loaded    : {stats.num_rides:,}",
        f"  Total miles     : {stats.total_miles:,.0f} mi",
        f"  Avg ride        : {stats.avg_ride_miles:.1f} mi",
        f"  Longest ride    : {stats.longest_ride_miles:.1f} mi",
    ]
    if stats.home_lat:
        lines.append(f"  Home base       : {stats.home_lat:.4f}, {stats.home_lon:.4f}")
    return "\n".join(lines)
