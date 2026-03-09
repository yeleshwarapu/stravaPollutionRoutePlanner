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


def _angular_diff(a: float, b: float) -> float:
    """Absolute angular difference between two bearings (0–180°)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _penalise_used_edges(
    G: nx.MultiDiGraph,
    path: list[int],
    penalty_factor: float = 20.0,
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
    tolerance: float = 0.15,
    min_loop_ratio: float = 0.25,
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

    # Build a paved-biased copy of the graph once — unpaved edges get 8× weight
    # so the router strongly prefers tarmac over dirt/gravel/tracks.
    G_paved = paved_weight_graph(G, unpaved_penalty=8.0)

    # Find all nodes at roughly half the target distance (use original lengths
    # for geographic distance, not the penalised weights)
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

        # Try the top-3 closest bearing matches to get a usable loop
        for turnaround in bearing_candidates[:3]:
            # ── Outbound: origin → turnaround (paved-biased) ──
            outbound = shortest_path(G_paved, origin_node, turnaround)
            if outbound is None or len(outbound) < 2:
                continue

            # ── Return: turnaround → origin, avoiding outbound edges ──
            H = _penalise_used_edges(G_paved, outbound)
            ret = shortest_path(H, turnaround, origin_node)
            if ret is None or len(ret) < 2:
                # Fall back to direct return on paved graph
                ret = shortest_path(G_paved, turnaround, origin_node)
            if ret is None:
                continue

            # ── Stitch full loop ──
            full_path = outbound + ret[1:]   # avoid duplicating turnaround node

            # ── Measure actual geographic length (original graph, not penalised) ──
            total_m = path_length_m(G, full_path)
            total_miles = meters_to_miles(total_m)

            # Reject if way off target
            if not (target_miles * 0.6 <= total_miles <= target_miles * 1.5):
                continue

            lr = _loop_ratio(outbound, ret)
            if lr < min_loop_ratio:
                continue

            # Coords for AQ sampling (use every 10th node to reduce API calls)
            coords = path_coords(G, full_path[::10])

            paved = paved_fraction(G, full_path)

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
            ))
            break   # found a good loop for this spoke — move to next bearing

    # Sort best-shaped loops first
    candidates.sort(key=lambda c: c.loop_ratio, reverse=True)
    return candidates