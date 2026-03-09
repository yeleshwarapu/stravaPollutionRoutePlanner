"""
rcycle/data/air_quality.py

Fetches hourly PM2.5 concentrations from the Open-Meteo Air Quality API.
No API key required.

API docs: https://open-meteo.com/en/docs/air-quality-api
"""

from __future__ import annotations
import datetime
import requests
from typing import Optional
import numpy as np


AQ_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"
TIMEOUT = 10


def _fetch_raw(lat: float, lon: float) -> dict:
    """Pull 24 h of hourly PM2.5 data from Open-Meteo."""
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "hourly": "pm2_5,us_aqi",
        "timezone": "auto",
        "forecast_days": 1,
    }
    resp = requests.get(AQ_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_current_pm25(lat: float, lon: float) -> float:
    """
    Return the current hour's PM2.5 reading (μg/m³) for a lat/lon.
    Falls back to daily mean if current hour is unavailable.
    """
    data = _fetch_raw(lat, lon)
    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    pm25s  = hourly.get("pm2_5", [])

    if not pm25s:
        return 10.0  # safe default

    # Find index closest to now
    now = datetime.datetime.now()
    best_idx = 0
    best_diff = float("inf")
    for i, t_str in enumerate(times):
        try:
            t = datetime.datetime.fromisoformat(t_str)
            diff = abs((t - now).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except ValueError:
            continue

    val = pm25s[best_idx]
    if val is None:
        # fallback: mean of non-null values
        valid = [v for v in pm25s if v is not None]
        return float(np.mean(valid)) if valid else 10.0
    return float(val)


def get_route_pm25(
    coords: list[tuple[float, float]],
    sample_every_n: int = 5,
) -> float:
    """
    Estimate average PM2.5 exposure along a route.

    Parameters
    ----------
    coords : list of (lat, lon) tuples sampled along the route
    sample_every_n : only query every Nth point to stay within rate limits

    Returns
    -------
    float : mean PM2.5 (μg/m³) across sampled points
    """
    if not coords:
        return 10.0

    sample = coords[::sample_every_n] if len(coords) > sample_every_n else coords
    readings: list[float] = []

    for lat, lon in sample:
        try:
            readings.append(get_current_pm25(lat, lon))
        except Exception:
            continue  # skip on network error

    return float(np.mean(readings)) if readings else 10.0


def pm25_to_aqi_category(pm25: float) -> tuple[str, str]:
    """
    Convert PM2.5 μg/m³ to EPA AQI category label and hex colour.
    Returns (label, hex_colour).
    """
    if pm25 <= 12.0:
        return "Good", "#00e400"
    elif pm25 <= 35.4:
        return "Moderate", "#ffff00"
    elif pm25 <= 55.4:
        return "Unhealthy for Sensitive Groups", "#ff7e00"
    elif pm25 <= 150.4:
        return "Unhealthy", "#ff0000"
    elif pm25 <= 250.4:
        return "Very Unhealthy", "#8f3f97"
    else:
        return "Hazardous", "#7e0023"


def normalise_pm25(pm25: float, ceiling: float = 55.4) -> float:
    """Return 0 (clean) → 1 (worst), clamped at ceiling."""
    return min(pm25 / ceiling, 1.0)
