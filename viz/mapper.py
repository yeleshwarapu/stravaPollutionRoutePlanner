"""
rcycle/viz/mapper.py

Builds a self-contained interactive HTML map using Leaflet.js directly.
Features:
  - All routes always visible; selected = full opacity, others = dimmed
  - Click a card → smooth animated polyline draw
  - Shade/tree cover overlay (green polygons from OSM, always on)
  - Live AQ + UV badges
  - Dark theme matching R'Cycle portfolio aesthetic
"""

from __future__ import annotations
import os
import json
import networkx as nx

from routing.scorer import ScoredRoute


GRADE_COLOUR = {
    "A": "#00e400",
    "B": "#a3d977",
    "C": "#ffff00",
    "D": "#ff7e00",
    "F": "#ff0000",
}


def _bearing_label(deg: float) -> str:
    dirs = ["North", "NE", "East", "SE", "South", "SW", "West", "NW"]
    return dirs[round(deg / 45) % 8]


def _get_geo_coords(G: nx.MultiDiGraph, path: list[int]) -> list[list[float]]:
    """
    Extract [lat, lon] points for a path, using edge geometry where available
    so polylines hug actual road curves instead of cutting straight across.
    """
    import pyproj

    if not path:
        return []

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
            return [float(lat), float(lon)]
        return [float(y), float(x)]

    def _node_latlon(node_id):
        d = G.nodes[node_id]
        if "lat" in d and "lon" in d:
            return [float(d["lat"]), float(d["lon"])]
        return _proj_to_latlon(d["x"], d["y"])

    coords = []
    for u, v in zip(path[:-1], path[1:]):
        if not G.has_edge(u, v):
            coords.append(_node_latlon(u))
            continue
        edge_data = G[u][v]
        best = min(edge_data.values(), key=lambda d: d.get("length", 1e9))
        geom = best.get("geometry")

        if geom is not None:
            pts = list(geom.coords)
            u_data = G.nodes[u]
            ux, uy = u_data["x"], u_data["y"]
            if pts and abs(pts[0][0] - ux) > abs(pts[-1][0] - ux):
                pts = pts[::-1]
            for x, y in pts[:-1]:
                coords.append(_proj_to_latlon(x, y))
        else:
            coords.append(_node_latlon(u))

    coords.append(_node_latlon(path[-1]))
    return coords


def build_map(
    scored_routes: list[ScoredRoute],
    origin_lat: float,
    origin_lon: float,
    G: nx.MultiDiGraph,
    output_path: str = "output/routes.html",
    title: str = "R'Cycle — Route Planner",
    uv_window: tuple | None = None,
    shade_geojson: list = None,
) -> str:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    routes_data = []
    for i, route in enumerate(scored_routes):
        coords = _get_geo_coords(G, route.path)
        if len(coords) < 2:
            coords = [[c[0], c[1]] for c in route.coords]
        routes_data.append({
            "rank":         i + 1,
            "grade":        route.grade(),
            "colour":       GRADE_COLOUR.get(route.grade(), "#aaa"),
            "miles":        round(route.length_miles, 1),
            "pm25":         round(route.pm25, 1),
            "aqi_label":    route.aqi_label,
            "aqi_colour":   route.aqi_colour,
            "ozone":        round(route.ozone, 1),
            "ozone_label":  route.ozone_label,
            "ozone_colour": route.ozone_colour,
            "uv":           round(route.uv, 1),
            "uv_label":     route.uv_label,
            "uv_colour":    route.uv_colour,
            "paved":        round(route.paved_frac * 100),
            "loop":         round(route.loop_ratio * 100),
            "score":        round(route.score * 100),
            "direction":    _bearing_label(route.bearing_deg),
            "target_miles": route.loop.target_miles,
            "coords":       coords,
        })

    uv_win_str = f"{uv_window[0]:02d}:00\u2013{uv_window[1]:02d}:00" if uv_window else ""

    routes_json = json.dumps(routes_data)
    shade_json  = json.dumps(shade_geojson or [])

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>""" + title + """</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
/* Suppress native cursor inside iframe so the parent's custom cursor shows */
html,body,#map,#app,#sidebar{cursor:none!important}
:root{--bg:#0d0d0d;--bg2:#141414;--bg3:#1c1c1c;--amber:#E8A43E;--teal:#3EC8C8;--text:#f0ece4;--muted:#6b6560;--border:#2a2a2a;}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'DM Mono',monospace;overflow:hidden}
#app{display:flex;height:100vh}
#sidebar{width:380px;min-width:380px;height:100%;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;z-index:10}
#map{flex:1;height:100%}
#header{padding:24px 24px 18px;border-bottom:1px solid var(--border);flex-shrink:0}
#header h1{font-family:'Syne',sans-serif;font-size:16px;font-weight:800;letter-spacing:.15em;text-transform:uppercase;color:var(--amber);margin-bottom:5px}
#header .sub{font-size:10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
#env-bar{display:flex;gap:8px;padding:14px 24px;border-bottom:1px solid var(--border);flex-shrink:0}
.env-badge{flex:1;background:var(--bg3);border:1px solid var(--border);padding:10px 12px;font-size:9px;letter-spacing:.08em;text-transform:uppercase}
.env-badge .val{font-size:13px;font-weight:500;margin-top:5px;white-space:nowrap}
#dist-tabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.dist-tab{flex:1;padding:13px 4px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;text-align:center;cursor:none;color:var(--muted);border-bottom:2px solid transparent;transition:all .2s}
.dist-tab:hover{color:var(--text)}
.dist-tab.active{color:var(--amber);border-bottom-color:var(--amber)}
#route-list{flex:1;min-height:0;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
#route-list::-webkit-scrollbar{width:4px}
#route-list::-webkit-scrollbar-thumb{background:var(--border)}
.route-card{background:var(--bg3);border:1px solid var(--border);padding:20px 20px 20px 23px;cursor:none;transition:border-color .2s,background .2s;position:relative;animation:fadeUp .3s ease both;flex-shrink:0}
.route-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--card-colour,#333)}
.route-card:hover{border-color:#3a3a3a;background:#1f1f1f}
.route-card.active{border-color:var(--card-colour,var(--amber));background:#1a1a1a}
.card-top{display:flex;align-items:center;gap:14px;margin-bottom:16px}
.grade-badge{width:44px;height:44px;display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-size:22px;font-weight:800;flex-shrink:0;color:#0d0d0d}
.card-title{flex:1}
.card-title .name{font-size:13px;font-weight:500;margin-bottom:4px}
.card-title .meta{font-size:11px;color:var(--muted)}
.score-num{text-align:right;font-size:26px;font-family:'Syne',sans-serif;font-weight:800;color:var(--card-colour,#aaa);flex-shrink:0}
.score-num .slabel{font-size:9px;color:var(--muted);font-family:'DM Mono',monospace;font-weight:400;letter-spacing:.1em;text-transform:uppercase}
.card-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.metric{background:var(--bg2);padding:10px 12px}
.metric .ml{font-size:9px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px}
.metric .mv{font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
#footer{padding:14px 24px;border-top:1px solid var(--border);font-size:9px;color:var(--muted);letter-spacing:.08em;flex-shrink:0;line-height:1.8}
.uv-win{color:var(--teal);margin-top:6px}
.leaflet-container{background:#0d0d0d}
.leaflet-control-zoom{border:1px solid var(--border)!important;background:var(--bg2)!important}
.leaflet-control-zoom a{background:var(--bg2)!important;color:var(--text)!important;border-color:var(--border)!important}
.leaflet-control-zoom a:hover{background:var(--bg3)!important}
.leaflet-control-attribution{background:rgba(13,13,13,.8)!important;color:var(--muted)!important;font-size:9px!important}
.leaflet-control-attribution a{color:var(--muted)!important}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <div id="header">
      <h1>R&#8217;Cycle Co-Op</h1>
      <div class="sub">Routes ranked by health score</div>
    </div>
    <div id="env-bar">
      <div class="env-badge"><div>Air Quality</div><div class="val" id="aq-val">&#8212;</div></div>
      <div class="env-badge"><div>Ozone</div><div class="val" id="ozone-val">&#8212;</div></div>
      <div class="env-badge"><div>UV Index</div><div class="val" id="uv-val">&#8212;</div></div>
      <div class="env-badge"><div>Best Window</div><div class="val" style="font-size:11px" id="win-val">""" + (uv_win_str or "&#8212;") + """</div></div>
    </div>
    <div id="dist-tabs"></div>
    <div id="route-list"></div>
    <div id="footer">
      Scored: PM2.5 (20%) &middot; Ozone (10%) &middot; UV (20%) &middot; Shade (25%) &middot; Loop (15%) &middot; Paved (10%)
      """ + (f'<div class="uv-win">&#9889; Best UV window: {uv_win_str}</div>' if uv_win_str else "") + """
    </div>
  </div>
  <div id="map"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const ROUTES      = """ + routes_json + """;
const ORIGIN      = [""" + str(origin_lat) + """, """ + str(origin_lon) + """];
const SHADE_GEOMS = """ + shade_json + """;

const map = L.map('map', {center: ORIGIN, zoom: 13});
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; CARTO', subdomains: 'abcd', maxZoom: 19
}).addTo(map);

// Origin marker
const oIcon = L.divIcon({
  html: '<div style="width:14px;height:14px;background:#E8A43E;border-radius:50%;border:2px solid #0d0d0d;box-shadow:0 0 0 4px rgba(232,164,62,.25)"></div>',
  iconSize:[14,14], iconAnchor:[7,7], className:''
});
L.marker(ORIGIN, {icon: oIcon}).bindTooltip('Start / Finish').addTo(map);

// ── Shade overlay ────────────────────────────────────────────────────────────
let shadeLayer = null;
if (SHADE_GEOMS.length > 0) {
  const fc = {type:'FeatureCollection', features: SHADE_GEOMS.map(g => ({type:'Feature',geometry:g,properties:{}}))};
  shadeLayer = L.geoJSON(fc, {
    style: {color:'#4ade80', weight:0, fillColor:'#4ade80', fillOpacity:0.13},
    pointToLayer: (f, ll) => L.circleMarker(ll, {radius:4, color:'#4ade80', fillOpacity:0.3, weight:0}),
  }).addTo(map);
}

// ── State ────────────────────────────────────────────────────────────────────
let activeLayer = null, bgLayers = [], activeRank = null, animTimer = null;

const byDist = {};
ROUTES.forEach(r => {
  const k = r.target_miles + ' mi';
  if (!byDist[k]) byDist[k] = [];
  byDist[k].push(r);
});
const distKeys = Object.keys(byDist).sort((a,b) => parseFloat(a) - parseFloat(b));
let activeDist = distKeys[0];

// Env badges
if (ROUTES.length > 0) {
  const r = ROUTES[0];
  const aqEl = document.getElementById('aq-val');
  aqEl.textContent = r.aqi_label; aqEl.style.color = r.aqi_colour;
  const ozEl = document.getElementById('ozone-val');
  if (ozEl) { ozEl.textContent = r.ozone_label; ozEl.style.color = r.ozone_colour; }
  const uvEl = document.getElementById('uv-val');
  uvEl.textContent = r.uv_label + ' (' + r.uv + ')'; uvEl.style.color = r.uv_colour;
}

// Distance tabs
const tabsEl = document.getElementById('dist-tabs');
distKeys.forEach(key => {
  const t = document.createElement('div');
  t.className = 'dist-tab' + (key === activeDist ? ' active' : '');
  t.textContent = key;
  t.onclick = () => {
    activeDist = key; activeRank = null;
    document.querySelectorAll('.dist-tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    renderCards(); clearRoutes(); drawAllRoutes(key, true);
  };
  tabsEl.appendChild(t);
});

function renderCards() {
  const list = document.getElementById('route-list');
  list.innerHTML = '';
  (byDist[activeDist] || []).forEach((r, i) => {
    const card = document.createElement('div');
    card.className = 'route-card' + (activeRank === r.rank ? ' active' : '');
    card.style.setProperty('--card-colour', r.colour);
    card.style.animationDelay = (i * 0.07) + 's';
    card.innerHTML =
      '<div class="card-top">' +
        '<div class="grade-badge" style="background:' + r.colour + '">' + r.grade + '</div>' +
        '<div class="card-title">' +
          '<div class="name">Route ' + (i+1) + ' &middot; ' + r.direction + '</div>' +
          '<div class="meta">' + r.miles + ' mi loop</div>' +
        '</div>' +
        '<div class="score-num">' + r.score + '%<div class="slabel">score</div></div>' +
      '</div>' +
      '<div class="card-metrics">' +
        '<div class="metric"><div class="ml">Air Quality</div><div class="mv"><span class="dot" style="background:' + r.aqi_colour + '"></span>' + r.aqi_label + '</div></div>' +
        '<div class="metric"><div class="ml">PM2.5</div><div class="mv">' + r.pm25 + ' μg/m³</div></div>' +
        '<div class="metric"><div class="ml">Ozone</div><div class="mv"><span class="dot" style="background:' + r.ozone_colour + '"></span>' + r.ozone_label + '</div></div>' +
        '<div class="metric"><div class="ml">Ozone (μg/m³)</div><div class="mv">' + r.ozone + '</div></div>' +
        '<div class="metric"><div class="ml">UV Index</div><div class="mv"><span class="dot" style="background:' + r.uv_colour + '"></span>' + r.uv_label + '</div></div>' +
        '<div class="metric"><div class="ml">Paved / Loop</div><div class="mv">' + r.paved + '% / ' + r.loop + '%</div></div>' +
      '</div>';
    card.onclick = () => activateRoute(r, card);
    list.appendChild(card);
  });
}

function clearRoutes() {
  if (activeLayer) { map.removeLayer(activeLayer); activeLayer = null; }
  bgLayers.forEach(l => map.removeLayer(l)); bgLayers = [];
  if (animTimer) { clearInterval(animTimer); animTimer = null; }
}

// Draw all routes for a distance tab at their base opacity.
// The selected route is drawn full weight; others are dimmed.
function drawAllRoutes(distKey, fit) {
  const routes = byDist[distKey] || [];
  let bounds = null;
  routes.forEach(r => {
    if (!r.coords || r.coords.length < 2) return;
    const isActive = r.rank === activeRank;
    const l = L.polyline(r.coords, {
      color:   r.colour,
      weight:  isActive ? 5 : 3,
      opacity: isActive ? 0.95 : 0.35,
    }).addTo(map);
    l.bindTooltip('Route &middot; ' + r.grade + ' &middot; ' + r.score + '%', {sticky: true});
    if (isActive) activeLayer = l;
    else bgLayers.push(l);
    if (!bounds) bounds = l.getBounds(); else bounds.extend(l.getBounds());
  });
  if (fit && bounds) map.fitBounds(bounds, {padding:[50,50]});
}

function activateRoute(r, cardEl) {
  document.querySelectorAll('.route-card').forEach(c => c.classList.remove('active'));
  cardEl.classList.add('active');
  activeRank = r.rank;
  clearRoutes();

  // Redraw all routes with updated opacity
  drawAllRoutes(activeDist, false);

  // Fit to selected route
  if (r.coords && r.coords.length > 1)
    map.fitBounds(L.polyline(r.coords).getBounds(), {padding:[60,60]});

  // Animate draw on top of the static line
  if (activeLayer) { map.removeLayer(activeLayer); activeLayer = null; }
  const drawn = [];
  const line = L.polyline([], {color: r.colour, weight: 5, opacity: 0.95}).addTo(map);
  activeLayer = line;
  const total = r.coords.length;
  const step = Math.max(1, Math.floor(total / 100));
  let idx = 0;
  animTimer = setInterval(() => {
    const end = Math.min(idx + step, total);
    for (let i = idx; i < end; i++) drawn.push(r.coords[i]);
    line.setLatLngs(drawn);
    idx = end;
    if (idx >= total) { clearInterval(animTimer); animTimer = null; }
  }, 16);
}

// Init — draw all routes then activate the first
renderCards();
drawAllRoutes(activeDist, true);
setTimeout(() => {
  const first = document.querySelector('.route-card');
  const firstRoute = (byDist[activeDist] || [])[0];
  if (first && firstRoute) activateRoute(firstRoute, first);
}, 700);

// Relay mouse position to parent so the custom cursor keeps tracking over the map.
// The iframe is a separate document — parent mousemove stops firing here, so
// we forward coordinates (offset by the iframe's own position) back up.
document.addEventListener('mousemove', e => {
  window.parent.postMessage({ type: 'mousemove', x: e.clientX, y: e.clientY }, '*');
});
document.addEventListener('mouseenter', () => {
  window.parent.postMessage({ type: 'mouseenter' }, '*');
});
document.addEventListener('mouseleave', () => {
  window.parent.postMessage({ type: 'mouseleave' }, '*');
});

window.addEventListener('message', e => {
  if (!e.data) return;

  // Route card activation from sidebar card click
  if (e.data.type === 'activateRoute') {
    const routes = byDist[activeDist] || [];
    const route = routes.find(r => r.rank === e.data.rank);
    if (!route) return;
    const cards = document.querySelectorAll('.route-card');
    const idx = routes.indexOf(route);
    if (idx >= 0 && cards[idx]) activateRoute(route, cards[idx]);
  }

  // Keyboard relay — parent forwards arrow/zoom keys when map isn't focused
  if (e.data.type === 'keydown') {
    const PAN = 80;
    switch (e.data.key) {
      case 'ArrowUp':    map.panBy([0, -PAN]); break;
      case 'ArrowDown':  map.panBy([0,  PAN]); break;
      case 'ArrowLeft':  map.panBy([-PAN, 0]); break;
      case 'ArrowRight': map.panBy([ PAN, 0]); break;
      case '+': case '=': map.zoomIn();  break;
      case '-': case '_': map.zoomOut(); break;
      case 'PageUp':   map.zoomIn();  break;
      case 'PageDown': map.zoomOut(); break;
    }
  }
});
</script>
</body>
</html>"""

    abs_path = os.path.abspath(output_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(html)
    return abs_path