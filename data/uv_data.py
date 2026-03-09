"""
rcycle/data/uv_data.py

Fetches hourly UV index from the Open-Meteo Forecast API.
No API key required.

API docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations
import datetime
import requests
import numpy as np
from typing import Optional


FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 10


def _fetch_raw(lat: float, lon: float) -> dict:
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "hourly": "uv_index,is_day",
        "timezone": "auto",
        "forecast_days": 1,
    }
    resp = requests.get(FORECAST_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _location_now(data: dict) -> datetime.datetime:
    """
    Return the current time at the target location using the
    utc_offset_seconds field Open-Meteo returns alongside timezone=auto.
    This avoids relying on the machine's local timezone.
    """
    utc_offset = data.get("utc_offset_seconds", 0)
    return datetime.datetime.utcnow() + datetime.timedelta(seconds=utc_offset)


def get_current_uv(lat: float, lon: float) -> float:
    """Return current hour's UV index for a lat/lon."""
    data  = _fetch_raw(lat, lon)
    times = data["hourly"].get("time", [])
    uvs   = data["hourly"].get("uv_index", [])

    if not uvs:
        return 3.0  # moderate default

    # Use location's local time, not machine's local time
    now = _location_now(data)

    best_idx, best_diff = 0, float("inf")
    for i, t_str in enumerate(times):
        try:
            t = datetime.datetime.fromisoformat(t_str)
            diff = abs((t - now).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except ValueError:
            continue

    val = uvs[best_idx]
    if val is None:
        valid = [v for v in uvs if v is not None]
        return float(np.mean(valid)) if valid else 3.0
    return float(val)


def get_uv_for_hour(lat: float, lon: float, hour: int) -> float:
    """
    Fetch UV index for a specific hour of the day (0-23) in the
    target location's local time.
    Useful for planning rides at different times.
    """
    data  = _fetch_raw(lat, lon)
    times = data["hourly"].get("time", [])
    uvs   = data["hourly"].get("uv_index", [])

    for i, t_str in enumerate(times):
        try:
            t = datetime.datetime.fromisoformat(t_str)
            if t.hour == hour:
                val = uvs[i]
                return float(val) if val is not None else 3.0
        except (ValueError, IndexError):
            continue
    return 3.0


def uv_category(uv: float) -> tuple[str, str]:
    """Return (label, hex_colour) for a UV index value."""
    if uv < 3:
        return "Low", "#299501"
    elif uv < 6:
        return "Moderate", "#f7e401"
    elif uv < 8:
        return "High", "#f95901"
    elif uv < 11:
        return "Very High", "#d90011"
    else:
        return "Extreme", "#6b49c8"


def best_window_today(lat: float, lon: float) -> tuple[int, int]:
    """
    Return (start_hour, end_hour) of the lowest-UV window in a
    4-hour block today (good for planning when to ride).
    """
    data   = _fetch_raw(lat, lon)
    times  = data["hourly"].get("time", [])
    uvs    = data["hourly"].get("uv_index", [])
    is_day = data["hourly"].get("is_day", [1] * len(times))

    windows = []
    for start in range(0, 20):  # windows from hour 0 to hour 20
        block_uvs = []
        for offset in range(4):
            idx = start + offset
            if idx < len(uvs) and uvs[idx] is not None and is_day[idx]:
                block_uvs.append(uvs[idx])
        if block_uvs:
            windows.append((start, float(np.mean(block_uvs))))

    if not windows:
        return (7, 11)  # sensible default: early morning

    best = min(windows, key=lambda x: x[1])
    return (best[0], best[0] + 4)


def normalise_uv(uv: float, ceiling: float = 11.0) -> float:
    """Return 0 (no UV) → 1 (extreme), clamped at ceiling."""
    return min(uv / ceiling, 1.0)