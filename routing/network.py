"""
rcycle/routing/network.py

Thin wrapper around OSMnx for downloading and querying the cycling
road network around an origin point.

OSMnx docs: https://osmnx.readthedocs.io
"""

from __future__ import annotations
import math
import numpy as np
import networkx as nx
import osmnx as ox
from typing import Optional


# Configure osmnx once at import time
ox.settings.log_console = False
ox.settings.use_cache   = True          # cache network downloads to disk
ox.settings.cache_folder = ".osmnx_cache"


def miles_to_meters(miles: float) -> float:
    return miles * 1609.344


def meters_to_miles(m: float) -> float:
    return m / 1609.344


def download_network(
    lat: float,
    lon: float,
    radius_miles: float,
    network_type: str = "bike",
) -> nx.MultiDiGraph:
    """
    Download the cycling road network within radius_miles of (lat, lon).
    Results are cached on disk by OSMnx.
    """
    import pyproj

    radius_m = miles_to_meters(radius_miles)
    G = ox.graph_from_point(
        (lat, lon),
        dist=radius_m,
        network_type=network_type,
        retain_all=False,
        simplify=True,
    )
    # Project to UTM for accurate distance / routing calculations.
    G = ox.project_graph(G)

    # Re-derive geographic lat/lon by inverse-projecting the UTM x/y.
    # We do this AFTER projection so it works even when loading from cache
    # (where the pre-projection x/y values are gone).
    transformer = pyproj.Transformer.from_crs(
        G.graph["crs"], "EPSG:4326", always_xy=True
    )
    for _, data in G.nodes(data=True):
        lon_geo, lat_geo = transformer.transform(data["x"], data["y"])
        data["lon"] = float(lon_geo)
        data["lat"] = float(lat_geo)

    return G


def nearest_node(G: nx.MultiDiGraph, lat: float, lon: float) -> int:
    """
    Return the graph node ID nearest to (lat, lon).
    Automatically projects the input coordinates to the graph's CRS
    so this works correctly on both geographic and projected graphs.
    """
    import pyproj
    crs = G.graph.get("crs")
    if crs and str(crs) != "epsg:4326":
        # Graph is projected (e.g. UTM) — convert input lat/lon to graph CRS
        transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", crs, always_xy=True
        )
        x, y = transformer.transform(lon, lat)   # always_xy: lon→X, lat→Y
        return ox.nearest_nodes(G, x, y)
    else:
        return ox.nearest_nodes(G, lon, lat)


def node_coords(G: nx.MultiDiGraph, node_id: int) -> tuple[float, float]:
    """
    Return (lat, lon) of a graph node.
    Uses the 'lat'/'lon' attributes saved before UTM projection.
    """
    data = G.nodes[node_id]
    if "lat" in data and "lon" in data:
        return float(data["lat"]), float(data["lon"])
    # Fallback: y/x in projected graph are UTM metres, not usable directly.
    # This shouldn't happen if download_network saved lat/lon before projecting.
    raise ValueError(f"Node {node_id} has no lat/lon attributes. "
                     "Ensure download_network saves lat/lon before projecting.")


def nodes_at_distance(
    G: nx.MultiDiGraph,
    origin_node: int,
    target_dist_m: float,
    tolerance_fraction: float = 0.15,
) -> list[int]:
    """
    Return all nodes whose shortest-path distance from origin_node is within
    ±tolerance_fraction of target_dist_m.

    Uses Dijkstra's algorithm weighted by edge 'length' (metres in projected graph).
    """
    lo = target_dist_m * (1 - tolerance_fraction)
    hi = target_dist_m * (1 + tolerance_fraction)

    lengths = nx.single_source_dijkstra_path_length(
        G, origin_node, cutoff=hi, weight="length"
    )
    return [n for n, d in lengths.items() if lo <= d <= hi]


def bearing_between(
    G: nx.MultiDiGraph,
    origin_node: int,
    other_node: int,
) -> float:
    """
    Compass bearing (0–360°, clockwise from North) from origin_node to other_node
    using stored node x/y coordinates (projected CRS, approximate).
    """
    o = G.nodes[origin_node]
    t = G.nodes[other_node]
    dx = t["x"] - o["x"]
    dy = t["y"] - o["y"]
    angle = math.degrees(math.atan2(dx, dy)) % 360
    return angle


def shortest_path(
    G: nx.MultiDiGraph,
    source: int,
    target: int,
    weight: str = "length",
) -> Optional[list[int]]:
    """Return shortest-path node list, or None if unreachable."""
    try:
        return nx.shortest_path(G, source, target, weight=weight)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def path_length_m(G: nx.MultiDiGraph, path: list[int]) -> float:
    """Total edge-length of a node path in metres."""
    if len(path) < 2:
        return 0.0
    total = 0.0
    for u, v in zip(path[:-1], path[1:]):
        # MultiDiGraph: take minimum-length parallel edge
        edges = G[u][v]
        total += min(d.get("length", 0) for d in edges.values())
    return total


def path_coords(
    G: nx.MultiDiGraph,
    path: list[int],
    geographic: bool = True,
) -> list[tuple[float, float]]:
    """
    Extract (lat, lon) coordinate list from a node path.

    If geographic=True, attempts to return geographic lat/lon.
    Falls back to projected (y, x) if no lat/lon stored.
    """
    coords = []
    for n in path:
        data = G.nodes[n]
        if geographic and "lat" in data:
            coords.append((float(data["lat"]), float(data["lon"])))
        else:
            # projected coords — caller must handle
            coords.append((float(data.get("y", 0)), float(data.get("x", 0))))
    return coords


def paved_fraction(G: nx.MultiDiGraph, path: list[int]) -> float:
    """
    Estimate fraction of path length that is paved.
    Uses OSM 'surface' tag first, then highway type via _is_edge_paved.
    """
    total_len = 0.0
    paved_len = 0.0
    for u, v in zip(path[:-1], path[1:]):
        edges = G[u][v]
        best = min(edges.values(), key=lambda d: d.get("length", 1e9))
        length = best.get("length", 0)
        total_len += length
        if _is_edge_paved(best):
            paved_len += length
    return (paved_len / total_len) if total_len > 0 else 0.0


def _is_edge_paved(edge_data: dict) -> bool:
    """
    Return True if an OSM edge is paved.
    Checks the 'surface' tag first (most reliable), then falls back to
    highway type. Tracks, paths, and unclassified roads default to unpaved.
    """
    surface = edge_data.get("surface", "")
    if isinstance(surface, list):
        surface = surface[0] if surface else ""
    surface = str(surface).lower()

    PAVED_SURFACES = {
        "paved", "asphalt", "concrete", "concrete:plates", "concrete:lanes",
        "paving_stones", "sett", "cobblestone", "metal",
    }
    UNPAVED_SURFACES = {
        "unpaved", "gravel", "fine_gravel", "compacted", "dirt", "earth",
        "grass", "ground", "mud", "sand", "woodchips", "pebblestone",
        "rock", "rocky", "stone",
    }
    if surface in PAVED_SURFACES:
        return True
    if surface in UNPAVED_SURFACES:
        return False

    hw = edge_data.get("highway", "")
    if isinstance(hw, list):
        hw = hw[0] if hw else ""
    hw = str(hw).lower()

    PAVED_HIGHWAYS = {
        "motorway", "motorway_link",
        "trunk", "trunk_link",
        "primary", "primary_link",
        "secondary", "secondary_link",
        "tertiary", "tertiary_link",
        "residential", "living_street",
        "cycleway", "service",
    }
    UNPAVED_HIGHWAYS = {
        "track", "path", "footway", "bridleway", "steps",
    }
    if hw in PAVED_HIGHWAYS:
        return True
    if hw in UNPAVED_HIGHWAYS:
        return False

    # unclassified / unknown → assume unpaved to be conservative
    return False


def paved_weight_graph(G: nx.MultiDiGraph, unpaved_penalty: float = 8.0) -> nx.MultiDiGraph:
    """
    Return a copy of G where unpaved edges have their length multiplied by
    unpaved_penalty, biasing shortest-path routing strongly toward paved roads.
    A penalty of 8 means the router will accept a paved detour up to 8x longer
    before using a dirt road.
    """
    H = G.copy()
    for u, v, key, data in H.edges(keys=True, data=True):
        if not _is_edge_paved(data):
            H[u][v][key]["length"] = data.get("length", 1.0) * unpaved_penalty
    return H