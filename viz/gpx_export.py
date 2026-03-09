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
    """Build a <gpx> ElementTree element for one route."""
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

    # Track
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = name
    ET.SubElement(trk, "type").text = "cycling"
    trkseg = ET.SubElement(trk, "trkseg")

    # Walk every node in the full path — use lat/lon saved by download_network
    for node_id in route.path:
        node = G.nodes[node_id]
        lat = node.get("lat")   # set by download_network before UTM projection
        lon = node.get("lon")
        if lat is None or lon is None:
            continue
        trkpt = ET.SubElement(trkseg, "trkpt", {
            "lat": f"{lat:.7f}",
            "lon": f"{lon:.7f}",
        })
        ele = node.get("elevation")
        if ele is not None:
            ET.SubElement(trkpt, "ele").text = f"{ele:.1f}"

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
        dist = round(route.loop.target_miles)
        direction = _bearing_to_compass(route.loop.bearing_deg)
        name = f"rcycle_{dist}mi_{direction}_grade{route.grade()}"
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
        dist = round(route.loop.target_miles)
        direction = _bearing_to_compass(route.loop.bearing_deg)
        base_name = f"rcycle_{dist}mi_{direction}_grade{route.grade()}"

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