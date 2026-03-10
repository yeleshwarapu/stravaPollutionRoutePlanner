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
# Calibrated against real cycling data:
#   Easy:   flat bike paths, gentle rail trails         ~0–250 ft/10mi
#   Medium: rolling hills, typical suburban riding      ~250–600 ft/10mi
#   Hard:   sustained climbs, hilly terrain             ~600–1200 ft/10mi
#   Any:    no filter
# Note: these are TOTAL GAIN normalised to 10 miles, not average grade.
# A 25mi route at 250ft/10mi = 625ft total gain — genuinely easy.
ELEVATION_THRESHOLDS = {
    "easy":   (0,    250),
    "medium": (200,  650),   # overlapping bands — allows graceful fallback
    "hard":   (550,  99999),
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
def fetch_elevation_gain_ft(G, path: list[int], sample_every: int = None) -> float:
    """
    Sample elevation along a route via Open-Topo-Data and compute total gain in feet.
    Targets ~60 samples per route (max 100 per Open-Topo-Data request).
    """
    if len(path) < 2:
        return 0.0
    # Aim for ~60 evenly-spaced samples; always include first and last node
    step = max(1, len(path) // 60)
    indices = list(range(0, len(path), step))
    if indices[-1] != len(path) - 1:
        indices.append(len(path) - 1)
    sampled = [path[i] for i in indices]
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

    import time as _time

    def log(msg: str, step: str = "", eta: int = 0):
        job["messages"].append(msg)
        if step:
            job["current_step"] = {"name": step, "eta": eta, "t": _time.time()}

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
        log("Fetching environmental conditions…", step="env", eta=4)
        uv_window = None
        try:
            from data.uv_data import uv_window_description, get_current_uv, uv_category
            from data.air_quality import get_current_pm25, pm25_to_aqi_category, get_current_ozone, ozone_to_aqi_category
            uv_now    = get_current_uv(lat, lon)
            uv_lab, _ = uv_category(uv_now)
            uv_desc   = uv_window_description(lat, lon)
            uv_window = uv_desc["best_window"]
            pm25_now  = get_current_pm25(lat, lon)
            aq_lab, _ = pm25_to_aqi_category(pm25_now)
            ozone_now    = get_current_ozone(lat, lon)
            ozone_lab, _ = ozone_to_aqi_category(ozone_now)
            log(f"  UV now     : {uv_now:.1f} ({uv_lab})")
            log(f"  Best window: {uv_desc['window_label']} (UV {uv_desc['window_uv']}, {uv_desc['window_category']})")
            log(f"  UV peak    : {uv_desc['peak_label']} — {uv_desc['peak_uv']} ({uv_desc['peak_category']})")
            log(f"  PM2.5 now  : {pm25_now:.1f} μg/m³ ({aq_lab})")
            log(f"  Ozone now  : {ozone_now:.1f} μg/m³ ({ozone_lab})")
            job["env"] = {
                "uv":             uv_now,
                "uv_label":       uv_lab,
                "uv_window":      uv_desc["window_label"],
                "uv_window_uv":   uv_desc["window_uv"],
                "uv_advice":      uv_desc["advice"],
                "uv_peak_label":  uv_desc["peak_label"],
                "uv_peak_uv":     uv_desc["peak_uv"],
                "uv_peak_cat":    uv_desc["peak_category"],
                "pm25":           pm25_now,
                "aq_label":       aq_lab,
                "ozone":          ozone_now,
                "ozone_label":    ozone_lab,
            }
        except Exception as e:
            log(f"  Environmental fetch failed: {e}")

        # 4. Road network (cached)
        # Radius = half the loop distance (the farthest the route ever goes) + 5% buffer.
        # Bucket to nearest 2mi so nearby distances share a cache entry instead of
        # re-downloading for every slider nudge.
        max_dist   = max(req.distances)
        radius_mi  = max_dist * 0.52          # 0.52 = half-loop + small buffer
        radius_bucket = round(radius_mi / 2) * 2   # round to nearest 2mi
        cache_key  = f"{round(lat,3)},{round(lon,3)},{req.network},{radius_bucket}"

        # Reuse any cached network that covers at least this radius
        cached_G = None
        for k, v in _graph_cache.items():
            if k.startswith(f"{round(lat,3)},{round(lon,3)},{req.network},"):
                try:
                    cached_r = int(k.split(",")[3])
                    if cached_r >= radius_bucket:
                        cached_G = v
                        break
                except (IndexError, ValueError):
                    pass

        if cached_G is not None:
            log(f"Loading cached road network…", step="network", eta=2)
            G = cached_G
        else:
            _net_eta = max(10, int(8 + radius_mi ** 1.4 * 0.4))
            log(f"Downloading {req.network} network within {radius_mi:.1f} mi…", step="network", eta=_net_eta)
            from routing.network import download_network
            G = download_network(lat, lon, radius_mi, req.network)
            _graph_cache[cache_key] = G
            log(f"  Network: {len(G.nodes):,} nodes, {len(G.edges):,} edges")

        from routing.network import nearest_node, node_coords, download_shade_features
        origin_node = nearest_node(G, lat, lon)
        origin_lat, origin_lon = node_coords(G, origin_node)

        # 4b. Shade / tree cover features
        shade_cache_key = f"shade_{round(lat,3)},{round(lon,3)},{radius_bucket}"
        if shade_cache_key in _graph_cache:
            shade_polys = _graph_cache[shade_cache_key]
        else:
            _shade_eta = max(5, int(4 + radius_mi ** 1.2 * 0.2))
            log("Fetching tree cover data…", step="shade", eta=_shade_eta)
            shade_polys = download_shade_features(lat, lon, radius_mi)
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

            # Each candidate needs ~1-2 API round trips; spokes scales linearly
            _score_eta = max(3, min(len(candidates), req.spokes) * 2)
            log(f"  Scoring routes…", step="scoring", eta=_score_eta)
            scored = score_all(
                candidates, cfg, origin_lat, origin_lon,
                max_candidates=cfg.max_candidates,
                G=G,
            )

            # Fetch elevation for every scored route so we can filter accurately
            # and always show the gain value on the card regardless of mode.
            if req.elevation != "any":
                log(f"  Fetching elevation data ({req.elevation} filter)…")
                for r in scored:
                    r._elevation_gain_ft = fetch_elevation_gain_ft(G, r.path)

                gains = [(r, r._elevation_gain_ft,
                          r._elevation_gain_ft / r.length_miles * 10 if r.length_miles else 0)
                         for r in scored]

                # Log what we actually found so bad filters are diagnosable
                gain_summary = ", ".join(
                    f"{g:.0f}ft/10mi" for _, _, g in sorted(gains, key=lambda x: x[2])
                )
                log(f"  Gains found: [{gain_summary}]")

                filtered = [r for r, _, g10 in gains
                            if elevation_matches(r._elevation_gain_ft, r.length_miles, req.elevation)]

                if not filtered:
                    # Graceful fallback cascade: hard→medium→easy→any
                    fallback_order = {"hard": ["medium", "easy"], "medium": ["easy"], "easy": []}
                    relaxed = None
                    for fallback in fallback_order.get(req.elevation, []):
                        relaxed_routes = [r for r, _, g10 in gains
                                          if elevation_matches(r._elevation_gain_ft, r.length_miles, fallback)]
                        if relaxed_routes:
                            relaxed = fallback
                            filtered = relaxed_routes
                            break
                    if not filtered:
                        filtered = scored  # last resort: return all
                        relaxed = "any"
                    log(f"  No {req.elevation} routes found — showing {relaxed} instead")
                else:
                    log(f"  {len(filtered)} route(s) match {req.elevation} elevation")

                # Re-sort filtered by score (elevation fetch doesn't change scores)
                scored = sorted(filtered, key=lambda r: r.score, reverse=True)
            else:
                for r in scored:
                    r._elevation_gain_ft = fetch_elevation_gain_ft(G, r.path)

            top = scored[:req.top]
            all_scored.extend(top)

        if not all_scored:
            fail("No routes generated. Try a different address or larger distances.")
            return

        # 6. Build map → capture HTML string
        log("Building map…", step="map", eta=2)

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
                "index":           i,
                "grade":           r.grade(),
                "miles":           round(r.length_miles, 1),
                "target_miles":    round(r.loop.target_miles),
                "direction":       _bearing_label(r.loop.bearing_deg),
                "pm25":            round(r.pm25, 1),
                "aqi_label":       r.aqi_label,
                "aqi_colour":      r.aqi_colour,
                "ozone":           round(r.ozone, 1),
                "ozone_label":     r.ozone_label,
                "ozone_colour":    r.ozone_colour,
                "uv":              round(r.uv, 1),
                "uv_label":        r.uv_label,
                "uv_colour":       r.uv_colour,
                "loop_pct":        round(r.loop.loop_ratio * 100),
                "paved_pct":       round(r.paved_frac * 100),
                "shade_pct":       round(r.shade_frac * 100),
                "score":           round(r.score * 100),
                "score_breakdown": {k: round(v * 100) for k, v in r.score_breakdown.items()},
                "elevation_ft":    round(getattr(r, "_elevation_gain_ft", 0) or 0),
                "gpx_id":          gpx_ids[i] if i < len(gpx_ids) else None,
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
        "status":       job["status"],
        "messages":     job["messages"],
        "result":       job["result"],
        "error":        job.get("error"),
        "current_step": job.get("current_step"),
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