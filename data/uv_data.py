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


def _fmt_hour(h: int) -> str:
    """Format hour 0-23 as human-friendly 12h string, e.g. 7 -> '7am', 14 -> '2pm'."""
    if h == 0:   return "12am"
    if h == 12:  return "12pm"
    if h < 12:   return f"{h}am"
    return f"{h - 12}pm"


def best_window_today(lat: float, lon: float) -> tuple[int, int]:
    """
    Return (start_hour, end_hour) of the lowest-UV 4-hour window today.
    """
    data   = _fetch_raw(lat, lon)
    times  = data["hourly"].get("time", [])
    uvs    = data["hourly"].get("uv_index", [])
    is_day = data["hourly"].get("is_day", [1] * len(times))

    windows = []
    for start in range(0, 20):
        block_uvs = []
        for offset in range(4):
            idx = start + offset
            if idx < len(uvs) and uvs[idx] is not None and is_day[idx]:
                block_uvs.append(uvs[idx])
        if block_uvs:
            windows.append((start, float(np.mean(block_uvs))))

    if not windows:
        return (7, 11)

    best = min(windows, key=lambda x: x[1])
    return (best[0], best[0] + 4)


def uv_window_description(lat: float, lon: float) -> dict:
    """
    Return a rich description of today's UV conditions including:
    - best_window: (start_hour, end_hour)
    - window_uv: average UV index during best window
    - peak_hour: hour with highest UV today
    - peak_uv: UV index at peak
    - window_label: e.g. "7am – 11am"
    - peak_label: e.g. "2pm"
    - advice: plain-English recommendation
    - window_category: UV label for the window
    - peak_category: UV label for the peak
    """
    data   = _fetch_raw(lat, lon)
    times  = data["hourly"].get("time", [])
    uvs    = data["hourly"].get("uv_index", [])
    is_day = data["hourly"].get("is_day", [1] * len(times))

    # Find best 4h window
    windows = []
    for start in range(0, 20):
        block_uvs = []
        for offset in range(4):
            idx = start + offset
            if idx < len(uvs) and uvs[idx] is not None and is_day[idx]:
                block_uvs.append(uvs[idx])
        if block_uvs:
            windows.append((start, float(np.mean(block_uvs))))

    if not windows:
        start_h, window_uv = 7, 2.0
    else:
        best = min(windows, key=lambda x: x[1])
        start_h, window_uv = best

    end_h = int(start_h) + 4

    # Find peak UV hour (daytime only)
    peak_uv, peak_hour = 0.0, 12
    for i, (uv, day) in enumerate(zip(uvs, is_day)):
        if day and uv is not None and uv > peak_uv:
            peak_uv = float(uv)
            # derive hour from index (hourly data starts at hour 0)
            try:
                peak_hour = datetime.datetime.fromisoformat(times[i]).hour
            except Exception:
                peak_hour = i % 24

    win_cat, _ = uv_category(window_uv)
    peak_cat, _ = uv_category(peak_uv)

    # Build plain-English advice
    window_str = f"{_fmt_hour(int(start_h))} – {_fmt_hour(end_h)}"
    peak_str   = _fmt_hour(peak_hour)

    if window_uv < 3:
        advice = f"Safe to ride anytime — UV stays Low. Best window {window_str}."
    elif window_uv < 6:
        advice = f"Ride {window_str} for Moderate UV. Peak {peak_uv:.0f} ({peak_cat}) around {peak_str} — sunscreen advised."
    elif window_uv < 8:
        advice = f"Ride early — best window {window_str} (UV {window_uv:.0f}). Avoid {peak_str} when UV hits {peak_uv:.0f} ({peak_cat})."
    else:
        advice = f"High UV day. Safest window {window_str} (UV {window_uv:.0f}). Peak {peak_uv:.0f} ({peak_cat}) around {peak_str} — cover up."

    return {
        "best_window":      (int(start_h), end_h),
        "window_uv":        round(window_uv, 1),
        "peak_hour":        peak_hour,
        "peak_uv":          round(peak_uv, 1),
        "window_label":     window_str,
        "peak_label":       peak_str,
        "window_category":  win_cat,
        "peak_category":    peak_cat,
        "advice":           advice,
    }


def normalise_uv(uv: float, ceiling: float = 11.0) -> float:
    """Return 0 (no UV) → 1 (extreme), clamped at ceiling."""
    return min(uv / ceiling, 1.0)