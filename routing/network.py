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

    # Custom filter: keep roads cyclists actually use.
    # Excludes motorways/trunk (too fast), pure footways, and
    # private/construction ways. This cuts 20–40% of nodes in dense
    # cities vs the default network_type="bike" filter.
    BIKE_FILTER = (
        '["highway"!~"motorway|motorway_link|trunk|trunk_link'
        '|footway|steps|corridor|elevator|escalator'
        '|construction|proposed|abandoned|raceway"]'
        '["access"!~"private|no"]'
    )

    G = ox.graph_from_point(
        (lat, lon),
        dist=radius_m,
        network_type=network_type,
        custom_filter=BIKE_FILTER,
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

    Uses a two-step approach for speed:
    1. Euclidean pre-filter — discard nodes geometrically too far to be
       reachable at target_dist_m (roads wind ~1.3× straight-line distance,
       so we use 1.5× as a safe cap). This shrinks the subgraph Dijkstra
       has to flood by 50–80% on large urban networks.
    2. Dijkstra on the subgraph with cutoff=hi for the exact answer.
    """
    lo = target_dist_m * (1 - tolerance_fraction)
    hi = target_dist_m * (1 + tolerance_fraction)

    # Euclidean pre-filter in UTM space (x/y are metres in projected graph)
    o_data = G.nodes[origin_node]
    ox_, oy_ = o_data["x"], o_data["y"]
    euclidean_cap = hi * 1.5   # 1.5× accounts for road winding
    candidate_nodes = [
        n for n, d in G.nodes(data=True)
        if math.hypot(d["x"] - ox_, d["y"] - oy_) <= euclidean_cap
    ]

    subgraph = G.subgraph(candidate_nodes)

    lengths = nx.single_source_dijkstra_path_length(
        subgraph, origin_node, cutoff=hi, weight="length"
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
    Extract (lat, lon) coordinate list from a node path, using edge geometry
    where available so the line follows actual road curves rather than drawing
    straight lines between intersections.
    """
    import pyproj
    # Build a transformer from the graph CRS back to WGS84 for projected graphs
    crs = G.graph.get("crs")
    transformer = None
    if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
        try:
            transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

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

    if len(path) == 0:
        return []
    if len(path) == 1:
        return [_node_latlon(path[0])]

    coords = []
    for u, v in zip(path[:-1], path[1:]):
        if not G.has_edge(u, v):
            # Edge missing in this graph — fall back to just the node point
            coords.append(_node_latlon(u))
            continue
        edges = G[u][v]
        best = min(edges.values(), key=lambda d: d.get("length", 1e9))
        geom = best.get("geometry")  # Shapely LineString or None

        if geom is not None:
            # Edge has full road geometry — extract all intermediate points
            pts = list(geom.coords)  # (x, y) in graph CRS
            # Detect if geometry runs u→v or v→u by comparing first point to u
            u_data = G.nodes[u]
            ux, uy = u_data["x"], u_data["y"]
            if pts and abs(pts[0][0] - ux) > abs(pts[-1][0] - ux):
                pts = pts[::-1]  # reverse so it runs u→v
            for x, y in pts[:-1]:  # exclude last point, added by next edge's first
                coords.append(_proj_to_latlon(x, y))
        else:
            # No geometry — just use the node point
            coords.append(_node_latlon(u))

    # Always append the final node
    coords.append(_node_latlon(path[-1]))
    return coords


def paved_fraction(G: nx.MultiDiGraph, path: list[int]) -> float:
    """
    Estimate fraction of path length that is paved.
    Uses OSM 'surface' tag first, then highway type via _is_edge_paved.
    """
    total_len = 0.0
    paved_len = 0.0
    for u, v in zip(path[:-1], path[1:]):
        if not G.has_edge(u, v):
            continue
        edges = G[u][v]
        best = min(edges.values(), key=lambda d: d.get("length", 1e9))
        length = best.get("length", 0)
        total_len += length
        if _is_edge_paved(best):
            paved_len += length
    return (paved_len / total_len) if total_len > 0 else 0.0


def _is_cycling_path(edge_data: dict) -> bool:
    """
    Return True if this edge is a designated or clearly cycling-friendly path,
    even if it's tagged as 'path', 'footway', or 'track'.

    Catches: park paths, riverside trails, converted rail paths, greenways —
    anything that OSM tags with bicycle access but isn't a full road.
    """
    # Explicit bicycle designation
    bicycle = str(edge_data.get("bicycle", "")).lower()
    if bicycle in ("designated", "yes", "permissive", "official"):
        return True

    # foot=no on a path usually means it's cycling-only
    foot = str(edge_data.get("foot", "")).lower()
    if foot == "no":
        return True

    # cycleway access tags (e.g. cycleway=track alongside a road)
    for tag in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both"):
        val = str(edge_data.get(tag, "")).lower()
        if val in ("track", "lane", "shared_lane", "designated", "yes"):
            return True

    # highway=path with no explicit bicycle restriction and paved surface
    hw = edge_data.get("highway", "")
    if isinstance(hw, list):
        hw = hw[0] if hw else ""
    hw = str(hw).lower()

    surface = edge_data.get("surface", "")
    if isinstance(surface, list):
        surface = surface[0] if surface else ""
    surface = str(surface).lower()

    PAVED_SURFACES = {
        "paved", "asphalt", "concrete", "concrete:plates", "concrete:lanes",
        "paving_stones", "sett", "cobblestone",
    }
    if hw in ("path", "footway") and surface in PAVED_SURFACES:
        return True

    return False


def _is_edge_paved(edge_data: dict) -> bool:
    """
    Return True if an OSM edge is paved/cycling-friendly.
    Checks surface tag first, then cycling path designation, then highway type.
    """
    # ── Surface tag (most reliable) ──────────────────────────────────────────
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

    # ── Cycling path check ───────────────────────────────────────────────────
    if _is_cycling_path(edge_data):
        return True

    # ── Highway type fallback ────────────────────────────────────────────────
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


def paved_weight_graph(
    G: nx.MultiDiGraph,
    unpaved_penalty: float = 8.0,
    park_polys: list = None,
    shade_bonus: float = 0.80,   # multiplier for paved edges inside shade polys
) -> nx.MultiDiGraph:
    """
    Return a copy of G with edge weights adjusted for surface quality and shade:

    - Paved roads: unchanged (1×)
    - Paved roads inside shade/park polys: 0.80× (router prefers shaded roads)
    - Designated cycling paths: 0.55× (bonus — router actively prefers them)
    - Cycling paths inside shade: 0.55 × 0.80 = 0.44×
    - Paths/tracks inside park polygons: 0.70× (preferred — parks are shadier and more scenic)
    - Unpaved non-cycling edges: 8× penalty (router avoids unless no alternative)

    park_polys is the list of Shapely geometries from download_shade_features.
    Passing it in allows river trails and park paths to be treated as
    cycling-friendly even when their OSM bicycle tag is missing, and gives
    paved roads under tree cover a routing preference so shade steers the route.
    """
    try:
        from shapely.geometry import Point
        from shapely.strtree import STRtree
        import pyproj

        crs = G.graph.get("crs")
        transformer = None
        if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
            transformer = pyproj.Transformer.from_crs(
                crs, "EPSG:4326", always_xy=True
            )

        park_tree = STRtree(park_polys) if park_polys else None

        def _edge_in_park(u_data, v_data) -> bool:
            if park_tree is None:
                return False
            mx = (u_data["x"] + v_data["x"]) / 2
            my = (u_data["y"] + v_data["y"]) / 2
            if transformer:
                lon, lat = transformer.transform(mx, my)
            else:
                lon, lat = mx, my
            pt = Point(lon, lat)
            return park_tree.query(pt, predicate="intersects").size > 0

    except Exception:
        park_tree = None

        def _edge_in_park(u_data, v_data) -> bool:
            return False

    H = G.copy()
    for u, v, key, data in H.edges(keys=True, data=True):
        length = data.get("length", 1.0)

        # Determine if this edge sits inside a shade/park polygon
        try:
            u_data = H.nodes[u]
            v_data = H.nodes[v]
            in_shade = _edge_in_park(u_data, v_data)
        except Exception:
            in_shade = False

        if _is_cycling_path(data):
            # Designated cycling path — actively prefer it
            base = length * 0.55
        elif _is_edge_paved(data):
            # Normal paved road — no base penalty
            base = length
        else:
            # Unpaved — check if it's inside a park before penalising
            if in_shade:
                # Park path with no explicit cycling tag — mild preference.
                # Parks are typically shadier, quieter, and more scenic;
                # 0.70× encourages routing through them over busy streets.
                base = length * 0.70
            else:
                # Genuine unpaved non-park edge — penalise
                base = length * unpaved_penalty

        # Apply shade bonus on top: router prefers paved/cycling edges in shade
        if in_shade and _is_edge_paved(data):
            base *= shade_bonus

        H[u][v][key]["length"] = base

    return H


# ── Shade / tree cover ────────────────────────────────────────────────────────

def download_shade_features(
    lat: float,
    lon: float,
    radius_miles: float,
) -> list:
    """
    Download tree cover and shade polygons from OSM within radius_miles.
    Returns a list of Shapely geometries (forests, parks, tree rows).
    Falls back to an empty list if the query fails or shapely is unavailable.
    """
    try:
        import osmnx as ox
        from shapely.geometry import MultiPolygon, Polygon
        from shapely.ops import unary_union

        radius_m = miles_to_meters(radius_miles)

        # Tags that indicate meaningful tree cover / shade
        shade_tags = {
            # Tree cover and natural terrain
            "natural":  ["wood", "tree_row", "tree",
                         "scrub", "heath", "shrubbery"],
            "landuse":  ["forest", "orchard", "vineyard",
                         "recreation_ground", "grass", "meadow",
                         "conservation"],
            "leisure":  ["park", "garden", "nature_reserve"],
            # Protected land — "national_forest" is the correct OSM tag for
            # US National Forests (e.g. Los Padres, Sierra); "forest" alone
            # is not a valid boundary value and was silently ignored by OSM.
            "boundary": ["national_park", "national_forest",
                         "protected_area", "wilderness",
                         "regional_park"],
            # Open space districts (common in Bay Area / California)
            "ownership": ["national_forest", "state_forest"],
        }

        try:
            features = ox.features_from_point(
                (lat, lon), tags=shade_tags, dist=radius_m
            )
        except Exception:
            return []

        if features is None or features.empty:
            return []

        polys = []
        MAX_AREA_DEG2 = 0.5     # ~5,000 km² — allows national parks/wilderness, skips continent-scale polygons
        SIMPLIFY_TOL  = 0.0001  # ~10 m tolerance — reduces vertex count dramatically
        # No hard polygon cap — simplification already makes each poly cheap.
        # We sort large polygons first so the most impactful shade features
        # (big parks, forests) are always included even in very dense areas.

        from shapely.ops import unary_union

        raw = []
        for geom in features.geometry:
            if geom is None:
                continue

            # GeometryCollection (returned for OSM Relations like open space
            # districts and national forests) — extract all polygon members
            if geom.geom_type == "GeometryCollection":
                parts = [g for g in geom.geoms
                         if g.geom_type in ("Polygon", "MultiPolygon")]
                if parts:
                    geom = unary_union(parts)
                else:
                    continue

            if geom.geom_type in ("Polygon", "MultiPolygon"):
                if geom.area < MAX_AREA_DEG2:
                    raw.append((geom.area, geom.simplify(SIMPLIFY_TOL, preserve_topology=True)))
                else:
                    # Very large polygon (e.g. entire national forest boundary) —
                    # clip it to the query radius before including so we don't
                    # store a massive geometry and still get the shaded region.
                    from shapely.geometry import Point
                    centre = Point(lon, lat)
                    clip   = centre.buffer(radius_m / 111320)  # degrees approx
                    clipped = geom.intersection(clip)
                    if not clipped.is_empty:
                        raw.append((clipped.area, clipped.simplify(SIMPLIFY_TOL, preserve_topology=True)))
            elif geom.geom_type == "LineString":
                # tree rows — buffer ~10m in degrees (~0.00009°)
                raw.append((0.0, geom.simplify(SIMPLIFY_TOL).buffer(0.00009)))
            # skip Point geometries — individual trees are too noisy to render

        # Sort largest first so big parks are always kept; small parks follow
        raw.sort(key=lambda x: x[0], reverse=True)
        polys = [g for _, g in raw]

        return polys

    except Exception:
        return []


def build_shade_index(G: nx.MultiDiGraph, shade_polys: list):
    """
    Pre-build the STRtree spatial index and CRS transformer once so that
    shade_fraction() can reuse them across all routes instead of rebuilding
    per call.  Returns (STRtree | None, transformer | None).
    """
    if not shade_polys:
        return None, None
    try:
        import pyproj
        from shapely.strtree import STRtree

        tree = STRtree(shade_polys)

        crs = G.graph.get("crs")
        transformer = None
        if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
            try:
                transformer = pyproj.Transformer.from_crs(
                    crs, "EPSG:4326", always_xy=True
                )
            except Exception:
                pass
        return tree, transformer
    except Exception:
        return None, None


def shade_fraction(
    G: nx.MultiDiGraph,
    path: list[int],
    shade_polys: list,
    _tree=None,
    _transformer=None,
) -> float:
    """
    Estimate fraction of the path that is shaded by tree cover.
    For each edge, checks if its midpoint falls within any shade polygon.
    Returns 0.0–1.0 (1.0 = fully shaded).
    Falls back to 0.0 if shapely is unavailable or polys is empty.

    Pass pre-built _tree / _transformer from build_shade_index() to avoid
    rebuilding the spatial index on every call.
    """
    if not shade_polys or len(path) < 2:
        return 0.0

    try:
        import pyproj
        from shapely.geometry import Point
        from shapely.strtree import STRtree

        # Reuse pre-built index if provided, otherwise build now (slow path)
        if _tree is not None:
            tree = _tree
            transformer = _transformer
        else:
            tree = STRtree(shade_polys)
            crs = G.graph.get("crs")
            transformer = None
            if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
                try:
                    transformer = pyproj.Transformer.from_crs(
                        crs, "EPSG:4326", always_xy=True
                    )
                except Exception:
                    pass

        def _edge_midpoint_latlon(u, v):
            """Return (lat, lon) of edge midpoint using geometry if available."""
            if not G.has_edge(u, v):
                return None
            best = min(G[u][v].values(), key=lambda d: d.get("length", 1e9))
            geom = best.get("geometry")
            if geom is not None:
                mp = geom.interpolate(0.5, normalized=True)
                x, y = mp.x, mp.y
            else:
                u_d, v_d = G.nodes[u], G.nodes[v]
                x = (u_d["x"] + v_d["x"]) / 2
                y = (u_d["y"] + v_d["y"]) / 2
            if transformer:
                lon, lat = transformer.transform(x, y)
                return float(lat), float(lon)
            return float(y), float(x)

        total_len = 0.0
        shaded_len = 0.0

        for u, v in zip(path[:-1], path[1:]):
            if not G.has_edge(u, v):
                continue
            best = min(G[u][v].values(), key=lambda d: d.get("length", 1e9))
            edge_len = best.get("length", 0.0)
            total_len += edge_len

            # Fast tunnel/covered check — always shaded
            if best.get("tunnel") or best.get("covered") == "yes":
                shaded_len += edge_len
                continue

            mid = _edge_midpoint_latlon(u, v)
            if mid is None:
                continue
            pt = Point(mid[1], mid[0])  # shapely uses (lon, lat)
            if tree.query(pt, predicate="intersects").size > 0:
                shaded_len += edge_len

        return (shaded_len / total_len) if total_len > 0 else 0.0

    except Exception:
        return 0.0


# ── Traffic proximity heatmap ─────────────────────────────────────────────────

# Intensity by highway class — higher = more traffic pollution exposure
_TRAFFIC_INTENSITY = {
    "motorway":       1.00,
    "motorway_link":  0.90,
    "trunk":          0.85,
    "trunk_link":     0.75,
    "primary":        0.65,
    "primary_link":   0.55,
    "secondary":      0.45,
    "secondary_link": 0.38,
    "tertiary":       0.25,
    "tertiary_link":  0.20,
}


def compute_traffic_heat(
    G: nx.MultiDiGraph,
    sample_spacing_m: float = 40.0,
) -> list[list[float]]:
    """
    Build a heatmap point cloud from the road network.
    Only includes roads with meaningful traffic (motorway → tertiary).
    Samples points every sample_spacing_m metres along each edge geometry.

    Returns list of [lat, lon, intensity] where intensity is 0–1.
    These can be fed directly into Leaflet.heat.
    """
    import pyproj

    crs = G.graph.get("crs")
    transformer = None
    if crs and str(crs).lower() not in ("epsg:4326", "wgs84"):
        try:
            transformer = pyproj.Transformer.from_crs(
                crs, "EPSG:4326", always_xy=True
            )
        except Exception:
            pass

    def _to_latlon(x, y):
        if transformer:
            lon, lat = transformer.transform(x, y)
            return float(lat), float(lon)
        return float(y), float(x)

    points = []
    seen_edges = set()

    for u, v, data in G.edges(data=True):
        # Deduplicate bidirectional edges
        edge_key = (min(u, v), max(u, v))
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        hw = data.get("highway", "")
        if isinstance(hw, list):
            hw = hw[0] if hw else ""
        hw = str(hw).lower()

        intensity = _TRAFFIC_INTENSITY.get(hw)
        if intensity is None:
            continue  # skip residential, cycleway, path etc.

        geom = data.get("geometry")
        length = data.get("length", 0.0)

        if geom is not None and length > 0:
            # Sample evenly along the geometry
            n_samples = max(2, int(length / sample_spacing_m))
            for i in range(n_samples):
                frac = i / (n_samples - 1) if n_samples > 1 else 0.5
                pt = geom.interpolate(frac, normalized=True)
                lat, lon = _to_latlon(pt.x, pt.y)
                points.append([lat, lon, round(intensity, 2)])
        else:
            # Fallback: just use the two endpoints
            u_d, v_d = G.nodes[u], G.nodes[v]
            for node_d in (u_d, v_d):
                lat, lon = _to_latlon(node_d["x"], node_d["y"])
                points.append([lat, lon, round(intensity, 2)])

    return points