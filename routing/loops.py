"""
rcycle/routing/loops.py

Generates candidate loop cycling routes from an origin point.

Algorithm
---------
Anchor-based multi-lobe routing.

1. Find candidate anchor nodes spread around the compass at ~target/N distance
2. Score anchors by surface quality, shade, and angular diversity
3. Select 2–4 best anchors with enforced angular spread (min 60° apart)
4. Route origin → A1 → A2 → ... → AN → origin, penalising used edges
   between legs so each segment uses different roads
5. Run multiple attempts with varied anchor counts and distances for diversity

This produces natural "clover" or "figure-8" shaped routes with multiple
distinct lobes — similar to how a cyclist manually plans a route by picking
2–4 destination waypoints and connecting them.
"""

from __future__ import annotations
import math
import random
import networkx as nx
from dataclasses import dataclass
from typing import Optional

from routing.network import (
    bearing_between,
    shortest_path,
    path_length_m,
    path_coords,
    paved_fraction,
    shade_fraction,
    build_shade_index,
    paved_weight_graph,
    miles_to_meters,
    meters_to_miles,
    nodes_at_distance,
    _is_edge_paved,
    _is_cycling_path,
    _has_bike_lane,
)


@dataclass
class CandidateLoop:
    path: list[int]
    length_miles: float
    outbound_path: list[int]   # kept for API compat — walk portion
    return_path: list[int]     # kept for API compat — final leg home
    turnaround_node: int       # furthest anchor node
    bearing_deg: float
    target_miles: float
    coords: list[tuple[float, float]]
    paved_frac: float = 0.0
    loop_ratio: float = 0.0
    shade_frac: float = 0.0


def _loop_ratio(path: list[int]) -> float:
    """Fraction of nodes that appear only once — 1.0 = perfect loop, 0.0 = all reused."""
    if len(path) < 2:
        return 0.0
    edges = [(min(u, v), max(u, v)) for u, v in zip(path[:-1], path[1:])]
    unique = len(set(edges))
    return unique / max(len(edges), 1)


def _penalise_used_edges(G: nx.MultiDiGraph, path: list[int], factor: float = 5.0):
    H = G.copy()
    for u, v in zip(path[:-1], path[1:]):
        for a, b in ((u, v), (v, u)):
            if H.has_edge(a, b):
                for key in H[a][b]:
                    H[a][b][key]["length"] = H[a][b][key].get("length", 1.0) * factor
    return H


def _angular_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _node_quality(
    G: nx.MultiDiGraph,
    node: int,
    origin_node: int,
    shade_tree,
    shade_transformer,
) -> float:
    """
    Score an anchor node by the quality of roads leading to it
    and whether it sits in a shaded / park area.
    """
    score = 0.0

    # Check edges around this node
    neighbors = list(G.successors(node)) + list(G.predecessors(node))
    paved_count = 0
    cycling_count = 0
    bike_lane_count = 0
    total = 0
    for nb in neighbors[:8]:
        for direction in ((node, nb), (nb, node)):
            u, v = direction
            if G.has_edge(u, v):
                ed = min(G[u][v].values(), key=lambda d: d.get("length", 1e9))
                if _is_cycling_path(ed):
                    cycling_count += 1
                elif _has_bike_lane(ed):
                    bike_lane_count += 1
                elif _is_edge_paved(ed):
                    paved_count += 1
                total += 1
                break

    if total > 0:
        score += (cycling_count * 3.0 + bike_lane_count * 2.0 + paved_count * 1.0) / total

    # Shade bonus
    if shade_tree is not None:
        try:
            from shapely.geometry import Point
            nd = G.nodes[node]
            if shade_transformer:
                lon, lat = shade_transformer.transform(nd["x"], nd["y"])
            else:
                lon, lat = nd["x"], nd["y"]
            if shade_tree.query(Point(lon, lat), predicate="intersects").size > 0:
                score += 1.5
        except Exception:
            pass

    return score


def _select_anchors(
    G: nx.MultiDiGraph,
    origin_node: int,
    target_m: float,
    num_anchors: int,
    rng: random.Random,
    shade_tree,
    shade_transformer,
    dist_fraction: float = 0.28,   # each anchor ~28% of total distance from origin
    tolerance: float = 0.25,
    min_angular_sep: float = 55.0, # minimum degrees between anchors
) -> list[int]:
    """
    Find num_anchors well-spread anchor nodes around the origin.

    Strategy:
    - Find all nodes at ~dist_fraction * target_m from origin
    - Score each by road quality + shade
    - Greedily pick anchors that are at least min_angular_sep apart
    - Shuffle candidates slightly so different seeds pick different anchors
    """
    anchor_dist_m = target_m * dist_fraction
    candidates = nodes_at_distance(G, origin_node, anchor_dist_m, tolerance_fraction=tolerance)

    if not candidates:
        # Widen tolerance
        candidates = nodes_at_distance(G, origin_node, anchor_dist_m, tolerance_fraction=0.45)
    if not candidates:
        return []

    # Score and shuffle (slight randomness per seed)
    scored = []
    for node in candidates:
        bearing = bearing_between(G, origin_node, node)
        q = _node_quality(G, node, origin_node, shade_tree, shade_transformer)
        # Add small random jitter so different seeds explore different anchors
        jitter = rng.uniform(-0.3, 0.3)
        scored.append((q + jitter, bearing, node))

    scored.sort(reverse=True)

    # Greedy angular spread selection
    selected_bearings = []
    selected_nodes = []

    for _, bearing, node in scored:
        if not selected_bearings:
            selected_bearings.append(bearing)
            selected_nodes.append(node)
            continue

        # Check angular separation from all already-selected anchors
        min_sep = min(_angular_diff(bearing, b) for b in selected_bearings)
        if min_sep >= min_angular_sep:
            selected_bearings.append(bearing)
            selected_nodes.append(node)

        if len(selected_nodes) >= num_anchors:
            break

    # Sort selected anchors by bearing so the route flows clockwise
    # (reduces unnecessary crossing of legs)
    if len(selected_nodes) >= 2:
        pairs = sorted(zip(selected_bearings, selected_nodes))
        selected_bearings = [b for b, _ in pairs]
        selected_nodes = [n for _, n in pairs]

    return selected_nodes


def _route_through_anchors(
    G: nx.MultiDiGraph,
    origin_node: int,
    anchors: list[int],
    target_m: float,
) -> Optional[CandidateLoop]:
    """
    Route origin → anchor[0] → anchor[1] → ... → origin.
    Each leg penalises edges used by prior legs to encourage diverse roads.
    """
    if not anchors:
        return None

    full_path: list[int] = [origin_node]
    used_edges: list[int] = [origin_node]
    total_dist_m = 0.0

    waypoints = anchors + [origin_node]
    G_current = G.copy()

    for wp in waypoints:
        src = full_path[-1]
        if src == wp:
            continue

        leg = shortest_path(G_current, src, wp)
        if leg is None:
            # Try unpenalised graph
            leg = shortest_path(G, src, wp)
        if leg is None:
            return None

        leg_dist = path_length_m(G, leg)
        total_dist_m += leg_dist

        # Stitch (skip duplicate junction node)
        full_path.extend(leg[1:])

        # Penalise this leg's edges so the next leg uses different roads
        G_current = _penalise_used_edges(G_current, leg, factor=5.0)

    if len(full_path) < 4:
        return None

    # Distance tolerance: 55%–170% of target
    if not (target_m * 0.55 <= total_dist_m <= target_m * 1.70):
        return None

    lr = _loop_ratio(full_path)

    o_data = G.nodes[origin_node]
    ox_, oy_ = o_data["x"], o_data["y"]
    furthest = max(full_path, key=lambda n: math.hypot(
        G.nodes[n]["x"] - ox_, G.nodes[n]["y"] - oy_
    ))
    bearing = bearing_between(G, origin_node, furthest)
    coords = path_coords(G, full_path[::max(1, len(full_path) // 200)])

    # For API compat — outbound = everything before last leg home
    last_anchor = anchors[-1]
    try:
        split = len(full_path) - full_path[::-1].index(last_anchor) - 1
        outbound = full_path[:split + 1]
        return_leg = full_path[split:]
    except ValueError:
        outbound = full_path[:len(full_path) // 2]
        return_leg = full_path[len(full_path) // 2:]

    return CandidateLoop(
        path=full_path,
        length_miles=meters_to_miles(total_dist_m),
        outbound_path=outbound,
        return_path=return_leg,
        turnaround_node=furthest,
        bearing_deg=bearing,
        target_miles=meters_to_miles(target_m),
        coords=coords,
        paved_frac=0.0,
        loop_ratio=lr,
        shade_frac=0.0,
    )


def generate_candidates(
    G: nx.MultiDiGraph,
    origin_node: int,
    target_miles: float,
    num_spokes: int = 8,
    tolerance: float = 0.20,
    min_loop_ratio: float = 0.08,
    shade_polys: list = None,
) -> list[CandidateLoop]:
    """
    Generate candidate loop routes using anchor-based multi-lobe routing.

    Tries multiple combinations of:
    - anchor count (2, 3, 4)
    - anchor distance fraction (how far out each anchor sits)
    - random seed (varies which specific anchors are chosen)

    Returns deduplicated list sorted by composite quality score.
    """
    target_m = miles_to_meters(target_miles)
    shade_polys = shade_polys or []

    G_weighted = paved_weight_graph(G, unpaved_penalty=8.0, park_polys=shade_polys)
    shade_tree, shade_transformer = build_shade_index(G, shade_polys)

    num_seeds = max(num_spokes * 3, 24)

    raw: list[CandidateLoop] = []

    # Vary anchor count and distance fraction across seeds
    # More seeds = more diversity
    configs = []
    for seed in range(num_seeds):
        # Cycle through anchor counts: 4, 3, 2 (prefer 4-lobe)
        n_anchors = [4, 4, 3, 4, 3, 2, 4, 3][seed % 8]

        # Vary how far anchors sit from origin
        # 4 anchors → shorter radius per anchor; 2 anchors → longer
        base_frac = {4: 0.26, 3: 0.30, 2: 0.38}[n_anchors]
        dist_frac = base_frac + (seed % 5) * 0.02   # slight variation

        configs.append((seed, n_anchors, dist_frac))

    for seed, n_anchors, dist_frac in configs:
        rng = random.Random(seed)
        try:
            anchors = _select_anchors(
                G=G,
                origin_node=origin_node,
                target_m=target_m,
                num_anchors=n_anchors,
                rng=rng,
                shade_tree=shade_tree,
                shade_transformer=shade_transformer,
                dist_fraction=dist_frac,
            )

            if len(anchors) < 2:
                # Fall back to fewer anchors with wider tolerance
                anchors = _select_anchors(
                    G=G,
                    origin_node=origin_node,
                    target_m=target_m,
                    num_anchors=2,
                    rng=rng,
                    shade_tree=shade_tree,
                    shade_transformer=shade_transformer,
                    dist_fraction=dist_frac,
                    tolerance=0.45,
                    min_angular_sep=40.0,
                )

            if len(anchors) < 2:
                continue

            # Try different orderings of the same anchors (different route shapes)
            orderings = [anchors]
            if len(anchors) >= 3:
                # Also try reversed order
                orderings.append(list(reversed(anchors)))
            if len(anchors) == 4 and seed % 3 == 0:
                # Try swapping middle two
                alt = [anchors[0], anchors[2], anchors[1], anchors[3]]
                orderings.append(alt)

            for ordered_anchors in orderings:
                result = _route_through_anchors(
                    G=G_weighted,
                    origin_node=origin_node,
                    anchors=ordered_anchors,
                    target_m=target_m,
                )
                if result is not None and result.loop_ratio >= min_loop_ratio:
                    result.paved_frac = paved_fraction(G, result.path)
                    result.shade_frac = shade_fraction(
                        G, result.path, shade_polys,
                        _tree=shade_tree, _transformer=shade_transformer,
                    )
                    raw.append(result)

        except Exception:
            continue

    if not raw:
        return []

    def _quality(c: CandidateLoop) -> float:
        dist_err = abs(c.length_miles - target_miles) / target_miles
        return (
            c.loop_ratio * 0.35
            + c.paved_frac * 0.30
            + c.shade_frac * 0.20
            - dist_err * 0.15
        )

    raw.sort(key=_quality, reverse=True)

    # Deduplicate — drop routes sharing >60% edge overlap
    kept: list[CandidateLoop] = []
    for cand in raw:
        cand_edges = set(zip(cand.path[:-1], cand.path[1:]))
        duplicate = any(
            len(cand_edges & set(zip(p.path[:-1], p.path[1:]))) / max(len(cand_edges), 1) > 0.60
            for p in kept
        )
        if not duplicate:
            kept.append(cand)

    return kept