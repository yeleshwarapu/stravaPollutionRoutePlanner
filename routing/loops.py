"""
rcycle/routing/loops.py

Generates candidate loop cycling routes from an origin point.

Algorithm
---------
Multi-start greedy cycle construction — no spokes, no forced turnaround.

For each attempt:
  1. From origin, greedily extend the route one edge at a time.
  2. At each step, score all neighbouring edges by a composite of:
       - paved/cycling quality
       - shade coverage
       - exploration bonus (prefer unvisited edges)
       - distance-to-origin pull (increases as budget runs out)
  3. Sample from the top candidates with some randomness so different
     attempts produce genuinely different routes.
  4. When remaining budget is insufficient to extend further, close the
     loop by routing back to origin via shortest path on a penalised graph
     (penalised so the return leg uses different roads where possible).
  5. Keep routes that meet distance tolerance and loop quality thresholds.

Running many random seeds produces a diverse pool of natural-feeling routes
that emerge from the road network rather than being imposed on it.
"""

from __future__ import annotations
import math
import random
import numpy as np
import networkx as nx
from dataclasses import dataclass, field
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
    _is_edge_paved,
    _is_cycling_path,
)


@dataclass
class CandidateLoop:
    path: list[int]                    # ordered node IDs
    length_miles: float
    outbound_path: list[int]           # walk portion
    return_path: list[int]             # closing leg back to origin
    turnaround_node: int               # last node before return leg
    bearing_deg: float                 # overall bearing from origin to furthest point
    target_miles: float
    coords: list[tuple[float, float]]  # (lat, lon) sampled along route
    paved_frac: float = 0.0
    loop_ratio: float = 0.0
    shade_frac: float = 0.0


def _loop_ratio(outbound: list[int], return_path: list[int]) -> float:
    """1.0 = perfect loop (no overlap), 0.0 = complete out-and-back."""
    out_set = set(outbound)
    ret_set = set(return_path)
    overlap = len(out_set & ret_set)
    total   = len(out_set | ret_set)
    return 1.0 - (overlap / total) if total > 0 else 0.0


def _penalise_used_edges(
    G: nx.MultiDiGraph,
    path: list[int],
    penalty_factor: float = 5.0,
) -> nx.MultiDiGraph:
    H = G.copy()
    for u, v in zip(path[:-1], path[1:]):
        for direction in ((u, v), (v, u)):
            a, b = direction
            if H.has_edge(a, b):
                for key in H[a][b]:
                    H[a][b][key]["length"] = H[a][b][key].get("length", 1.0) * penalty_factor
    return H


def _edge_score(
    G: nx.MultiDiGraph,
    u: int,
    v: int,
    visited_edges: set,
    shade_tree,
    shade_transformer,
    shade_polys: list,
) -> float:
    """
    Score an edge u→v for greedy walk selection.
    Higher = more desirable. Returns -inf for impassable edges.
    """
    if not G.has_edge(u, v):
        return float("-inf")

    best = min(G[u][v].values(), key=lambda d: d.get("length", 1e9))
    length = best.get("length", 1.0)
    if length <= 0:
        return float("-inf")

    score = 0.0

    # ── Surface / cycling quality ─────────────────────────────────────────────
    if _is_cycling_path(best):
        score += 3.0          # designated cycling path — strongly prefer
    elif _is_edge_paved(best):
        score += 1.0          # paved road — baseline good
    else:
        score -= 2.0          # unpaved — avoid unless no alternative

    # ── Shade bonus ───────────────────────────────────────────────────────────
    if shade_tree is not None:
        try:
            from shapely.geometry import Point
            u_d, v_d = G.nodes[u], G.nodes[v]
            mx = (u_d["x"] + v_d["x"]) / 2
            my = (u_d["y"] + v_d["y"]) / 2
            if shade_transformer:
                lon, lat = shade_transformer.transform(mx, my)
            else:
                lon, lat = mx, my
            pt = Point(lon, lat)
            if shade_tree.query(pt, predicate="intersects").size > 0:
                score += 1.5
        except Exception:
            pass

    # ── Exploration bonus ─────────────────────────────────────────────────────
    edge_key = (min(u, v), max(u, v))
    if edge_key not in visited_edges:
        score += 2.0          # prefer unvisited roads

    # ── Avoid high-traffic roads ──────────────────────────────────────────────
    hw = best.get("highway", "")
    if isinstance(hw, list):
        hw = hw[0] if hw else ""
    hw = str(hw).lower()
    if hw in ("primary", "primary_link", "secondary", "secondary_link"):
        score -= 0.5
    if hw in ("motorway", "motorway_link", "trunk", "trunk_link"):
        score -= 5.0          # should already be filtered but just in case

    return score


def _greedy_walk(
    G: nx.MultiDiGraph,
    G_weighted: nx.MultiDiGraph,
    origin_node: int,
    target_m: float,
    seed: int,
    shade_tree,
    shade_transformer,
    shade_polys: list,
    top_k: int = 4,
    return_budget_frac: float = 0.55,
) -> Optional[CandidateLoop]:
    """
    Single greedy walk attempt from origin_node targeting target_m metres.

    return_budget_frac: when accumulated distance exceeds this fraction of
    target_m, start closing back toward origin.
    """
    rng = random.Random(seed)

    visited_edges: set[tuple[int, int]] = set()
    walk: list[int] = [origin_node]
    walk_dist_m: float = 0.0
    current = origin_node

    budget_m = target_m
    close_threshold = budget_m * return_budget_frac

    o_data = G.nodes[origin_node]
    ox_, oy_ = o_data["x"], o_data["y"]

    max_steps = int(budget_m / 30) + 500   # safety cap to prevent infinite loops

    for _ in range(max_steps):
        remaining = budget_m - walk_dist_m

        # Once we've used return_budget_frac of distance, start closing
        closing = walk_dist_m >= close_threshold

        # ── Score all outgoing edges ──────────────────────────────────────────
        neighbors = list(G.successors(current)) + list(G.predecessors(current))
        neighbors = list(set(neighbors) - {walk[-2]} if len(walk) >= 2 else set(neighbors))

        if not neighbors:
            break

        # Filter out edges that would overshoot badly
        candidates = []
        for nb in neighbors:
            if not G.has_edge(current, nb) and not G.has_edge(nb, current):
                continue
            edge_len = 0.0
            if G.has_edge(current, nb):
                edge_len = min(d.get("length", 1.0) for d in G[current][nb].values())
            elif G.has_edge(nb, current):
                edge_len = min(d.get("length", 1.0) for d in G[nb][current].values())

            if edge_len > remaining * 1.3:
                continue   # would massively overshoot

            base_score = _edge_score(
                G, current, nb, visited_edges,
                shade_tree, shade_transformer, shade_polys
            )
            if base_score == float("-inf"):
                base_score = _edge_score(
                    G, nb, current, visited_edges,
                    shade_tree, shade_transformer, shade_polys
                )
            if base_score == float("-inf"):
                continue

            # ── Homing pull toward origin ─────────────────────────────────────
            if closing:
                nb_data = G.nodes[nb]
                dist_to_origin = math.hypot(nb_data["x"] - ox_, nb_data["y"] - oy_)
                # Convert metres to a 0–3 bonus (closer = higher bonus)
                max_dist = budget_m * 0.6
                homing = max(0.0, 3.0 * (1.0 - dist_to_origin / max_dist))
                base_score += homing

            candidates.append((base_score, nb, edge_len))

        if not candidates:
            break

        # Sort by score, pick randomly from top_k
        candidates.sort(key=lambda x: x[0], reverse=True)
        pool = candidates[:top_k]
        scores = [max(0.01, s + 5.0) for s, _, _ in pool]  # shift to positive
        total_w = sum(scores)
        probs = [s / total_w for s in scores]

        chosen_idx = rng.choices(range(len(pool)), weights=probs, k=1)[0]
        _, next_node, edge_len = pool[chosen_idx]

        walk.append(next_node)
        walk_dist_m += edge_len
        edge_key = (min(current, next_node), max(current, next_node))
        visited_edges.add(edge_key)
        current = next_node

        # ── Try to close the loop ─────────────────────────────────────────────
        if walk_dist_m >= close_threshold:
            # Estimate if we have enough budget left to get back
            current_data = G.nodes[current]
            dist_to_origin_now = math.hypot(
                current_data["x"] - ox_, current_data["y"] - oy_
            )
            # If straight-line distance to origin × 1.4 fits in remaining budget, try closing
            if dist_to_origin_now * 1.4 <= remaining * 1.05:
                H_close = _penalise_used_edges(G_weighted, walk, penalty_factor=4.0)
                ret = shortest_path(H_close, current, origin_node)
                if ret is None:
                    ret = shortest_path(G, current, origin_node)
                if ret is not None:
                    ret_dist = path_length_m(G, ret)
                    total_dist = walk_dist_m + ret_dist
                    tol_lo = target_m * 0.70
                    tol_hi = target_m * 1.45
                    if tol_lo <= total_dist <= tol_hi:
                        # Good loop — accept it
                        full_path = walk + ret[1:]
                        outbound = walk
                        lr = _loop_ratio(outbound, ret)

                        # Compute overall bearing from origin to furthest point
                        furthest = max(walk, key=lambda n: math.hypot(
                            G.nodes[n]["x"] - ox_, G.nodes[n]["y"] - oy_
                        ))
                        bearing = bearing_between(G, origin_node, furthest)

                        coords = path_coords(G, full_path[::max(1, len(full_path) // 200)])
                        paved = paved_fraction(G, full_path)
                        shade = shade_fraction(
                            G, full_path, shade_polys,
                            _tree=shade_tree, _transformer=shade_transformer
                        )

                        return CandidateLoop(
                            path=full_path,
                            length_miles=meters_to_miles(total_dist),
                            outbound_path=outbound,
                            return_path=ret,
                            turnaround_node=current,
                            bearing_deg=bearing,
                            target_miles=meters_to_miles(target_m),
                            coords=coords,
                            paved_frac=paved,
                            loop_ratio=lr,
                            shade_frac=shade,
                        )

    # Walk ended without a valid close — try one last forced close
    if len(walk) >= 3 and current != origin_node:
        ret = shortest_path(G, current, origin_node)
        if ret is not None:
            ret_dist = path_length_m(G, ret)
            total_dist = walk_dist_m + ret_dist
            tol_lo = target_m * 0.60
            tol_hi = target_m * 1.60
            if tol_lo <= total_dist <= tol_hi:
                full_path = walk + ret[1:]
                outbound = walk
                lr = _loop_ratio(outbound, ret)
                furthest = max(walk, key=lambda n: math.hypot(
                    G.nodes[n]["x"] - ox_, G.nodes[n]["y"] - oy_
                ))
                bearing = bearing_between(G, origin_node, furthest)
                coords = path_coords(G, full_path[::max(1, len(full_path) // 200)])
                paved = paved_fraction(G, full_path)
                shade = shade_fraction(
                    G, full_path, shade_polys,
                    _tree=shade_tree, _transformer=shade_transformer
                )
                return CandidateLoop(
                    path=full_path,
                    length_miles=meters_to_miles(total_dist),
                    outbound_path=outbound,
                    return_path=ret,
                    turnaround_node=current,
                    bearing_deg=bearing,
                    target_miles=meters_to_miles(target_m),
                    coords=coords,
                    paved_frac=paved,
                    loop_ratio=lr,
                    shade_frac=shade,
                )

    return None


def generate_candidates(
    G: nx.MultiDiGraph,
    origin_node: int,
    target_miles: float,
    num_spokes: int = 8,          # repurposed as num_attempts
    tolerance: float = 0.20,      # kept for API compatibility, unused
    min_loop_ratio: float = 0.15,
    shade_polys: list = None,
) -> list[CandidateLoop]:
    """
    Generate candidate loop routes using multi-start greedy cycle construction.

    num_spokes is repurposed as the number of random walk attempts.
    More attempts = more diverse routes but slower.

    Returns a list of CandidateLoop objects, deduplicated and sorted by
    composite quality score.
    """
    target_m = miles_to_meters(target_miles)
    shade_polys = shade_polys or []

    G_weighted = paved_weight_graph(G, unpaved_penalty=8.0, park_polys=shade_polys)
    shade_tree, shade_transformer = build_shade_index(G, shade_polys)

    # Number of walk attempts — more spokes = more attempts = more variety
    num_attempts = max(num_spokes * 3, 24)

    candidates: list[CandidateLoop] = []

    for seed in range(num_attempts):
        try:
            result = _greedy_walk(
                G=G,
                G_weighted=G_weighted,
                origin_node=origin_node,
                target_m=target_m,
                seed=seed,
                shade_tree=shade_tree,
                shade_transformer=shade_transformer,
                shade_polys=shade_polys,
            )
            if result is not None and result.loop_ratio >= min_loop_ratio:
                candidates.append(result)
        except Exception:
            continue

    if not candidates:
        return []

    # ── Deduplicate: drop routes sharing >65% edge overlap with a better one ──
    def _composite_score(c: CandidateLoop) -> float:
        return c.loop_ratio * 0.4 + c.paved_frac * 0.3 + c.shade_frac * 0.3

    candidates.sort(key=_composite_score, reverse=True)

    kept: list[CandidateLoop] = []
    for cand in candidates:
        cand_edges = set(zip(cand.path[:-1], cand.path[1:]))
        duplicate = False
        for prior in kept:
            prior_edges = set(zip(prior.path[:-1], prior.path[1:]))
            overlap = len(cand_edges & prior_edges) / max(len(cand_edges), 1)
            if overlap > 0.65:
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)

    return kept