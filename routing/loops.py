"""
rcycle/routing/loops.py

Generates candidate loop cycling routes from an origin point.

Algorithm
---------
For each target distance and compass bearing (spoke):
  1. Find the graph node that lies at ~half the target distance in that
     bearing direction (the "turnaround node").
  2. Route out from origin → turnaround via shortest path.
  3. Route back from turnaround → origin using a *penalised* graph where
     edges on the outbound path are given high weight — encouraging a
     different return path and a true loop shape.
  4. Stitch outbound + return into one loop, deduplicate nodes.

This produces diverse, geographically real loops using the actual
cycling road network.
"""

from __future__ import annotations
import math
import copy
import numpy as np
import networkx as nx
from dataclasses import dataclass, field

from routing.network import (
    nodes_at_distance,
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
)


@dataclass
class CandidateLoop:
    path: list[int]                    # ordered node IDs
    length_miles: float
    outbound_path: list[int]
    return_path: list[int]
    turnaround_node: int
    bearing_deg: float                 # spoke direction
    target_miles: float
    coords: list[tuple[float, float]]  # (lat, lon) sampled along route
    paved_frac: float = 0.0
    loop_ratio: float = 0.0            # 1.0 = perfect loop, 0 = out-and-back
    shade_frac: float = 0.0            # fraction of route under tree cover / shade


def _angular_diff(a: float, b: float) -> float:
    """Absolute angular difference between two bearings (0–180°)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _penalise_used_edges(
    G: nx.MultiDiGraph,
    path: list[int],
    penalty_factor: float = 6.0,
) -> nx.MultiDiGraph:
    """
    Return a copy of G where edges on `path` have inflated weights,
    encouraging the router to find an alternative return path.
    """
    H = G.copy()
    for u, v in zip(path[:-1], path[1:]):
        if H.has_edge(u, v):
            for key in H[u][v]:
                orig = H[u][v][key].get("length", 1.0)
                H[u][v][key]["length"] = orig * penalty_factor
        # Also penalise reverse direction
        if H.has_edge(v, u):
            for key in H[v][u]:
                orig = H[v][u][key].get("length", 1.0)
                H[v][u][key]["length"] = orig * penalty_factor
    return H


def _loop_ratio(outbound: list[int], return_path: list[int]) -> float:
    """
    Measure how 'loopy' the route is by counting shared nodes.
    1.0 = no overlap (perfect loop), 0.0 = complete overlap (out-and-back).
    """
    out_set = set(outbound)
    ret_set = set(return_path)
    overlap = len(out_set & ret_set)
    total   = len(out_set | ret_set)
    return 1.0 - (overlap / total) if total > 0 else 0.0


def generate_candidates(
    G: nx.MultiDiGraph,
    origin_node: int,
    target_miles: float,
    num_spokes: int = 8,
    tolerance: float = 0.20,
    min_loop_ratio: float = 0.15,
    shade_polys: list = None,
) -> list[CandidateLoop]:
    """
    Generate candidate loop routes for a given target distance.

    Parameters
    ----------
    G             : projected cycling road network
    origin_node   : starting node ID
    target_miles  : desired total loop distance
    num_spokes    : number of directional spokes to try
    tolerance     : ±fraction for turnaround node distance matching
    min_loop_ratio: discard loops more out-and-back than this threshold

    Returns
    -------
    List of CandidateLoop objects, sorted by loop_ratio descending.
    """
    half_dist_m = miles_to_meters(target_miles / 2)
    candidates: list[CandidateLoop] = []

    # Build a paved-biased copy of the graph once.
    G_paved = paved_weight_graph(G, unpaved_penalty=8.0, park_polys=shade_polys)

    # Pre-build shade spatial index once — reused for every candidate route.
    _shade_tree, _shade_transformer = build_shade_index(G, shade_polys or [])

    # Track edges used by already-accepted candidates so subsequent spokes are
    # forced onto different roads. This prevents all routes funnelling through
    # the same bottleneck (bridges, underpasses, narrow peninsulas).
    accepted_edges: set[tuple[int, int]] = set()
    DIVERSITY_PENALTY = 4.0   # multiplier on edges already used by a prior route

    def _apply_diversity_penalty(H: nx.MultiDiGraph) -> nx.MultiDiGraph:
        """Return H with accepted_edges penalised to encourage new geography."""
        if not accepted_edges:
            return H
        H2 = H.copy()
        for u, v in accepted_edges:
            for g in (H2, ):
                for direction in ((u, v), (v, u)):
                    a, b = direction
                    if g.has_edge(a, b):
                        for key in g[a][b]:
                            g[a][b][key]["length"] = g[a][b][key].get("length", 1.0) * DIVERSITY_PENALTY
        return H2

    # Find all nodes at roughly half the target distance
    midpoint_nodes = nodes_at_distance(
        G, origin_node, half_dist_m, tolerance_fraction=tolerance
    )

    if not midpoint_nodes:
        return []

    # Evenly-spaced bearings around the compass
    bearings = [i * (360 / num_spokes) for i in range(num_spokes)]

    for bearing in bearings:
        # Find the midpoint node closest to this bearing direction
        bearing_candidates = sorted(
            midpoint_nodes,
            key=lambda n: _angular_diff(bearing_between(G, origin_node, n), bearing),
        )

        # Apply diversity penalty on top of paved weights so this spoke avoids
        # roads already used by previously accepted candidates.
        G_diverse = _apply_diversity_penalty(G_paved)

        # Try the top-5 closest bearing matches to get a usable loop
        for turnaround in bearing_candidates[:5]:
            # ── Outbound: origin → turnaround (paved + diversity biased) ──
            outbound = shortest_path(G_diverse, origin_node, turnaround)
            if outbound is None or len(outbound) < 2:
                continue

            # Sanity-check the outbound geographic length.
            outbound_m = path_length_m(G, outbound)
            if outbound_m > miles_to_meters(target_miles * 1.2):
                outbound = shortest_path(G, origin_node, turnaround)
                if outbound is None or len(outbound) < 2:
                    continue

            # ── Return: turnaround → origin, avoiding outbound edges ──
            H = _penalise_used_edges(G_diverse, outbound)
            ret = shortest_path(H, turnaround, origin_node)
            if ret is None or len(ret) < 2:
                ret = shortest_path(G_diverse, turnaround, origin_node)
            if ret is None:
                continue

            # ── Stitch full loop ──
            full_path = outbound + ret[1:]

            # ── Measure actual geographic length ──
            total_m = path_length_m(G, full_path)
            total_miles = meters_to_miles(total_m)

            if not (target_miles * 0.45 <= total_miles <= target_miles * 2.20):
                continue

            lr = _loop_ratio(outbound, ret)
            if lr < min_loop_ratio:
                continue

            coords = path_coords(G, full_path[::10])
            paved = paved_fraction(G, full_path)
            shade = shade_fraction(G, full_path, shade_polys or [], _tree=_shade_tree, _transformer=_shade_transformer)

            candidates.append(CandidateLoop(
                path=full_path,
                length_miles=total_miles,
                outbound_path=outbound,
                return_path=ret,
                turnaround_node=turnaround,
                bearing_deg=bearing,
                target_miles=target_miles,
                coords=coords,
                paved_frac=paved,
                loop_ratio=lr,
                shade_frac=shade,
            ))

            # Record this route's edges so future spokes route around them
            for u, v in zip(full_path[:-1], full_path[1:]):
                accepted_edges.add((u, v))
                accepted_edges.add((v, u))

            break   # found a good loop for this spoke — move to next bearing

    # Sort best-shaped loops first
    candidates.sort(key=lambda c: c.loop_ratio, reverse=True)

    # Deduplicate: drop any candidate that shares >70% of its edges with a
    # higher-ranked candidate already in the kept list. This catches cases
    # where bottleneck networks (peninsulas, bridges) force multiple spokes
    # onto the same corridor despite the diversity penalty.
    kept: list[CandidateLoop] = []
    for cand in candidates:
        cand_edges = set(zip(cand.path[:-1], cand.path[1:]))
        duplicate = False
        for prior in kept:
            prior_edges = set(zip(prior.path[:-1], prior.path[1:]))
            overlap = len(cand_edges & prior_edges) / max(len(cand_edges), 1)
            if overlap > 0.70:
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)

    return kept