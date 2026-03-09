"""
rcycle/routing/scorer.py

Scores candidate loop routes on health and quality metrics:
  - PM2.5 air quality (EPA breakpoints)
  - UV index exposure
  - Loop shape quality (vs. out-and-back)
  - Paved / bike-friendly surface fraction

Final score: 0.0 (worst) → 1.0 (best)

AQ and UV are sampled at the 25% path-distance point of each route —
the point where a loop rider is genuinely furthest out in a new environment,
rather than at the shared origin (which made all scores identical).

Dev mode
--------
Set the environment variable RCYCLE_DEV=1 to skip live API calls and use
fixed stub values instead — makes iteration near-instant.
"""

from __future__ import annotations
import math
import os
from dataclasses import dataclass

from routing.loops import CandidateLoop
from data.air_quality import get_route_pm25, normalise_pm25, pm25_to_aqi_category
from data.uv_data import get_current_uv, normalise_uv, uv_category
from config import Config, DEFAULT

# Set RCYCLE_DEV=1 (or pass --dev via main.py which sets this) to skip API calls
DEV_MODE: bool = os.environ.get("RCYCLE_DEV", "0") == "1"

_DEV_PM25 = 8.0    # "Good" air quality stub
_DEV_UV   = 3.5    # "Moderate" UV stub


@dataclass
class ScoredRoute:
    loop: CandidateLoop
    score: float            # 0–1, higher = healthier
    pm25: float             # μg/m³
    uv: float               # UV index
    aqi_label: str
    aqi_colour: str
    uv_label: str
    uv_colour: str
    score_breakdown: dict   # component scores before weighting

    @property
    def length_miles(self) -> float:
        return self.loop.length_miles

    @property
    def bearing_deg(self) -> float:
        return self.loop.bearing_deg

    @property
    def loop_ratio(self) -> float:
        return self.loop.loop_ratio

    @property
    def paved_frac(self) -> float:
        return self.loop.paved_frac

    @property
    def coords(self) -> list:
        return self.loop.coords

    @property
    def path(self) -> list:
        return self.loop.path

    def grade(self) -> str:
        """Letter grade A–F for the overall route health score."""
        if self.score >= 0.85: return "A"
        if self.score >= 0.70: return "B"
        if self.score >= 0.55: return "C"
        if self.score >= 0.40: return "D"
        return "F"

    def summary(self) -> str:
        direction = _bearing_to_compass(self.bearing_deg)
        return (
            f"Grade {self.grade()} | {self.length_miles:.1f} mi | "
            f"AQ: {self.aqi_label} ({self.pm25:.1f} μg/m³) | "
            f"UV: {self.uv_label} ({self.uv:.1f}) | "
            f"Loop: {self.loop.loop_ratio:.0%} | "
            f"Paved: {self.paved_frac:.0%} | "
            f"Heads {direction}"
        )


def _bearing_to_compass(deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(deg / 45) % 8
    return dirs[idx]


def _quarter_point_coords(loop: CandidateLoop, G) -> tuple[float, float]:
    """
    Walk the route by cumulative edge length and return the lat/lon of the
    node closest to 25% of total path distance — the point where a loop
    rider is genuinely furthest out before turning back.

    Falls back to the first coordinate pair if the graph walk fails.
    """
    nodes = loop.path  # list of OSMnx node IDs

    if not nodes or G is None:
        # fall back to first coord in the coords list
        if loop.coords:
            return loop.coords[0]
        return (loop.coords[0][0], loop.coords[0][1])

    # compute total route length from edge data
    total_length = 0.0
    edge_lengths = []
    for u, v in zip(nodes, nodes[1:]):
        edge_data = G.get_edge_data(u, v)
        if edge_data:
            length = edge_data[0].get("length", 0.0)
        else:
            length = 0.0
        edge_lengths.append(length)
        total_length += length

    if total_length == 0:
        if loop.coords:
            return loop.coords[0]
        return (G.nodes[nodes[0]]["y"], G.nodes[nodes[0]]["x"])

    quarter = total_length * 0.25
    cumulative = 0.0
    sample_node = nodes[0]

    for i, (u, v) in enumerate(zip(nodes, nodes[1:])):
        cumulative += edge_lengths[i]
        if cumulative >= quarter:
            sample_node = v
            break

    return (G.nodes[sample_node]["y"], G.nodes[sample_node]["x"])


def score_route(
    loop: CandidateLoop,
    cfg: Config = DEFAULT,
    origin_lat: float = None,
    origin_lon: float = None,
    G=None,                     # OSMnx graph — needed for quarter-point sampling
    _cached_uv: float = None,   # ignored — UV now sampled per-route
) -> ScoredRoute:
    """
    Score a single CandidateLoop.

    PM2.5 and UV are fetched at the 25% path-distance node of each route so
    that routes heading in different directions get genuinely different
    environmental scores.

    In DEV_MODE (RCYCLE_DEV=1) stub values are used instead of API calls.
    """
    lat = origin_lat or cfg.origin_lat
    lon = origin_lon or cfg.origin_lon

    # ── Sample point: 25% along the route path ───────────────────────────────
    if G is not None and not DEV_MODE:
        try:
            sample_lat, sample_lon = _quarter_point_coords(loop, G)
        except Exception:
            sample_lat, sample_lon = lat, lon
    else:
        sample_lat, sample_lon = lat, lon

    # ── Air quality ──────────────────────────────────────────────────────────
    if DEV_MODE:
        pm25 = _DEV_PM25
    else:
        pm25 = get_route_pm25([(sample_lat, sample_lon)], sample_every_n=1)

    pm25_norm = normalise_pm25(pm25, ceiling=cfg.pm25_unhealthy)
    aqi_label, aqi_colour = pm25_to_aqi_category(pm25)

    # ── UV index — sampled at same quarter-point ──────────────────────────────
    if DEV_MODE:
        uv = _DEV_UV
    else:
        try:
            uv = get_current_uv(sample_lat, sample_lon)
        except Exception:
            uv = get_current_uv(lat, lon)

    uv_norm = normalise_uv(uv, ceiling=cfg.uv_very_high)
    uv_lab, uv_col = uv_category(uv)

    # ── Structural scores ─────────────────────────────────────────────────────
    loop_score  = loop.loop_ratio   # already 0–1
    paved_score = loop.paved_frac   # already 0–1

    # Component scores (higher = better)
    aq_score = 1.0 - pm25_norm
    uv_score = 1.0 - uv_norm

    breakdown = {
        "air_quality": round(aq_score, 3),
        "uv":          round(uv_score, 3),
        "loop_shape":  round(loop_score, 3),
        "paved":       round(paved_score, 3),
    }

    # ── Weighted final score ──────────────────────────────────────────────────
    final = (
        cfg.weight_pm25  * aq_score   +
        cfg.weight_uv    * uv_score   +
        cfg.weight_loop  * loop_score +
        cfg.weight_paved * paved_score
    )

    return ScoredRoute(
        loop=loop,
        score=round(final, 4),
        pm25=pm25,
        uv=uv,
        aqi_label=aqi_label,
        aqi_colour=aqi_colour,
        uv_label=uv_lab,
        uv_colour=uv_col,
        score_breakdown=breakdown,
    )


def score_all(
    loops: list[CandidateLoop],
    cfg: Config = DEFAULT,
    origin_lat: float = None,
    origin_lon: float = None,
    max_candidates: int = None,
    G=None,                     # OSMnx graph — pass through for quarter-point sampling
) -> list[ScoredRoute]:
    """
    Score a list of candidate loops, return sorted best-first.
    Limits API calls by capping at max_candidates before scoring.
    Each route is sampled at its own 25% path-distance point so that
    routes in different directions receive genuinely different AQ/UV scores.
    """
    cap = max_candidates or cfg.max_candidates
    subset = loops[:cap]

    lat = origin_lat or cfg.origin_lat
    lon = origin_lon or cfg.origin_lon

    scored = []
    for loop in subset:
        try:
            sr = score_route(loop, cfg, lat, lon, G=G)
            scored.append(sr)
        except Exception as e:
            print(f"  [scorer] Warning: skipping route — {e}")

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored