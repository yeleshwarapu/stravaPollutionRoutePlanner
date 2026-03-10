#!/usr/bin/env python3
"""
rcycle — R'Cycle Route Planner
================================
Generates cycling loop routes optimised for air quality and UV exposure
using real road network data (OpenStreetMap) and live environmental APIs
(Open-Meteo — no API key required).

Usage
-----
  # Basic: plan routes from default origin (UCR, Riverside CA)
  python main.py

  # Custom origin
  python main.py --lat 28.6139 --lon 77.2090

  # Specify distances (miles)
  python main.py --distances 5 10 15

  # Include your Strava export for stats context
  python main.py --strava /path/to/activities.csv

  # Run at a specific hour (for UV planning)
  python main.py --hour 7

  # Don't open browser automatically
  python main.py --no-browser

Full options: python main.py --help
"""

import argparse
import os
import sys
import webbrowser
import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    RICH = True
except ImportError:
    RICH = False


def _print(*args, **kwargs):
    print(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    from config import DEFAULT as _cfg
    p = argparse.ArgumentParser(
        prog="rcycle",
        description="Health-aware cycling route planner (air quality + UV)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--lat",        type=float, default=_cfg.origin_lat, help="Origin latitude")
    p.add_argument("--lon",        type=float, default=_cfg.origin_lon, help="Origin longitude")
    p.add_argument("--distances",  type=float, nargs="+", default=[5, 8, 12],
                   help="Target loop distances in miles (default: 5 8 12)")
    p.add_argument("--top",        type=int,   default=3,
                   help="Top N routes to show per distance (default: 3)")
    p.add_argument("--spokes",     type=int,   default=8,
                   help="Number of directional spokes to try (default: 8)")
    p.add_argument("--strava",     type=str,   default=None,
                   help="Path to Strava activities.csv export")
    p.add_argument("--hour",       type=int,   default=None,
                   help="Plan for this hour of day (0-23); default: now")
    p.add_argument("--output",     type=str,   default="output/routes.html",
                   help="Output HTML map path (default: output/routes.html)")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't open the map in a browser automatically")
    p.add_argument("--dev",        action="store_true",
                   help="Dev mode: 1 distance, 4 spokes, mock APIs — fast iteration")
    p.add_argument("--network",    type=str,   default="bike",
                   choices=["bike", "walk", "drive"],
                   help="OSM network type (default: bike)")
    return p


def main():
    args = build_parser().parse_args()

    console = Console() if RICH else None

    def info(msg):
        if console:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)

    def success(msg):
        if console:
            console.print(f"[green]{msg}[/green]")
        else:
            print(msg)

    def header(msg):
        if console:
            console.rule(f"[bold yellow]{msg}[/bold yellow]")
        else:
            print(f"\n{'─'*60}\n{msg}\n{'─'*60}")

    # ── Banner ────────────────────────────────────────────────────────────────
    header("R'Cycle — Health-Aware Route Planner")
    info(f"Origin     : {args.lat}, {args.lon}")
    info(f"Distances  : {args.distances} miles")
    info(f"Network    : {args.network}")
    info(f"Started    : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # ── Strava stats (optional) ───────────────────────────────────────────────
    from data.strava_loader import load as load_strava, summarise
    strava = load_strava(args.strava or "")
    if strava.num_rides > 0:
        header("Strava Data")
        print(summarise(strava))

    # ── Config ────────────────────────────────────────────────────────────────
    from config import Config
    cfg = Config(
        origin_lat=args.lat,
        origin_lon=args.lon,
        target_distances_miles=args.distances,
        num_spokes=args.spokes,
        top_routes_per_distance=args.top,
        network_type=args.network,
        output_dir=os.path.dirname(args.output) or "output",
        open_browser=not args.no_browser,
    )

    # ── Dev mode overrides ────────────────────────────────────────────────────
    if args.dev:
        args.distances = [args.distances[0]]  # only shortest distance
        args.spokes    = 4
        args.top       = 2
        os.environ["RCYCLE_DEV"] = "1"        # signals scorer to skip API calls
        info("[DEV] Mode active: 1 distance, 4 spokes, 2 routes, mock APIs")

    # ── UV best-window advisory ───────────────────────────────────────────────
    header("Environmental Conditions")
    from data.uv_data import best_window_today, get_current_uv, uv_category
    from data.air_quality import get_current_pm25, pm25_to_aqi_category

    info("Fetching UV index…")
    try:
        uv_now = get_current_uv(args.lat, args.lon)
        uv_lab, _ = uv_category(uv_now)
        uv_window = best_window_today(args.lat, args.lon)
        info(f"  UV now     : {uv_now:.1f} ({uv_lab})")
        info(f"  Best window: {uv_window[0]:02d}:00–{uv_window[1]:02d}:00 (lowest UV 4-hr block)")
    except Exception as e:
        info(f"  UV fetch failed: {e}")
        uv_window = None

    info("Fetching air quality…")
    try:
        pm25_now = get_current_pm25(args.lat, args.lon)
        aqi_lab, _ = pm25_to_aqi_category(pm25_now)
        info(f"  PM2.5 now  : {pm25_now:.1f} μg/m³ ({aqi_lab})")
    except Exception as e:
        info(f"  AQ fetch failed: {e}")

    # ── Road network download ─────────────────────────────────────────────────
    header("Road Network")
    max_dist = max(args.distances)
    info(f"Downloading {args.network} network within {max_dist * 0.75:.1f} mi of origin…")
    info("(Cached after first run — using OSMnx disk cache)")

    from routing.network import download_network, nearest_node, node_coords, download_shade_features
    import shutil, os as _os
    if not args.dev and _os.path.exists(".osmnx_cache"):
        shutil.rmtree(".osmnx_cache")
        info("Cleared OSMnx cache (ensures fresh coordinate data)")
    G = download_network(args.lat, args.lon, max_dist * 0.75, args.network)
    origin_node = nearest_node(G, args.lat, args.lon)
    # Use the actual snapped node position so the marker aligns with routes
    origin_lat, origin_lon = node_coords(G, origin_node)
    success(f"Network: {len(G.nodes):,} nodes, {len(G.edges):,} edges")

    info("Fetching tree cover / shade data…")
    try:
        shade_polys = download_shade_features(args.lat, args.lon, max_dist * 0.75)
        info(f"  Found {len(shade_polys)} shade features")
    except Exception as e:
        info(f"  Shade fetch failed: {e}")
        shade_polys = []

    # ── Generate + score routes ───────────────────────────────────────────────
    from routing.loops import generate_candidates
    from routing.scorer import score_all, ScoredRoute

    all_scored: list[ScoredRoute] = []

    for dist in sorted(args.distances):
        header(f"Generating {dist:.0f}-mile loops")
        info(f"Searching {args.spokes} directional spokes…")

        candidates = generate_candidates(
            G, origin_node, dist,
            num_spokes=args.spokes,
            shade_polys=shade_polys,
        )
        info(f"Found {len(candidates)} candidate loops")

        if not candidates:
            info(f"  No loops found for {dist} mi — try a different origin or larger distance.")
            continue

        info("Scoring (fetching live PM2.5 + UV along each route)…")
        scored = score_all(
            candidates, cfg, origin_lat, origin_lon,
            max_candidates=cfg.max_candidates,
            G=G,
        )
        top = scored[:args.top]
        all_scored.extend(top)

        # Print table
        if RICH:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow")
            table.add_column("#",        width=3)
            table.add_column("Grade",    width=7)
            table.add_column("Miles",    width=7)
            table.add_column("PM2.5",    width=22)
            table.add_column("UV",       width=16)
            table.add_column("Loop",     width=8)
            table.add_column("Paved",    width=8)
            table.add_column("Score",    width=7)
            for i, r in enumerate(top, 1):
                table.add_row(
                    str(i),
                    f"[bold]{r.grade()}[/bold]",
                    f"{r.length_miles:.1f}",
                    f"{r.aqi_label} ({r.pm25:.1f})",
                    f"{r.uv_label} ({r.uv:.1f})",
                    f"{r.loop.loop_ratio:.0%}",
                    f"{r.paved_frac:.0%}",
                    f"{r.score:.0%}",
                )
            console.print(table)
        else:
            for i, r in enumerate(top, 1):
                print(f"  {i}. {r.summary()}")

    if not all_scored:
        print("No routes generated. Check your origin coordinates and network type.")
        sys.exit(1)

    # ── Build map ─────────────────────────────────────────────────────────────
    header("Building Map")
    from viz.mapper import build_map

    abs_path = build_map(
        scored_routes=all_scored,
        origin_lat=args.lat,
        origin_lon=args.lon,
        G=G,
        output_path=args.output,
        uv_window=uv_window,
    )

    success(f"Map saved: {abs_path}")
    # ── Export best route per distance as GPX ────────────────────────────────
    from viz.gpx_export import export_best_routes
    gpx_files = export_best_routes(
        all_scored=all_scored,
        G=G,
        output_dir=os.path.dirname(args.output) or "output",
    )
    for gf in gpx_files:
        success(f"GPX saved: {gf}")

    if cfg.open_browser:
        info("Opening in browser…")
        webbrowser.open(f"file://{abs_path}")

    header("Done")
    if all_scored:
        best = all_scored[0]
        if console:
            console.print(Panel(
                f"[bold green]Best route: Grade {best.grade()} · {best.length_miles:.1f} mi[/bold green]\n"
                f"AQ: {best.aqi_label} ({best.pm25:.1f} μg/m³)  "
                f"UV: {best.uv_label} ({best.uv:.1f})  "
                f"Score: {best.score:.0%}",
                title="[yellow]Recommendation[/yellow]",
                border_style="yellow",
            ))
        else:
            print(f"\nBest route: {best.summary()}")


if __name__ == "__main__":
    main()