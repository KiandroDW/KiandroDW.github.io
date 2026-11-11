#!/usr/bin/env python3
"""
Interactive editor to simplify the Belgian OSM rail network to ONE line per corridor.

Usage:
    python rail_editor.py                # fetch (first time) + start editor on http://localhost:8765
    python rail_editor.py --refresh      # re-download from Overpass (edit IDs may change!)
    python rail_editor.py --port 9000 --no-browser

How it works:
  * Downloads your Overpass query once and caches it (rail_be.json).
  * Builds a graph (OSM nodes + consecutive-node edges) and contracts it into "chains":
    paths between junction/end nodes (nodes with degree != 2). One click = one chain.
  * Browser editor (Leaflet): delete chains (parallel tracks), add straight connections
    between any two OSM nodes (snaps to nearest node). Autosaves to edits.json.
  * Export: applies your edits, re-merges chains that became plain pass-through nodes and
    writes out/simplified.geojson and out/graph.json (nodes + edges, ready for routing).

Only needs the Python standard library (Leaflet + map tiles load from the internet in the browser).
"""
import argparse
import json
import math
import os
import shutil
import threading
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUERY = """[out:json][timeout:600];
area["ISO3166-1"="BE"][admin_level=2]->.be;
way(area.be)["railway"="rail"]
  ["service"!~"^(yard|siding|spur|crossover)$"]
  ["usage"!~"^(industrial|military|tourism)$"];
out geom;"""

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


# --------------------------------------------------------------------------- data
def fetch_overpass(cache):
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            print(f"Downloading from {url} (can take a minute)...")
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "be-rail-editor/1.0"})
            with urllib.request.urlopen(req, timeout=700) as r:
                raw = r.read()
            data = json.loads(raw)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        except Exception as e:  # noqa
            last = e
            print(f"  failed: {e}")
    raise SystemExit(f"All Overpass endpoints failed: {last}")


def build_graph(data):
    coords, adj = {}, defaultdict(set)
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        nodes, geom = el.get("nodes", []), el.get("geometry", [])
        if len(nodes) != len(geom):
            continue
        for nid, g in zip(nodes, geom):
            if g:
                coords[nid] = (g["lat"], g["lon"])
        for a, b in zip(nodes, nodes[1:]):
            if a != b and a in coords and b in coords:
                adj[a].add(b)
                adj[b].add(a)
    return coords, adj


def build_chains(coords, adj):
    key = {n for n, nb in adj.items() if len(nb) != 2}
    seen = set()
    chains = []

    def edge(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start, nxt):
        seen.add(edge(start, nxt))
        path = [start, nxt]
        prev, cur = start, nxt
        while cur not in key:
            other = next((n for n in sorted(adj[cur]) if n != prev), None)
            if other is None or edge(cur, other) in seen:
                break
            seen.add(edge(cur, other))
            path.append(other)
            prev, cur = cur, other
        return path

    for k in sorted(key):
        for n in sorted(adj[k]):
            if edge(k, n) not in seen:
                chains.append(walk(k, n))
    for k in sorted(adj):  # pure loops without junctions
        for n in sorted(adj[k]):
            if edge(k, n) not in seen:
                chains.append(walk(k, n))

    out = []
    for path in chains:
        out.append({
            "id": f"{path[0]}-{path[1]}",
            "a": path[0],
            "b": path[-1],
            "n": path,
            "p": [[round(coords[n][0], 6), round(coords[n][1], 6)] for n in path],
        })
    return out


CELL = 0.002


class NodeIndex:
    def __init__(self, coords):
        self.coords = coords
        self.grid = defaultdict(list)
        for nid, (la, lo) in coords.items():
            self.grid[(int(la // CELL), int(lo // CELL))].append(nid)

    def nearest(self, lat, lon):
        cy, cx = int(lat // CELL), int(lon // CELL)
        kx = math.cos(math.radians(lat))
        best = None
        for r in range(0, 9):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dy), abs(dx)) != r:
                        continue
                    for nid in self.grid.get((cy + dy, cx + dx), ()):
                        la, lo = self.coords[nid]
                        d = (la - lat) ** 2 + ((lo - lon) * kx) ** 2
                        if best is None or d < best[0]:
                            best = (d, nid)
            if best and best[0] <= (r * CELL * kx) ** 2:
                break
        if not best:
            return None
        la, lo = self.coords[best[1]]
        return {"id": best[1], "lat": la, "lon": lo}


# --------------------------------------------------------------------------- export
def haversine(p, q):
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def merge_degree2(edges):
    """Merge edges that meet at a node with exactly two incident edges."""
    edges = {i: dict(e) for i, e in enumerate(edges)}
    inc = defaultdict(set)
    for i, e in edges.items():
        inc[e["a"]].add(i)
        inc[e["b"]].add(i)
    nxt = len(edges)
    for n in list(inc):
        ids = inc[n]
        if len(ids) != 2:
            continue
        i, j = tuple(ids)
        e1, e2 = edges[i], edges[j]
        old = {e1["a"], e1["b"], e2["a"], e2["b"]}
        if e1["a"] == n:
            e1 = {"a": e1["b"], "b": e1["a"], "p": e1["p"][::-1]}
        if e2["b"] == n:
            e2 = {"a": e2["b"], "b": e2["a"], "p": e2["p"][::-1]}
        new = {"a": e1["a"], "b": e2["b"], "p": e1["p"] + e2["p"][1:]}
        del edges[i], edges[j]
        for o in old:
            inc[o].discard(i)
            inc[o].discard(j)
        edges[nxt] = new
        inc[new["a"]].add(nxt)
        inc[new["b"]].add(nxt)
        nxt += 1
    return list(edges.values())


def migrate_state(st, chains, coords):
    """Convert an edits.json written by the ChatGPT-modified editor into this format.

    That editor cut a track at arbitrary points and invented nodes called "cut:N:node-a" for the new
    ends. Those nodes don't exist in OSM, so nothing can ever connect to them. Here every such piece is
    re-attached to the original chain it came from, snapped to REAL OSM nodes (the free end is cut back
    to the last real node), and the chain is registered as "cut". "confirmed" becomes "done".
    Returns (new_state, notes) - notes is None when there was nothing to convert."""
    is_fake = lambda x: isinstance(x, str) and x.startswith("cut:")
    added = st.get("added", [])
    fakes = [a for a in added if is_fake(a.get("a")) or is_fake(a.get("b"))]
    if not fakes and "confirmed" not in st:
        return st, None
    by_id = {c["id"]: c for c in chains}
    xyidx = defaultdict(list)
    for c in chains:
        for k, q in enumerate(c["p"]):
            xyidx[(q[0], q[1])].append((c["id"], k))
    byxy = {}
    for n_, (la, lo) in coords.items():
        byxy.setdefault((round(la, 6), round(lo, 6)), n_)
    pieces = [pc for pc in st.get("pieces", [])]
    have = {pc["id"] for pc in pieces}
    cut = set(st.get("cut", []))
    notes = {"pieces": 0, "free_pieces": 0, "dropped": 0, "confirmed_lost": 0}
    for a in fakes:
        hits = [(q, xyidx.get((round(q[0], 6), round(q[1], 6)))) for q in a["p"]]
        hits = [(q, h) for q, h in hits if h]            # the interpolated cut points match nothing
        if len(hits) < 2:
            notes["dropped"] += 1
            continue
        cands = set(cid for cid, _ in hits[0][1])
        for _, h in hits[1:]:
            cands &= set(cid for cid, _ in h)
        placed = False
        for cid in sorted(cands):
            idx = [next(k for c2, k in h if c2 == cid) for _, h in hits]
            step = {idx[i + 1] - idx[i] for i in range(len(idx) - 1)}
            if step in ({1}, {-1}):
                s, e = min(idx), max(idx)
                c = by_id[cid]
                pid = f"{cid}/{s}-{e}"
                if pid not in have:
                    have.add(pid)
                    pieces.append({"id": pid, "a": c["n"][s], "b": c["n"][e], "p": c["p"][s:e + 1],
                                   "n": c["n"][s:e + 1]})
                cut.add(cid)
                notes["pieces"] += 1
                placed = True
                break
        if not placed:                                    # could not find the parent chain: keep as free piece
            ids = [byxy.get((round(q[0], 6), round(q[1], 6))) for q, _ in hits]
            if None in ids or ids[0] == ids[-1]:
                notes["dropped"] += 1
                continue
            pid = f"mig:{len(pieces) + 1}"
            pieces.append({"id": pid, "a": ids[0], "b": ids[-1], "p": [q for q, _ in hits], "n": ids})
            notes["free_pieces"] += 1
    removed = [i for i in st.get("removed", []) if i not in cut]
    done = list(st.get("done", []))
    for i in st.get("confirmed", []):
        if i in by_id:
            done.append(i)
        else:
            notes["confirmed_lost"] += 1
    plain = [a for a in added if not (is_fake(a.get("a")) or is_fake(a.get("b")))]
    new = {"removed": removed, "done": sorted(set(done)), "cut": sorted(cut), "pieces": pieces, "added": plain}
    return new, notes


def split_at_shared_nodes(edges):
    """A track whose middle node is the end node of another edge (a cut end, or a green line
    attached mid-track) is split there, so the two really are connected in the exported graph."""
    ends = set()
    for e in edges:
        ends.add(e["a"])
        ends.add(e["b"])
    out = []
    for e in edges:
        n = e.get("n")
        cuts = [k for k in range(1, len(n) - 1) if n[k] in ends] if n and len(n) == len(e["p"]) else []
        if not cuts:
            out.append(e)
            continue
        bounds = [0] + cuts + [len(n) - 1]
        for s, t in zip(bounds, bounds[1:]):
            out.append({"a": n[s], "b": n[t], "p": e["p"][s:t + 1], "n": n[s:t + 1]})
    return out


def export(app, outdir):
    st = app.state
    removed = set(st.get("removed", []))
    hidden = set(st.get("cut", []))
    base = [c for c in app.chains if c["id"] not in hidden] + st.get("pieces", [])
    nid = lambda x: int(x) if str(x).lstrip("-").isdigit() else x   # ids may be strings in edits.json
    edges = [{"a": nid(c["a"]), "b": nid(c["b"]), "p": c["p"], "n": [nid(x) for x in c["n"]] if c.get("n") else None}
             for c in base if c["id"] not in removed]
    edges += [{"a": nid(c["a"]), "b": nid(c["b"]), "p": c["p"]} for c in st.get("added", [])]
    edges = [e for e in edges if e["a"] != e["b"] or len(e["p"]) > 2]
    edges = split_at_shared_nodes(edges)
    merged = merge_degree2(edges)
    os.makedirs(outdir, exist_ok=True)
    nodes, gedges, feats = {}, [], []
    for e in merged:
        nodes[str(e["a"])] = e["p"][0]
        nodes[str(e["b"])] = e["p"][-1]
        length = sum(haversine(p, q) for p, q in zip(e["p"], e["p"][1:]))
        gedges.append({"a": e["a"], "b": e["b"], "length_m": round(length, 1), "geometry": e["p"]})
        feats.append({
            "type": "Feature",
            "properties": {"a": e["a"], "b": e["b"], "length_m": round(length, 1)},
            "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in e["p"]]},
        })
    with open(os.path.join(outdir, "simplified.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    with open(os.path.join(outdir, "graph.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": gedges}, f)
    return {"edges_before": len(app.chains) + len(st.get("added", [])),
            "edges_after": len(merged), "nodes_after": len(nodes), "outdir": os.path.abspath(outdir)}


# --------------------------------------------------------------------------- server
def compute_parallel(chains, dist=15.0, step=10.0):
    """For every chain, find other chains that run alongside it (>=50% of its sample
    points within `dist` metres of the other chain). Chains < 40 m (crossovers, switch
    links) are ignored, as are the 15 m next to each end (where chains meet at junctions)."""
    kx, ky = math.cos(math.radians(50.6)) * 111320.0, 110574.0
    cell = dist
    grid = defaultdict(list)
    samples = []
    for ci, c in enumerate(chains):
        pts = [(p[1] * kx, p[0] * ky) for p in c["p"]]
        seg = [math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts, pts[1:])]
        total = sum(seg)
        out = []
        if total >= 40:
            t, acc = 15.0, 0.0
            for (x1, y1), (x2, y2), ln in zip(pts, pts[1:], seg):
                while ln > 0 and t <= acc + ln and t <= total - 15:
                    f = (t - acc) / ln
                    out.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
                    t += step
                acc += ln
        samples.append(out)
        for x, y in out:
            grid[(int(x // cell), int(y // cell))].append((x, y, ci))
    partners = defaultdict(set)
    d2 = dist * dist
    for ci, pts in enumerate(samples):
        if not pts:
            continue
        cnt = defaultdict(int)
        for x, y in pts:
            gx, gy = int(x // cell), int(y // cell)
            found = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for x2, y2, cj in grid.get((gx + dx, gy + dy), ()):
                        if cj != ci and (x2 - x) ** 2 + (y2 - y) ** 2 <= d2:
                            found.add(cj)
            for cj in found:
                cnt[cj] += 1
        for cj, n in cnt.items():
            if n >= 0.5 * len(pts):
                partners[ci].add(cj)
                partners[cj].add(ci)
    return {chains[i]["id"]: [chains[j]["id"] for j in s] for i, s in partners.items()}


class App:
    pass


app = App()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self._send(HTML, "text/html")
        elif u.path == "/api/data":
            self._send(json.dumps({"chains": app.chains, "state": app.state}, separators=(",", ":")))
        elif u.path == "/api/parallel":
            with app.lock:
                if app.parallel is None:
                    print("Computing parallel tracks...")
                    app.parallel = compute_parallel(app.chains, app.pdist)
                    print("done")
            self._send(json.dumps(app.parallel, separators=(",", ":")))
        elif u.path == "/api/nearest":
            q = urllib.parse.parse_qs(u.query)
            n = app.index.nearest(float(q["lat"][0]), float(q["lon"][0]))
            self._send(json.dumps(n or {}))
        else:
            self._send("not found", "text/plain", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/state":
            app.state = {k: body.get(k, []) for k in ("removed", "done", "cut", "pieces", "added")}
            tmp = app.edits_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(app.state, f)
            os.replace(tmp, app.edits_path)
            self._send('{"ok":true}')
        elif self.path == "/api/export":
            self._send(json.dumps(export(app, app.outdir)))
        else:
            self._send("not found", "text/plain", 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="rail_generators/rail_be.json")
    ap.add_argument("--edits", default="rail_generators/edits.json")
    ap.add_argument("--out", default="data")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--parallel-dist", type=float, default=15.0,
                    help="max distance (m) between tracks to count as parallel")
    ap.add_argument("--reset-cuts", action="store_true",
                    help="undo ALL cuts in edits.json (backup saved as edits.json.bak); other edits are kept")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    if a.refresh or not os.path.exists(a.cache):
        data = fetch_overpass(a.cache)
    else:
        with open(a.cache, encoding="utf-8") as f:
            data = json.load(f)
    coords, adj = build_graph(data)
    app.chains = build_chains(coords, adj)
    app.index = NodeIndex(coords)
    app.edits_path, app.outdir = a.edits, a.out
    app.lock, app.parallel, app.pdist = threading.Lock(), None, a.parallel_dist
    app.state = {"removed": [], "added": []}
    if os.path.exists(a.edits):
        with open(a.edits) as f:
            app.state = json.load(f)
    app.state, note = migrate_state(app.state, app.chains, coords)
    if note:
        shutil.copy(a.edits, a.edits + ".chatgpt.bak")
        with open(a.edits, "w") as f:
            json.dump(app.state, f)
        print(f"Converted edits.json from the old format (backup: {a.edits}.chatgpt.bak): {note}")
    if a.reset_cuts and os.path.exists(a.edits):
        shutil.copy(a.edits, a.edits + ".bak")
        app.state["cut"], app.state["pieces"] = [], []
        for k in ("removed", "done"):
            app.state[k] = [i for i in app.state.get(k, []) if "/" not in i]
        with open(a.edits, "w") as f:
            json.dump(app.state, f)
        print("All cuts reset (backup: edits.json.bak)")
    print(f"{len(coords)} nodes, {len(app.chains)} chains. Editor: http://localhost:{a.port}")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{a.port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- frontend
HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Rail graph editor</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body,#map{height:100%;margin:0}
#bar{position:absolute;z-index:1000;top:10px;left:56px;background:#fff;padding:8px 10px;border-radius:6px;
     font:13px/1.4 system-ui,sans-serif;box-shadow:0 1px 6px #0006;max-width:420px}
#bar button{margin:0 3px 4px 0;padding:4px 9px;border:1px solid #888;border-radius:4px;background:#f4f4f4;cursor:pointer}
#bar button.on{background:#2b6cb0;color:#fff;border-color:#2b6cb0}
#help{color:#555;font-size:12px}
#status{margin-top:4px;font-weight:600}
</style></head><body>
<div id="map"></div>
<div id="bar">
  <button id="bDel" class="on">Delete (D)</button>
  <button id="bAdd">Add edge (A)</button>
  <button id="bCut">Cut section (X)</button>
  <button id="bDone">Mark done (C)</button>
  <button id="bRestore">Restore cut (R)</button>
  <button id="bUndo">Undo (Z)</button>
  <button id="bGhost">Show removed (G)</button>
  <button id="bMap" class="on">Map (M)</button>
  <button id="bPar">Double track (P)</button>
  <button id="bExp">Export</button>
  <div id="help"></div>
  <div id="status">loading…</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ONE shared canvas renderer for every vector layer (tracks, markers, box). Using several
// canvases made a later one cover the track layers and block clicks (the "can't delete after adding" bug).
const renderer = L.canvas({tolerance: 7});
const map = L.map('map', {renderer, boxZoom: false}).setView([50.6, 4.5], 8);
const baseLayer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution: '&copy; OpenStreetMap contributors', maxZoom: 19}).addTo(map);
const $ = id => document.getElementById(id);

let chains = {}, layers = {}, removed = new Set(), done = new Set(), undoStack = [];
let mode = 'delete', ghost = false, nextUid = 1, pending = null, pendingMarker = null, hoverMarker = null;
let cutState = null, cutMarker = null, cutPrev = null, hoverChain = null;
let saveTimer = null, savedOk = true;

const GHOST = {color: '#888', weight: 2, dashArray: '4 4', opacity: .7};
let showPar = false, parallel = {};   // parallel[id] = ids of chains running alongside it
// a chain is "live" if it exists and isn't removed; a chain that was cut is live while any of its pieces is
const live = id => { const c = chains[id]; if (!c) return false; return c.kids ? c.kids.some(live) : !removed.has(id); };
const isDouble = c => showPar && !done.has(c.id) && !removed.has(c.id) && (parallel[c.id] || []).some(live);
const styleOf = c => c.added ? {color: '#1a9850', weight: 4}
  : isDouble(c) ? {color: '#e6007e', weight: 4} : {color: '#2b6cb0', weight: 3};

function refresh(id) {
  const c = chains[id], l = layers[id];
  if (!l) return;
  if (c.kids) {   // cut chain: only its pieces are shown, except in restore mode (original drawn dashed)
    if (mode === 'restore') {
      l.setStyle({color: '#ff7f00', weight: 7, dashArray: '6 6', opacity: .8});
      if (!map.hasLayer(l)) l.addTo(map);
      l.bringToBack();
    } else if (map.hasLayer(l)) map.removeLayer(l);
    return;
  }
  if (removed.has(id)) {
    if (ghost || mode === 'restore') { l.setStyle(GHOST); if (!map.hasLayer(l)) l.addTo(map); }
    else if (map.hasLayer(l)) map.removeLayer(l);
  } else {
    l.setStyle(Object.assign({opacity: .9, dashArray: null}, styleOf(c)));
    if (!map.hasLayer(l)) l.addTo(map);
  }
}
function refreshDeep(id) {
  const c = chains[id];
  if (!c) return;
  if (c.kids) c.kids.forEach(refreshDeep);
  refresh(id);
}
function addLayer(c) {
  const l = L.polyline(c.p, Object.assign({renderer, opacity: .9}, styleOf(c)));
  l.on('mouseover', () => { hoverChain = c; if (!removed.has(c.id) || mode === 'restore') l.setStyle({color: '#ff7f00', weight: 5}); });
  l.on('mouseout', () => { if (hoverChain === c) hoverChain = null; refresh(c.id); });
  l.on('click', e => {
    if (mode === 'delete') {
      L.DomEvent.stopPropagation(e);
      const rm = !removed.has(c.id);
      toggle([c.id], rm);
      undoStack.push({kind: 'rm', ids: [c.id], val: rm});
    } else if (mode === 'done') {
      L.DomEvent.stopPropagation(e);
      if (removed.has(c.id)) return;
      const v = !done.has(c.id);
      setDone([c.id], v);
      undoStack.push({kind: 'done', ids: [c.id], val: v});
    } else if (mode === 'cut') {
      L.DomEvent.stopPropagation(e);
      cutClick(e.latlng, c);
    } else if (mode === 'restore') {
      L.DomEvent.stopPropagation(e);
      restoreClick(c);
    }
  });
  layers[c.id] = l;
  refresh(c.id);
}
function toggle(ids, rm) {
  ids.forEach(id => { rm ? removed.add(id) : removed.delete(id); });
  const touched = new Set(ids);
  if (showPar) ids.forEach(id => (parallel[id] || []).forEach(p => touched.add(p)));
  touched.forEach(refreshDeep);
  changed();
}
function setDone(ids, v) {
  ids.forEach(id => { v ? done.add(id) : done.delete(id); refresh(id); });
  changed();
}
function undo() {
  const u = undoStack.pop();
  if (!u) return;
  if (u.kind === 'rm') toggle(u.ids, !u.val);
  else if (u.kind === 'done') setDone(u.ids, !u.val);
  else if (u.kind === 'cut') undoCut(u);
  else if (u.kind === 'restore') undoRestore(u);
  else if (u.kind === 'group') { toggle(u.origs, true); toggle(u.greens, false); }
}

// --- precise cutting: remove the section between two nodes of one chain
function nearestVertex(c, ll) {
  const pt = map.latLngToLayerPoint(ll);
  let best = 0, bd = Infinity;
  for (let k = 0; k < c.p.length; k++) {
    const d = map.latLngToLayerPoint(c.p[k]).distanceTo(pt);
    if (d < bd) { bd = d; best = k; }
  }
  return best;
}
function cutHover(ll, c) {
  if (cutState) c = chains[cutState.id];
  if (!c || c.kids || c.added || removed.has(c.id)) {
    if (hoverMarker) { map.removeLayer(hoverMarker); hoverMarker = null; }
    return;
  }
  const k = nearestVertex(c, ll), pt = c.p[k];
  if (!hoverMarker) hoverMarker = L.circleMarker(pt, {radius: 6, color: '#d73027', fillOpacity: .3, interactive: false}).addTo(map);
  else hoverMarker.setLatLng(pt);
  if (cutState) {
    const seg = c.p.slice(Math.min(cutState.i, k), Math.max(cutState.i, k) + 1);
    if (!cutPrev) cutPrev = L.polyline(seg, {color: '#d73027', weight: 8, opacity: .6, interactive: false}).addTo(map);
    else cutPrev.setLatLngs(seg);
  }
}
function cutClick(ll, c) {
  if (!cutState) {
    if (!c || c.kids || c.added || removed.has(c.id)) return;
    cutState = {id: c.id, i: nearestVertex(c, ll)};
    cutMarker = L.circleMarker(c.p[cutState.i], {radius: 8, color: '#ff7f00', fillOpacity: .7, interactive: false}).addTo(map);
    $('help').textContent = 'Now click the other end of the section to cut out (anywhere near this same track). Esc cancels.';
    return;
  }
  const c0 = chains[cutState.id], i = cutState.i, j = nearestVertex(c0, ll);
  cancelCut();
  if (i !== j) doCut(c0, Math.min(i, j), Math.max(i, j));
}
function cancelCut() {
  cutState = null;
  [cutMarker, cutPrev].forEach(x => { if (x) map.removeLayer(x); });
  cutMarker = cutPrev = null;
  if (mode === 'cut') setHelp();
}
function doCut(c, i, j) {
  const n = c.n || [c.a, c.b], last = c.p.length - 1, kids = [];
  const mk = (s, e) => {
    if (e - s < 1) return;
    const id = `${c.id}/${s}-${e}`;
    chains[id] = {id, a: n[s], b: n[e], p: c.p.slice(s, e + 1), n: n.slice(s, e + 1), piece: true};
    parallel[id] = (parallel[c.id] || []).slice();
    if (done.has(c.id)) done.add(id);
    addLayer(chains[id]);
    kids.push(id);
  };
  mk(0, i); mk(j, last);
  c.kids = kids;
  refresh(c.id);
  (parallel[c.id] || []).forEach(refreshDeep);
  undoStack.push({kind: 'cut', id: c.id, kids});
  changed();
}
function undoCut(u) {
  u.kids.forEach(id => {
    if (layers[id]) { if (map.hasLayer(layers[id])) map.removeLayer(layers[id]); delete layers[id]; }
    delete chains[id]; delete parallel[id]; removed.delete(id); done.delete(id);
  });
  const c = chains[u.id];
  delete c.kids;
  refresh(c.id);
  (parallel[c.id] || []).forEach(refreshDeep);
  changed();
}

// --- restore: throw away every cut of a chain and bring the original geometry back
function dropChain(id) {
  if (layers[id]) { if (map.hasLayer(layers[id])) map.removeLayer(layers[id]); delete layers[id]; }
  delete chains[id]; delete parallel[id]; removed.delete(id); done.delete(id);
}
function restoreChain(rootId) {
  const root = chains[rootId];
  if (!root || !root.kids) return;
  const prefix = rootId + '/';
  const pieces = Object.values(chains).filter(c => c.piece && c.id.startsWith(prefix));
  const u = {kind: 'restore', id: rootId, kids: root.kids.slice(),
             pieces: pieces.map(c => ({c, rem: removed.has(c.id), dn: done.has(c.id), par: parallel[c.id]}))};
  pieces.forEach(c => dropChain(c.id));
  delete root.kids;
  refresh(rootId);
  (parallel[rootId] || []).forEach(refreshDeep);
  undoStack.push(u);
  changed();
}
function undoRestore(u) {
  u.pieces.forEach(({c, rem, dn, par}) => {
    chains[c.id] = c;
    if (par) parallel[c.id] = par;
    if (rem) removed.add(c.id);
    if (dn) done.add(c.id);
    addLayer(c);
  });
  chains[u.id].kids = u.kids;
  refreshDeep(u.id);
  (parallel[u.id] || []).forEach(refreshDeep);
  changed();
}

// --- restore for tracks that were replaced by green (added) lines
const KX = Math.cos(50.6 * Math.PI / 180) * 111320, KY = 110574;
const metric = q => [q[1] * KX, q[0] * KY];
function distSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
  let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
// do all vertices of g lie within tol metres of the polyline o?
function lies(g, o, tol = 4) {
  if (!o._m) o._m = o.p.map(metric);
  const M = o._m;
  return g.p.every(q => {
    const [x, y] = metric(q);
    for (let i = 0; i + 1 < M.length; i++) if (distSeg(x, y, M[i][0], M[i][1], M[i + 1][0], M[i + 1][1]) <= tol) return true;
    return false;
  });
}
const originals = pred => Object.values(chains).filter(c => !c.added && !c.kids && pred(c));
const greens = () => Object.values(chains).filter(c => c.added && !removed.has(c.id));
function restoreClick(c) {
  if (c.kids || c.piece) { restoreChain(c.piece ? c.id.split('/')[0] : c.id); return; }
  if (removed.has(c.id)) {
    if (c.added) { toggle([c.id], false); undoStack.push({kind: 'rm', ids: [c.id], val: false}); return; }
    applyGroup([c.id], greens().filter(g => lies(g, c)));        // removed original: drop green copies of it
    return;
  }
  if (!c.added) return;
  const os = originals(o => removed.has(o.id) && lies(c, o));  // green line: find the removed original(s) under it
  if (!os.length) {
    if (originals(o => !removed.has(o.id) && lies(c, o)).length) {   // duplicate of a live track: just drop it
      toggle([c.id], true); undoStack.push({kind: 'rm', ids: [c.id], val: true});
    } else $('help').textContent = 'No removed original track found under this green line.';
    return;
  }
  const gs = new Set([c, ...os.flatMap(o => greens().filter(g => lies(g, o)))]);
  applyGroup(os.map(o => o.id), [...gs]);
}
function applyGroup(origs, gs) {
  const g = gs.map(x => x.id);
  toggle(g, true); toggle(origs, false);
  undoStack.push({kind: 'group', origs, greens: g});
}

function status() {
  const all = Object.values(chains).filter(c => !c.kids);
  const nAdded = all.filter(c => c.added && !removed.has(c.id)).length;
  const nRem = all.filter(c => !c.added && removed.has(c.id)).length;
  const nDone = all.filter(c => done.has(c.id) && !removed.has(c.id)).length;
  $('status').textContent = `${all.filter(c => !c.added).length} chains · ${nRem} removed · ${nDone} done · ` +
    `${nAdded} added · ` + (savedOk ? 'saved' : 'saving…');
}
function changed() {
  savedOk = false; status();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 400);
}
async function save() {
  const all = Object.values(chains);
  const state = {
    removed: [...removed].filter(id => !(chains[id] && chains[id].added)),
    done: [...done].filter(id => chains[id] && !chains[id].kids),
    cut: all.filter(c => c.kids && !c.piece).map(c => c.id),
    pieces: all.filter(c => c.piece && !c.kids).map(c => ({id: c.id, a: c.a, b: c.b, p: c.p, n: c.n})),
    added: all.filter(c => c.added && !removed.has(c.id)).map(c => ({a: c.a, b: c.b, p: c.p}))
  };
  await fetch('/api/state', {method: 'POST', body: JSON.stringify(state)});
  savedOk = true; status();
}

function cancelPending() {
  pending = null;
  if (pendingMarker) { map.removeLayer(pendingMarker); pendingMarker = null; }
}
const HELP = {
  delete: 'Click a chain to remove it (click a dashed one to restore). Shift+drag: remove every chain fully inside the box.',
  add: 'Zoom in (≥13), click a node, then a second node: a straight connection is created. Esc cancels.',
  done: 'Click a chain to mark it done (not pink in double-track view); click again to unmark. Shift+drag: mark all chains fully inside the box.',
  restore: 'Dashed = removed/cut tracks. Click a dashed track, or a wrong green line, to bring back the original track and drop the green replacement(s). Undo (Z) works.',
  cut: 'Click a node on a chain, then another node on the same chain: the section between them is removed. Select the chain end to shorten it. Esc cancels.'
};
const BTN = {delete: 'bDel', add: 'bAdd', done: 'bDone', cut: 'bCut', restore: 'bRestore'};
function setHelp() { $('help').textContent = HELP[mode]; }
function setMode(m) {
  mode = m; cancelPending(); cancelCut();
  if (hoverMarker) { map.removeLayer(hoverMarker); hoverMarker = null; }
  for (const k in BTN) $(BTN[k]).classList.toggle('on', k === m);
  setHelp();
  Object.values(chains).filter(c => c.kids).forEach(c => refresh(c.id));
  [...removed].forEach(refresh);
  $('map').style.cursor = (m === 'add' || m === 'cut') ? 'crosshair' : (m === 'done' || m === 'restore' ? 'pointer' : '');
}

// --- box select (shift + drag) in delete / done mode
let rect = null, rectStart = null;
const boxMode = () => mode === 'delete' || mode === 'done';
document.addEventListener('keydown', e => { if (e.key === 'Shift' && boxMode()) map.dragging.disable(); });
document.addEventListener('keyup', e => { if (e.key === 'Shift') map.dragging.enable(); });
map.on('mousedown', e => {
  if (boxMode() && e.originalEvent.shiftKey) {
    rectStart = e.latlng;
    rect = L.rectangle([rectStart, rectStart], {color: mode === 'done' ? '#1a9850' : '#d73027', weight: 1, interactive: false}).addTo(map);
  }
});
map.on('mouseup', () => {
  if (!rect) return;
  const b = rect.getBounds(); map.removeLayer(rect); rect = null;
  const ids = [];
  for (const id in chains) {
    const c = chains[id];
    if (c.kids || removed.has(id) || (mode === 'done' && done.has(id))) continue;
    if (c.p.every(p => b.contains(L.latLng(p[0], p[1])))) ids.push(id);
  }
  if (!ids.length) return;
  if (mode === 'done') { setDone(ids, true); undoStack.push({kind: 'done', ids, val: true}); }
  else { toggle(ids, true); undoStack.push({kind: 'rm', ids, val: true}); }
});

// --- add mode
async function nearest(ll) {
  const r = await fetch(`/api/nearest?lat=${ll.lat}&lon=${ll.lng}`);
  return r.json();
}
let busy = false;
map.on('mousemove', async e => {
  if (rect) { rect.setBounds(L.latLngBounds(rectStart, e.latlng)); return; }
  if (mode === 'cut') { cutHover(e.latlng, hoverChain); return; }
  if (mode !== 'add' || busy || map.getZoom() < 13) return;
  busy = true;
  try {
    const n = await nearest(e.latlng);
    if (n.id && mode === 'add') {
      if (!hoverMarker) hoverMarker = L.circleMarker([n.lat, n.lon], {radius: 6, color: '#d73027', fillOpacity: .3, interactive: false}).addTo(map);
      else hoverMarker.setLatLng([n.lat, n.lon]);
    }
  } finally { busy = false; }
});
map.on('click', async e => {
  if (mode === 'cut') { if (cutState) cutClick(e.latlng, null); return; }
  if (mode !== 'add' || map.getZoom() < 13) return;
  const n = await nearest(e.latlng);
  if (!n.id) return;
  if (!pending) {
    pending = n;
    pendingMarker = L.circleMarker([n.lat, n.lon], {radius: 8, color: '#1a9850', fillOpacity: .6, interactive: false}).addTo(map);
    return;
  }
  if (n.id !== pending.id) {
    const c = {id: 'add:' + (nextUid++), a: pending.id, b: n.id, added: true,
               p: [[pending.lat, pending.lon], [n.lat, n.lon]]};
    chains[c.id] = c; addLayer(c);
    undoStack.push({kind: 'rm', ids: [c.id], val: false});
    changed();
  }
  cancelPending();
});

// --- UI
$('bDel').onclick = () => setMode('delete');
$('bAdd').onclick = () => setMode('add');
$('bDone').onclick = () => setMode('done');
$('bCut').onclick = () => setMode('cut');
$('bRestore').onclick = () => setMode('restore');
$('bUndo').onclick = undo;
$('bGhost').onclick = () => {
  ghost = !ghost; $('bGhost').classList.toggle('on', ghost);
  for (const id of removed) refresh(id);
};
$('bMap').onclick = () => {
  const on = map.hasLayer(baseLayer);
  if (on) map.removeLayer(baseLayer); else baseLayer.addTo(map);
  $('bMap').classList.toggle('on', !on);
  $('map').style.background = '#fff';
};
function inheritParallel() {   // pieces of a cut chain run alongside the same chains as their root chain
  Object.values(chains).forEach(c => { if (c.piece) parallel[c.id] = (parallel[c.id.split('/')[0]] || []).slice(); });
}
$('bPar').onclick = async () => {
  showPar = !showPar;
  $('bPar').classList.toggle('on', showPar);
  if (showPar && !Object.keys(parallel).length) {
    $('status').textContent = 'computing parallel tracks (first time takes a while)…';
    parallel = await (await fetch('/api/parallel')).json();
    inheritParallel();
    status();
  }
  Object.keys(chains).forEach(refresh);
};
$('bExp').onclick = async () => {
  await save();
  const r = await (await fetch('/api/export', {method: 'POST', body: '{}'})).json();
  alert(`Exported to ${r.outdir}\nedges: ${r.edges_before} -> ${r.edges_after}, nodes: ${r.nodes_after}\n` +
        `Files: simplified.geojson, graph.json`);
};
document.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'd') setMode('delete');
  else if (k === 'a') setMode('add');
  else if (k === 'c') setMode('done');
  else if (k === 'x') setMode('cut');
  else if (k === 'r') setMode('restore');
  else if (k === 'z') undo();
  else if (k === 'g') $('bGhost').click();
  else if (k === 'm') $('bMap').click();
  else if (k === 'p') $('bPar').click();
  else if (k === 'escape') { cancelPending(); cancelCut(); }
});

// --- load
fetch('/api/data').then(r => r.json()).then(d => {
  let minLa = 90, maxLa = -90, minLo = 180, maxLo = -180;
  d.chains.forEach(c => {
    chains[c.id] = c;
    c.p.forEach(p => { minLa = Math.min(minLa, p[0]); maxLa = Math.max(maxLa, p[0]); minLo = Math.min(minLo, p[1]); maxLo = Math.max(maxLo, p[1]); });
  });
  const st = d.state;
  (st.pieces || []).forEach(pc => { chains[pc.id] = {id: pc.id, a: pc.a, b: pc.b, p: pc.p, n: pc.n, piece: true}; });
  (st.cut || []).forEach(id => {
    if (chains[id]) chains[id].kids = Object.keys(chains).filter(k => chains[k].piece && k.startsWith(id + '/'));
  });
  (st.removed || []).forEach(id => { if (chains[id]) removed.add(id); });
  (st.done || []).forEach(id => { if (chains[id]) done.add(id); });
  (st.added || []).forEach(a => {
    const c = {id: 'add:' + (nextUid++), a: a.a, b: a.b, p: a.p, added: true};
    chains[c.id] = c;
  });
  Object.values(chains).forEach(addLayer);
  if (minLa < 90) map.fitBounds([[minLa, minLo], [maxLa, maxLo]]);
  setMode('delete'); savedOk = true; status();
});
</script></body></html>
"""

if __name__ == "__main__":
    main()
