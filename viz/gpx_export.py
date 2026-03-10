"""
rcycle/viz/gpx_export.py

Exports the healthiest ScoredRoute for each distance bucket as a .gpx file.
Uses the full node path from the OSMnx graph for precise coordinates.
"""

from __future__ import annotations
import os
import datetime
import xml.etree.ElementTree as ET

from routing.scorer import ScoredRoute


def _route_to_gpx(route: ScoredRoute, G, name: str) -> ET.Element:
    """Build a <gpx> ElementTree element for one route, using edge geometry
    so the track follows actual road curves rather than straight node-to-node lines."""
    import pyproj

    gpx = ET.Element("gpx", {
        "version":   "1.1",
        "creator":   "R'Cycle Co-Op",
        "xmlns":     "http://www.topografix.com/GPX/1/1",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:schemaLocation": (
            "http://www.topografix.com/GPX/1/1 "
            "http://www.topografix.com/GPX/1/1/gpx.xsd"
        ),
    })

    # Metadata
    meta = ET.SubElement(gpx, "metadata")
    ET.SubElement(meta, "name").text = name
    ET.SubElement(meta, "desc").text = (
        f"Grade {route.grade()} · {route.length_miles:.1f} mi · "
        f"AQ: {route.aqi_label} ({route.pm25:.1f} μg/m³) · "
        f"UV: {route.uv_label} ({route.uv:.1f}) · "
        f"Score: {route.score:.0%}"
    )
    ET.SubElement(meta, "time").text = (
        datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Build CRS transformer once
    crs = G.graph.get("crs")
    transformer = None
    if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
        try:
            transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            pass

    def _proj_to_latlon(x, y):
        if transformer:
            lon, lat = transformer.transform(x, y)
            return float(lat), float(lon)
        return float(y), float(x)

    def _node_latlon(node_id):
        d = G.nodes[node_id]
        if "lat" in d and "lon" in d:
            return float(d["lat"]), float(d["lon"])
        return _proj_to_latlon(d["x"], d["y"])

    # Track
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = name
    ET.SubElement(trk, "type").text = "cycling"
    trkseg = ET.SubElement(trk, "trkseg")

    path = route.path
    if not path:
        return gpx

    def _add_trkpt(lat, lon, node_id=None):
        trkpt = ET.SubElement(trkseg, "trkpt", {
            "lat": f"{lat:.7f}",
            "lon": f"{lon:.7f}",
        })
        if node_id is not None:
            ele = G.nodes[node_id].get("elevation")
            if ele is not None:
                ET.SubElement(trkpt, "ele").text = f"{ele:.1f}"

    # Walk each edge, using geometry when available
    for u, v in zip(path[:-1], path[1:]):
        # Handle both edge directions — greedy walk may traverse edges either way
        if G.has_edge(u, v):
            edge_data = G[u][v]
        elif G.has_edge(v, u):
            edge_data = G[v][u]
        else:
            lat, lon = _node_latlon(u)
            _add_trkpt(lat, lon, u)
            continue
        best = min(edge_data.values(), key=lambda d: d.get("length", 1e9))
        geom = best.get("geometry")

        if geom is not None:
            pts = list(geom.coords)
            # Ensure geometry runs u→v
            u_data = G.nodes[u]
            ux, uy = u_data["x"], u_data["y"]
            if pts and abs(pts[0][0] - ux) > abs(pts[-1][0] - ux):
                pts = pts[::-1]
            for x, y in pts[:-1]:
                lat, lon = _proj_to_latlon(x, y)
                trkpt = ET.SubElement(trkseg, "trkpt", {
                    "lat": f"{lat:.7f}",
                    "lon": f"{lon:.7f}",
                })
        else:
            lat, lon = _node_latlon(u)
            _add_trkpt(lat, lon, u)

    # Final node
    lat, lon = _node_latlon(path[-1])
    _add_trkpt(lat, lon, path[-1])

    return gpx


def export_best_routes(
    all_scored: list[ScoredRoute],
    G,
    output_dir: str = "output",
    origin_lat: float = None,
    origin_lon: float = None,
) -> list[str]:
    """
    For each unique target distance, export the top-scoring route as a GPX.
    Returns a list of file paths written.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Group by target distance and take the best (already sorted best-first)
    seen_distances: set[float] = set()
    best_per_distance: list[ScoredRoute] = []
    for r in all_scored:
        d = round(r.loop.target_miles)
        if d not in seen_distances:
            seen_distances.add(d)
            best_per_distance.append(r)

    written = []
    for route in best_per_distance:
        name = _build_filename(route)
        filename = os.path.join(output_dir, f"{name}.gpx")

        gpx_el = _route_to_gpx(route, G, name)

        tree = ET.ElementTree(gpx_el)
        ET.indent(tree, space="  ")  # pretty-print (Python 3.9+)
        with open(filename, "wb") as f:
            tree.write(f, xml_declaration=True, encoding="utf-8")

        written.append(os.path.abspath(filename))

    return written


def export_all_routes(
    all_scored: list[ScoredRoute],
    G,
    output_dir: str = "output",
    location: str = "",
) -> list[str]:
    """
    Export every scored route as its own GPX file, one per route.
    Returns a list of file paths in the same order as all_scored.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Track used names to avoid collisions (e.g. two NE routes at same distance)
    used_names: dict[str, int] = {}
    written = []

    for i, route in enumerate(all_scored):
        base_name = _build_filename(route, location)

        # Deduplicate filenames if two routes share the same label
        if base_name in used_names:
            used_names[base_name] += 1
            name = f"{base_name}_{used_names[base_name]}"
        else:
            used_names[base_name] = 1
            name = base_name

        filename = os.path.join(output_dir, f"{name}.gpx")
        gpx_el = _route_to_gpx(route, G, name)
        tree = ET.ElementTree(gpx_el)
        ET.indent(tree, space="  ")
        with open(filename, "wb") as f:
            tree.write(f, xml_declaration=True, encoding="utf-8")

        written.append(os.path.abspath(filename))

    return written


def _bearing_to_compass(deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(deg / 45) % 8]


def _build_filename(route: ScoredRoute, location: str = "") -> str:
    """
    Build a camelCase filename using city/location and download timestamp.
    e.g. riversideCA_20260309_1432.gpx
    """
    import datetime
    import re

    # Parse city and state/country from location string
    # Nominatim returns e.g. "Riverside, Riverside County, California, United States"
    parts = [p.strip() for p in location.split(",") if p.strip()]

    city = parts[0] if parts else "unknown"
    # Try to get a short state/country suffix
    suffix = ""
    if len(parts) >= 3:
        # US: use 2-letter state abbreviation heuristic from full state name
        US_STATES = {
            "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR",
            "California":"CA","Colorado":"CO","Connecticut":"CT","Delaware":"DE",
            "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID",
            "Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS",
            "Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD",
            "Massachusetts":"MA","Michigan":"MI","Minnesota":"MN","Mississippi":"MS",
            "Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
            "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY",
            "North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK",
            "Oregon":"OR","Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC",
            "South Dakota":"SD","Tennessee":"TN","Texas":"TX","Utah":"UT",
            "Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV",
            "Wisconsin":"WI","Wyoming":"WY","District of Columbia":"DC",
        }
        for part in parts[1:]:
            abbr = US_STATES.get(part.strip())
            if abbr:
                suffix = abbr
                break
        if not suffix and parts[-1].strip() not in ("United States", "USA"):
            # Non-US: use first two letters of country
            suffix = re.sub(r'[^A-Za-z]', '', parts[-1])[:2].upper()

    # camelCase the city: strip non-alphanumeric, capitalise each word
    city_clean = re.sub(r'[^A-Za-z0-9 ]', '', city)
    city_words = city_clean.split()
    if city_words:
        city_camel = city_words[0].lower() + ''.join(w.capitalize() for w in city_words[1:])
    else:
        city_camel = "unknown"

    location_part = city_camel + suffix  # e.g. "riversideCA"

    now = datetime.datetime.now()
    dist      = round(route.loop.target_miles)
    direction = _bearing_to_compass(route.loop.bearing_deg)
    date_part = now.strftime("%Y_%m%d")  # 2026_0309
    time_part = now.strftime("%H%M")     # 1432

    return f"{location_part}_{dist}mi_{direction}_{date_part}_{time_part}"