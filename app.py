"""
rcycle/app.py
=============
FastAPI web server for the R'Cycle route planner.

Run with:
    pip install fastapi uvicorn
    uvicorn app:app --reload --port 8000
Then open http://localhost:8000
"""

from __future__ import annotations
import os
import sys
import uuid
import json
import tempfile
import threading
import time
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import requests as _requests

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="R'Cycle Co-Op")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ── In-memory state ───────────────────────────────────────────────────────────
_jobs: Dict[str, Dict[str, Any]] = {}   # job_id → job dict
_graph_cache: Dict[str, Any] = {}       # cache key → G

# ── Elevation thresholds (ft gain per 10 miles, scaled linearly) ──────────────
ELEVATION_THRESHOLDS = {
    "easy":   (0,    400),
    "medium": (400,  900),
    "hard":   (900,  99999),
    "any":    (0,    99999),
}

# ── Request model ─────────────────────────────────────────────────────────────
class PlanRequest(BaseModel):
    address:    str
    distances:  list[float] = [5, 8, 12]
    elevation:  str         = "any"          # easy | medium | hard | any
    top:        int         = 3
    spokes:     int         = 8
    network:    str         = "bike"


# ── Geocoding ─────────────────────────────────────────────────────────────────
def geocode_address(address: str) -> tuple[float, float, str]:
    """Return (lat, lon, display_name) via Nominatim."""
    resp = _requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": "RCycleCoOp/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Address not found: {address!r}")
    r = results[0]
    return float(r["lat"]), float(r["lon"]), r["display_name"]


# ── Elevation ─────────────────────────────────────────────────────────────────
def fetch_elevation_gain_ft(G, path: list[int], sample_every: int = 15) -> float:
    """
    Sample elevation along a route via Open-Topo-Data and compute total gain in feet.
    Samples every Nth node to limit API calls (max 100 locations per request).
    """
    sampled = path[::sample_every]
    if len(sampled) < 2:
        return 0.0

    # Build location string: "lat,lon|lat,lon|..."
    locs = []
    for node_id in sampled:
        node = G.nodes[node_id]
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is not None and lon is not None:
            locs.append(f"{lat:.6f},{lon:.6f}")

    if len(locs) < 2:
        return 0.0

    try:
        resp = _requests.get(
            "https://api.opentopodata.org/v1/aster30m",
            params={"locations": "|".join(locs)},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        elevations = [r.get("elevation") for r in data.get("results", [])]
        elevations = [e for e in elevations if e is not None]
    except Exception:
        return 0.0   # if elevation API fails, don't crash

    if len(elevations) < 2:
        return 0.0

    # Sum positive elevation changes (total gain)
    gain_m = sum(
        max(0, elevations[i+1] - elevations[i])
        for i in range(len(elevations) - 1)
    )
    return gain_m * 3.28084   # metres → feet


def elevation_matches(gain_ft: float, length_miles: float, difficulty: str) -> bool:
    """Check if a route's elevation gain matches the requested difficulty."""
    if difficulty == "any":
        return True
    lo, hi = ELEVATION_THRESHOLDS.get(difficulty, (0, 99999))
    # Normalise gain to per-10-miles so short and long routes are comparable
    gain_per_10mi = (gain_ft / length_miles * 10) if length_miles > 0 else 0
    return lo <= gain_per_10mi < hi


# ── Background planning job ───────────────────────────────────────────────────
def _run_plan(job_id: str, req: PlanRequest):
    job = _jobs[job_id]

    def log(msg: str):
        job["messages"].append(msg)

    def fail(msg: str):
        job["status"] = "error"
        job["error"]  = msg
        log(f"ERROR: {msg}")

    try:
        # 1. Geocode
        log(f"Geocoding address…")
        lat, lon, display = geocode_address(req.address)
        log(f"  Found: {display}")
        log(f"  Coordinates: {lat:.4f}, {lon:.4f}")

        # 2. Config
        from config import Config
        cfg = Config(
            origin_lat=lat,
            origin_lon=lon,
            target_distances_miles=req.distances,
            num_spokes=req.spokes,
            top_routes_per_distance=req.top,
            network_type=req.network,
        )

        # 3. Environmental conditions
        log("Fetching environmental conditions…")
        uv_window = None
        try:
            from data.uv_data import best_window_today, get_current_uv, uv_category
            from data.air_quality import get_current_pm25, pm25_to_aqi_category
            uv_now    = get_current_uv(lat, lon)
            uv_lab, _ = uv_category(uv_now)
            uv_window = best_window_today(lat, lon)
            pm25_now  = get_current_pm25(lat, lon)
            aq_lab, _ = pm25_to_aqi_category(pm25_now)
            log(f"  UV now     : {uv_now:.1f} ({uv_lab})")
            log(f"  Best window: {uv_window[0]:02d}:00–{uv_window[1]:02d}:00")
            log(f"  PM2.5 now  : {pm25_now:.1f} μg/m³ ({aq_lab})")
            job["env"] = {
                "uv":        uv_now,
                "uv_label":  uv_lab,
                "uv_window": f"{uv_window[0]:02d}:00–{uv_window[1]:02d}:00",
                "pm25":      pm25_now,
                "aq_label":  aq_lab,
            }
        except Exception as e:
            log(f"  Environmental fetch failed: {e}")

        # 4. Road network (cached)
        cache_key = f"{round(lat,3)},{round(lon,3)},{req.network}"
        if cache_key in _graph_cache:
            log("Loading cached road network…")
            G = _graph_cache[cache_key]
        else:
            max_dist = max(req.distances)
            log(f"Downloading {req.network} network within {max_dist * 0.6:.1f} mi…")
            from routing.network import download_network
            G = download_network(lat, lon, max_dist * 0.6, req.network)
            _graph_cache[cache_key] = G
            log(f"  Network: {len(G.nodes):,} nodes, {len(G.edges):,} edges")

        from routing.network import nearest_node, node_coords, download_shade_features
        origin_node = nearest_node(G, lat, lon)
        origin_lat, origin_lon = node_coords(G, origin_node)

        # 4b. Shade / tree cover features
        shade_cache_key = f"shade_{round(lat,3)},{round(lon,3)}"
        if shade_cache_key in _graph_cache:
            shade_polys = _graph_cache[shade_cache_key]
        else:
            max_dist = max(req.distances)
            log("Fetching tree cover data…")
            shade_polys = download_shade_features(lat, lon, max_dist * 0.6)
            _graph_cache[shade_cache_key] = shade_polys
            log(f"  Found {len(shade_polys)} shade features")

        # 5. Generate + score routes
        from routing.loops import generate_candidates
        from routing.scorer import score_all, ScoredRoute

        all_scored: list[ScoredRoute] = []

        for dist in sorted(req.distances):
            log(f"Generating {dist:.0f}-mile loops…")
            candidates = generate_candidates(G, origin_node, dist, num_spokes=req.spokes, shade_polys=shade_polys)
            log(f"  Found {len(candidates)} candidate loops")
            if not candidates:
                continue

            log(f"  Scoring routes…")
            scored = score_all(
                candidates, cfg, origin_lat, origin_lon,
                max_candidates=cfg.max_candidates,
                G=G,
            )

            # Filter by elevation difficulty
            if req.elevation != "any":
                log(f"  Fetching elevation data ({req.elevation} filter)…")
                filtered = []
                for r in scored:
                    gain = fetch_elevation_gain_ft(G, r.path)
                    r._elevation_gain_ft = gain   # stash on object
                    if elevation_matches(gain, r.length_miles, req.elevation):
                        filtered.append(r)
                if not filtered:
                    log(f"  No {req.elevation} routes found for {dist:.0f} mi — relaxing filter")
                    filtered = scored   # fall back to unfiltered
                scored = filtered
            else:
                for r in scored:
                    r._elevation_gain_ft = None

            top = scored[:req.top]
            all_scored.extend(top)

        if not all_scored:
            fail("No routes generated. Try a different address or larger distances.")
            return

        # 6. Build map → capture HTML string
        log("Building map…")

        # Convert shade polys to GeoJSON for the map.
        # Polys are already simplified in download_shade_features so vertex
        # counts are low. Cap the browser overlay at 500 — enough to show all
        # neighbourhood parks without overwhelming Leaflet.
        shade_geojson = []
        try:
            from shapely.geometry import mapping
            for poly in shade_polys[:500]:
                try:
                    shade_geojson.append(mapping(poly))
                except Exception:
                    pass
        except Exception:
            pass

        from viz.mapper import build_map
        tmp_dir  = tempfile.mkdtemp()
        map_path = os.path.join(tmp_dir, "routes.html")
        build_map(
            scored_routes=all_scored,
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            G=G,
            output_path=map_path,
            uv_window=uv_window,
            shade_geojson=shade_geojson,
        )
        with open(map_path, "r", encoding="utf-8") as f:
            map_html = f.read()

        # 7. Export GPX files — one per route
        log("Exporting GPX files…")
        from viz.gpx_export import export_all_routes
        gpx_files = export_all_routes(
            all_scored=all_scored,
            G=G,
            output_dir=tmp_dir,
            location=display,
        )
        # Store every GPX path keyed by a unique ID, in the same order as all_scored
        gpx_ids = []
        for gf in gpx_files:
            gpx_id = str(uuid.uuid4())
            _jobs[f"gpx_{gpx_id}"] = gf
            gpx_ids.append(gpx_id)

        # 8. Serialise route cards
        route_cards = []
        for i, r in enumerate(all_scored):
            card = {
                "index":         i,
                "grade":         r.grade(),
                "miles":         round(r.length_miles, 1),
                "target_miles":  round(r.loop.target_miles),
                "direction":     _bearing_label(r.loop.bearing_deg),
                "pm25":          round(r.pm25, 1),
                "aqi_label":     r.aqi_label,
                "aqi_colour":    r.aqi_colour,
                "uv":            round(r.uv, 1),
                "uv_label":      r.uv_label,
                "uv_colour":     r.uv_colour,
                "loop_pct":      round(r.loop.loop_ratio * 100),
                "paved_pct":     round(r.paved_frac * 100),
                "shade_pct":     round(r.shade_frac * 100),
                "score":         round(r.score * 100),
                "elevation_ft":  round(getattr(r, "_elevation_gain_ft", 0) or 0),
                "gpx_id":        gpx_ids[i] if i < len(gpx_ids) else None,
            }
            route_cards.append(card)

        log("Done.")
        job["status"]  = "done"
        job["result"]  = {
            "map_html":    map_html,
            "routes":      route_cards,
            "env":         job.get("env", {}),
            "address":     display,
            "lat":         lat,
            "lon":         lon,
        }

    except Exception as e:
        import traceback
        fail(str(e))
        job["traceback"] = traceback.format_exc()


def _bearing_label(deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(deg / 45) % 8]


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/plan")
async def start_plan(req: PlanRequest):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status":   "running",
        "messages": [],
        "env":      {},
        "result":   None,
        "error":    None,
    }
    t = threading.Thread(target=_run_plan, args=(job_id, req), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {
        "status":   job["status"],
        "messages": job["messages"],
        "result":   job["result"],
        "error":    job.get("error"),
    }


@app.get("/api/gpx/{gpx_id}")
async def download_gpx(gpx_id: str):
    path = _jobs.get(f"gpx_{gpx_id}")
    if not path or not os.path.exists(path):
        return JSONResponse({"error": "GPX not found"}, status_code=404)
    return FileResponse(
        path,
        media_type="application/gpx+xml",
        filename=os.path.basename(path),
    )


@app.get("/api/geocode")
async def geocode(q: str):
    try:
        lat, lon, display = geocode_address(q)
        return {"lat": lat, "lon": lon, "display": display}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)