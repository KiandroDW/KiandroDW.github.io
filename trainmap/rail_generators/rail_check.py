#!/usr/bin/env python3
"""
rail_check.py - sanity checker for the network you pruned with rail_editor.py

    python rail_check.py                 # reads rail_be.json + edits.json, writes out/check_report.html
    python rail_check.py --no-browser    # don't open the report in the browser
    python rail_check.py --no-serve       # only write out/check_report.html (the map background needs http://)

It rebuilds the network exactly like the editor's Export does (removed chains gone, cuts applied,
green lines added, tracks split where a cut end / added line attaches mid-track) and then checks:

  parallel  edges that are still detected as running alongside another edge and are not marked "done"
  gap       dead-end nodes (degree 1) that lie close to another track  -> most likely a missing connection
  deadend   all other dead ends (real termini / border, or unconnected pieces)
  dup       two or more edges between the same two nodes, or an edge that loops back to its own node
  touch     two different nodes (both degree >= 2) a few metres apart that are NOT connected
  island    pieces of network that are not connected to the main network
  kink      sharp bends at pass-through nodes, and added lines that meet a track at an odd angle
  cross     added (green) lines that cross another track without a node
  long      very long added connections (straight lines cut corners)
  tiny      tiny edges between two junctions (leftover crossovers)
  hidegree  nodes with 4+ edges (leftover yards / crossings)         [off by default in the report]
  split     places where a track is split because a line attaches mid-track [off by default]

The report has an OK button per item; those are remembered (browser localStorage) across re-runs,
so you only have to look at new problems after each fix. Needs only the Python standard library.
"""
import argparse
import json
import math
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict

KX = math.cos(math.radians(50.6)) * 111320.0
KY = 110574.0


def mxy(p):
    return (p[1] * KX, p[0] * KY)


# ------------------------------------------------------------------ same chain building as rail_editor.py
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
    for k in sorted(adj):
        for n in sorted(adj[k]):
            if edge(k, n) not in seen:
                chains.append(walk(k, n))
    return [{"id": f"{p[0]}-{p[1]}", "a": p[0], "b": p[-1], "n": p,
             "p": [[round(coords[n][0], 6), round(coords[n][1], 6)] for n in p]} for p in chains]


# ------------------------------------------------------------------ same parallel detection as the editor
def compute_parallel(edges, dist=15.0, step=10.0):
    cell = dist
    grid = defaultdict(list)
    samples = []
    for ci, c in enumerate(edges):
        pts = [mxy(p) for p in c["p"]]
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
    return partners


# ------------------------------------------------------------------ rebuild the final network
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


def load_state(path):
    st = {"removed": [], "done": [], "cut": [], "pieces": [], "added": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            st.update(json.load(f))
    return st


def edge_key(e):
    if e["kind"] == "added":
        lo, hi = sorted((str(e["a"]), str(e["b"])))
        return f"add{lo}-{hi}"
    return e["id"]


def nid(x):
    """All node ids are handled as strings, so real (int) and invented (str) ids never clash."""
    return str(x)


def build_edges(chains, st):
    removed, hidden = set(st.get("removed", [])), set(st.get("cut", []))
    known = {c["id"] for c in chains}
    edges = []
    for c in chains:
        if c["id"] in hidden or c["id"] in removed:
            continue
        edges.append({"id": c["id"], "a": nid(c["a"]), "b": nid(c["b"]), "p": c["p"],
                      "n": [nid(x) for x in c["n"]], "kind": "orig"})
    for pc in st.get("pieces", []):
        known.add(pc["id"])
        if pc["id"] in removed:
            continue
        edges.append({"id": pc["id"], "a": nid(pc["a"]), "b": nid(pc["b"]), "p": pc["p"],
                      "n": [nid(x) for x in pc["n"]] if pc.get("n") else None, "kind": "piece"})
    for i, a in enumerate(st.get("added", [])):
        edges.append({"id": f"add:{i + 1}", "a": nid(a["a"]), "b": nid(a["b"]), "p": a["p"],
                      "n": [nid(a["a"]), nid(a["b"])], "kind": "added"})
        known.add(f"add:{i + 1}")
    edges = [e for e in edges if e["a"] != e["b"] or len(e["p"]) > 2]
    chain_ids = {c["id"] for c in chains}
    stale = {
        "removed": sum(1 for i in removed if i not in known),
        "done": sum(1 for i in st.get("done", []) if i not in known),
        "cut": sum(1 for i in hidden if i not in chain_ids),
        "pieces": sum(1 for pc in st.get("pieces", []) if pc["id"].split("/")[0] not in chain_ids),
    }
    return edges, stale


def split_at_shared_nodes(edges):
    """A track whose middle node is the end node of another edge (a cut end or a green line that
    was attached mid-track) is split there, so that the two are really connected."""
    ends = set()
    for e in edges:
        ends.add(e["a"])
        ends.add(e["b"])
    out, splits = [], []
    for e in edges:
        n = e.get("n")
        cuts = [k for k in range(1, len(n) - 1) if n[k] in ends] if n and len(n) == len(e["p"]) else []
        if not cuts:
            out.append(e)
            continue
        bounds = [0] + cuts + [len(n) - 1]
        for s, t in zip(bounds, bounds[1:]):
            out.append(dict(e, id=f"{e['id']}#{s}", parent=e["id"], a=n[s], b=n[t],
                            p=e["p"][s:t + 1], n=n[s:t + 1]))
        splits += [(n[k], e["id"], e["p"][k]) for k in cuts]
    return out, splits


# ------------------------------------------------------------------ geometry helpers
def length_m(pts):
    m = [mxy(p) for p in pts]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(m, m[1:]))


def dir_from(e, node, dist=30.0):
    seq = [mxy(q) for q in (e["p"] if e["a"] == node else e["p"][::-1])]
    x0, y0 = seq[0]
    for x, y in seq[1:]:
        if math.hypot(x - x0, y - y0) >= dist:
            return (x - x0, y - y0)
    return (seq[-1][0] - x0, seq[-1][1] - y0)


def angle_between(v1, v2):
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 8 or n2 < 8:
        return None
    c = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def seg_cross(p1, p2, p3, p4):
    d1 = (p2[0] - p1[0], p2[1] - p1[1])
    d2 = (p4[0] - p3[0], p4[1] - p3[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / den
    u = ((p3[0] - p1[0]) * d1[1] - (p3[1] - p1[1]) * d1[0]) / den
    if 0 < t < 1 and 0 < u < 1:
        return (p1[0] + t * d1[0], p1[1] + t * d1[1])
    return None


def sample_edge(pts, step):
    out = []
    for k in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[k], pts[k + 1]
        m = max(1, int(math.hypot(x2 - x1, y2 - y1) // step))
        for s in range(m):
            f = s / m
            out.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
    out.append(pts[-1])
    return out


def ll(xy):
    return [round(xy[1] / KY, 6), round(xy[0] / KX, 6)]


# ------------------------------------------------------------------ the checks
CATS = [
    ("parallel", "Still parallel, not done", "#e6007e", True,
     "Edge runs alongside another edge and is not marked done"),
    ("gap", "Dead end near a track", "#d73027", True,
     "Degree-1 node close to another track: probably a missing connection"),
    ("dup", "Duplicate / looping edges", "#762a83", True,
     "Several edges between the same two nodes, or an edge looping back to its own node"),
    ("touch", "Touching, not connected", "#b8860b", True,
     "Two different nodes a few metres apart that are not connected"),
    ("island", "Disconnected pieces", "#1b9e77", True,
     "Pieces of network not connected to the main network"),
    ("kink", "Sharp bends / odd angles", "#8c510a", True,
     "Sharp bend at a pass-through node, or an added line meeting a track at a strange angle"),
    ("cross", "Added line crosses a track", "#111111", True,
     "A green line crosses another track without a node"),
    ("deadend", "Other dead ends", "#fd8d3c", True,
     "Degree-1 nodes with nothing close by: real termini / border, or an unconnected piece"),
    ("long", "Long added lines", "#1a9850", True,
     "Straight added connections that may cut a corner"),
    ("tiny", "Tiny links between junctions", "#7b6fd0", True,
     "Very short edges between two junction nodes: leftover crossovers"),
    ("hidegree", "Nodes with 4+ edges", "#6a6a9a", False, "Complex junctions: leftover yards or crossings"),
    ("split", "Mid-track attach points", "#2b6cb0", False,
     "A track is split here because a cut end or added line attaches in its middle"),
]


def run_checks(edges, splits, a):
    issues = []

    def add(cat, key, title, info="", g=None, p=None, x=None):
        issues.append({"c": cat, "k": f"{cat}:{key}", "t": title, "s": info, "g": g, "p": p, "x": x})

    done = set()
    N = len(edges)
    deg, inc, coord = defaultdict(int), defaultdict(list), {}
    for i, e in enumerate(edges):
        for n, pt in ((e["a"], e["p"][0]), (e["b"], e["p"][-1])):
            deg[n] += 1
            inc[n].append(i)
            coord[n] = pt
    elen = [length_m(e["p"]) for e in edges]
    ekey = [edge_key(e) for e in edges]
    return issues, add, deg, inc, coord, elen, ekey, done, N


def check_all(chains, st, a):
    edges, stale = build_edges(chains, st)
    edges, splits = split_at_shared_nodes(edges)
    issues, add, deg, inc, coord, elen, ekey, _, N = run_checks(edges, splits, a)
    done_ids = set(st.get("done", []))
    is_done = lambda e: e.get("parent", e["id"]) in done_ids and e["kind"] != "added"
    line = lambda i: [edges[i]["p"]]

    # 1. still parallel and not done
    partners = compute_parallel(edges, a.parallel_dist)
    for i in sorted(range(N), key=lambda i: -elen[i]):
        e = edges[i]
        ps = [j for j in partners.get(i, ()) if j != i]
        if ps and not is_done(e):
            mid = e["p"][len(e["p"]) // 2]
            add("parallel", ekey[i], f"Parallel to {len(ps)} edge(s), {elen[i]:.0f} m",
                f"{e['kind']} edge {e['id']} · partners: " + ", ".join(edges[j]["id"] for j in ps[:4]),
                g=line(i), p=mid, x=[edges[j]["p"] for j in ps[:5]])

    # 2. dead ends: gap vs plain
    dead = [n for n, d in deg.items() if d == 1]
    cell = a.gap_dist
    grid = defaultdict(list)
    for i, e in enumerate(edges):
        pts = [mxy(q) for q in e["p"]]
        for k, (x, y) in enumerate(sample_edge(pts, 15.0)):
            grid[(int(x // cell), int(y // cell))].append((x, y, i))
    best = {}
    for n in dead:
        x, y = mxy(coord[n])
        i0 = inc[n][0]
        bd, bq = None, None
        gx, gy = int(x // cell), int(y // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for x2, y2, j in grid.get((gx + dx, gy + dy), ()):
                    if j == i0:
                        continue
                    d = math.hypot(x2 - x, y2 - y)
                    if d <= a.gap_dist and (bd is None or d < bd):
                        bd, bq = d, (x2, y2, j)
        best[n] = (bd, bq)
    # near a dead end of another edge: report that pair once
    near_dead = {}
    dead_on = defaultdict(list)
    for m in dead:
        dead_on[inc[m][0]].append(m)
    for n, (bd, bq) in best.items():
        if bq:
            for m in dead_on.get(bq[2], ()):
                mx, my = mxy(coord[m])
                if m != n and math.hypot(mx - bq[0], my - bq[1]) < 1:
                    near_dead[n] = m
    for n in dead:
        bd, bq = best[n]
        i0 = inc[n][0]
        if bd is None:
            continue
        m = near_dead.get(n)
        if m is not None and near_dead.get(m) == n and m < n:
            continue   # the facing dead end reports this pair
        x, y = mxy(coord[n])
        what = f"another dead end (node {m})" if m is not None else f"edge {edges[bq[2]]['id']}"
        add("gap", str(n), f"Dead end {bd:.0f} m from a track",
            f"node {n} · edge {edges[i0]['id']} ({elen[i0]:.0f} m) · near {what}",
            g=line(i0), p=coord[n], x=[[coord[n], ll(bq[:2])]])
        best[n] = (bd, bq, True)
    for n in dead:
        if len(best[n]) == 3 or best[n][0] is not None:
            continue
        i0 = inc[n][0]
        short = " · SHORT SPUR" if elen[i0] < 150 else ""
        add("deadend", str(n), f"Dead end, edge {elen[i0]:.0f} m long",
            f"node {n} · edge {edges[i0]['id']}{short}", g=line(i0), p=coord[n])
    # dead ends that were skipped as the "other half" of a facing pair are covered by their partner

    # 3. duplicates and loops
    pair = defaultdict(list)
    for i, e in enumerate(edges):
        if e["a"] == e["b"]:
            add("dup", ekey[i], f"Edge loops back to its own node, {elen[i]:.0f} m",
                f"node {e['a']} · edge {e['id']}", g=line(i), p=e["p"][0])
        else:
            pair[tuple(sorted((e["a"], e["b"])))].append(i)
    for (u, v), ids in pair.items():
        if len(ids) > 1:
            add("dup", f"{u}-{v}", f"{len(ids)} edges between the same two nodes",
                "edges " + ", ".join(edges[i]["id"] for i in ids[:5]) +
                " · lengths " + ", ".join(f"{elen[i]:.0f} m" for i in ids[:5]),
                g=[edges[i]["p"] for i in ids], p=coord[u])

    # 4. nodes touching but not connected (both degree >= 2)
    nodes = [n for n in deg if deg[n] >= 2]
    tg = defaultdict(list)
    tc = a.touch_dist
    for n in nodes:
        x, y = mxy(coord[n])
        tg[(int(x // tc), int(y // tc))].append((n, x, y))
    adjacent = {tuple(sorted((e["a"], e["b"]))) for e in edges}
    seen_pairs = set()
    for n in nodes:
        x, y = mxy(coord[n])
        gx, gy = int(x // tc), int(y // tc)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m, x2, y2 in tg.get((gx + dx, gy + dy), ()):
                    pk = tuple(sorted((n, m)))
                    if m != n and pk not in seen_pairs and pk not in adjacent and math.hypot(x2 - x, y2 - y) <= tc:
                        seen_pairs.add(pk)
                        add("touch", f"{pk[0]}-{pk[1]}",
                            f"Nodes {math.hypot(x2 - x, y2 - y):.1f} m apart, not connected",
                            f"nodes {n} and {m}", p=coord[n])

    # 5. islands
    parent = {}

    def find(u):
        while parent.setdefault(u, u) != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u
    for e in edges:
        parent[find(e["a"])] = find(e["b"])
    comp = defaultdict(list)
    for i, e in enumerate(edges):
        comp[find(e["a"])].append(i)
    if comp:
        sizes = {r: sum(elen[i] for i in ids) for r, ids in comp.items()}
        main = max(sizes, key=sizes.get)
        for r, ids in sorted(comp.items(), key=lambda kv: sizes[kv[0]]):
            if r == main:
                continue
            first = min(ids, key=lambda i: ekey[i])
            add("island", ekey[first], f"Disconnected piece: {len(ids)} edge(s), {sizes[r] / 1000:.2f} km",
                "edges " + ", ".join(edges[i]["id"] for i in ids[:4]) + (" ..." if len(ids) > 4 else ""),
                g=[edges[i]["p"] for i in ids], p=edges[first]["p"][0])
        main_info = (len(comp), sizes[main] / 1000)
    else:
        main_info = (0, 0)

    # 6. kinks
    for n in deg:
        if deg[n] != 2 or len(set(inc[n])) != 2:
            continue
        i, j = inc[n]
        th = angle_between(dir_from(edges[i], n), dir_from(edges[j], n))
        if th is not None and th < a.kink_angle:
            add("kink", str(n), f"Sharp bend of {180 - th:.0f}° at a pass-through node",
                f"node {n} · edges {edges[i]['id']} and {edges[j]['id']}", p=coord[n],
                x=[edges[i]["p"], edges[j]["p"]])
    for i, e in enumerate(edges):
        if e["kind"] != "added":
            continue
        for n in (e["a"], e["b"]):
            if deg[n] < 3 or e["a"] == e["b"]:
                continue
            ths = []
            for j in set(inc[n]):
                if j != i and edges[j]["a"] != edges[j]["b"]:
                    th = angle_between(dir_from(e, n), dir_from(edges[j], n))
                    if th is not None:
                        ths.append(th)
            if ths and all(a.odd_angle < t < 180 - a.odd_angle for t in ths):
                add("kink", f"{ekey[i]}@{n}",
                    f"Added line meets the track at {min(ths, key=lambda t: abs(t - 90)):.0f}°",
                    f"node {n} · {elen[i]:.0f} m line", g=line(i), p=coord[n])

    # 7. added lines crossing tracks
    if any(e["kind"] == "added" for e in edges):
        sg = defaultdict(list)
        for i, e in enumerate(edges):
            pts = [mxy(q) for q in e["p"]]
            for k in range(len(pts) - 1):
                (x1, y1), (x2, y2) = pts[k], pts[k + 1]
                for cx in range(int(min(x1, x2) // 100), int(max(x1, x2) // 100) + 1):
                    for cy in range(int(min(y1, y2) // 100), int(max(y1, y2) // 100) + 1):
                        sg[(cx, cy)].append((i, pts[k], pts[k + 1]))
        for i, e in enumerate(edges):
            if e["kind"] != "added":
                continue
            ends = [mxy(e["p"][0]), mxy(e["p"][-1])]
            hits, done_seg = [], set()
            pts = [mxy(q) for q in e["p"]]
            for k in range(len(pts) - 1):
                (x1, y1), (x2, y2) = pts[k], pts[k + 1]
                for cx in range(int(min(x1, x2) // 100), int(max(x1, x2) // 100) + 1):
                    for cy in range(int(min(y1, y2) // 100), int(max(y1, y2) // 100) + 1):
                        for j, q1, q2 in sg.get((cx, cy), ()):
                            if j == i or (j, q1) in done_seg:
                                continue
                            done_seg.add((j, q1))
                            c = seg_cross(pts[k], pts[k + 1], q1, q2)
                            if c and all(math.hypot(c[0] - u[0], c[1] - u[1]) > 3 for u in ends):
                                hits.append((c, edges[j]["id"]))
            if hits:
                add("cross", ekey[i], f"Added line crosses {len(hits)} track(s)",
                    f"{elen[i]:.0f} m line · crosses " + ", ".join(h[1] for h in hits[:4]),
                    g=line(i), p=ll(hits[0][0]))

    # 8. long added lines, tiny links, complex nodes
    for i, e in enumerate(edges):
        if e["kind"] == "added" and elen[i] > a.long_add:
            add("long", ekey[i], f"Added line {elen[i]:.0f} m long", f"edge {e['id']}", g=line(i), p=e["p"][0])
        if elen[i] < a.tiny and deg[e["a"]] >= 3 and deg[e["b"]] >= 3 and e["a"] != e["b"]:
            add("tiny", ekey[i], f"Tiny edge {elen[i]:.0f} m between two junctions",
                f"edge {e['id']}", g=line(i), p=e["p"][0])
    for n, d in deg.items():
        if d >= 4:
            add("hidegree", str(n), f"Node with {d} edges", f"node {n}", p=coord[n])
    seen_split = set()
    for n, eid, pt in splits:
        if n not in seen_split:
            seen_split.add(n)
            add("split", str(n), "Track split here", f"node {n} on edge {eid}", p=pt)

    stats = {"edges": N, "nodes": len(deg), "km": round(sum(elen) / 1000, 1),
             "components": main_info[0], "main_km": round(main_info[1], 1), "stale": stale}
    return issues, edges, stats


# ------------------------------------------------------------------ report
def write_report(issues, edges, stats, path, with_network):
    order = {c[0]: i for i, c in enumerate(CATS)}
    issues.sort(key=lambda it: order[it["c"]])          # stable: keeps the per-category ordering
    for i, it in enumerate(issues):
        it["i"] = i
    data = {
        "cats": {c[0]: {"label": c[1], "color": c[2], "on": c[3], "desc": c[4]} for c in CATS},
        "issues": issues,
        "network": [e["p"] for e in edges] if with_network else [],
        "stats": stats,
    }
    html = HTML.replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description="Sanity-check the pruned rail network")
    ap.add_argument("--cache", default="rail_generators/rail_be.json")
    ap.add_argument("--edits", default="rail_generators/edits.json")
    ap.add_argument("--out", default=os.path.join("rail_generators/", "check_report.html"))
    ap.add_argument("--parallel-dist", type=float, default=15.0, help="same value as in the editor")
    ap.add_argument("--gap-dist", type=float, default=60.0, help="dead end counts as 'near a track' within this many m")
    ap.add_argument("--touch-dist", type=float, default=5.0)
    ap.add_argument("--kink-angle", type=float, default=100.0, help="pass-through nodes with an interior angle below this are flagged")
    ap.add_argument("--odd-angle", type=float, default=50.0)
    ap.add_argument("--long-add", type=float, default=300.0)
    ap.add_argument("--tiny", type=float, default=25.0)
    ap.add_argument("--no-network", action="store_true", help="leave the grey background network out of the report")
    ap.add_argument("--port", type=int, default=8766, help="port of the local page that shows the report")
    ap.add_argument("--no-serve", action="store_true", help="only write the HTML file, don't start the local server")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.cache):
        raise SystemExit(f"{a.cache} not found - run rail_editor.py once first (it downloads the data).")
    with open(a.cache, encoding="utf-8") as f:
        data = json.load(f)
    coords, adj = build_graph(data)
    chains = build_chains(coords, adj)
    st = load_state(a.edits)
    st, note = migrate_state(st, chains, coords)
    if note:
        print(f"edits.json is in the old ChatGPT format - converted in memory (file untouched): {note}")
    if not os.path.exists(a.edits):
        print(f"note: {a.edits} not found - checking the untouched network")
    issues, edges, stats = check_all(chains, st, a)
    write_report(issues, edges, stats, a.out, not a.no_network)

    cnt = defaultdict(int)
    for it in issues:
        cnt[it["c"]] += 1
    print(f"Network after your edits: {stats['edges']} edges, {stats['nodes']} nodes, {stats['km']} km")
    print(f"Connected pieces: {stats['components']} (main network {stats['main_km']} km)")
    for c in CATS:
        print(f"  {c[1]:<34}{cnt[c[0]]:>6}")
    if any(stats["stale"].values()):
        print(f"WARNING: edits.json refers to ids that are not in {a.cache}: {stats['stale']} "
              "(was the data re-downloaded with --refresh?)")
    print(f"Report file: {os.path.abspath(a.out)}")
    if a.no_serve:
        return
    # The report is served from http://localhost because the OpenStreetMap tile server refuses
    # requests that come from a file:// page (no Referer) - that gave an empty background map.
    out_path = os.path.abspath(a.out)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            with open(out_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    url = f"http://localhost:{a.port}"
    print(f"Report: {url}   (Ctrl+C to stop)")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Rail network check</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body{height:100%;margin:0;font:13px/1.4 system-ui,sans-serif;color:#222}
#app{display:flex;height:100%}
#side{width:400px;display:flex;flex-direction:column;border-right:1px solid #ccc;background:#fafafa}
#map{flex:1;background:#fff}
#head{padding:10px 12px;border-bottom:1px solid #ddd}
#head h1{font-size:15px;margin:0 0 3px}
#sum{color:#555;font-size:12px}
#cats{padding:6px 10px;border-bottom:1px solid #ddd;display:flex;flex-wrap:wrap;gap:4px}
.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid #bbb;border-radius:12px;padding:2px 8px;cursor:pointer;background:#fff;user-select:none;font-size:12px}
.chip.off{opacity:.45}
.dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:4px}
.chip .dot{margin:0}
#tools{padding:6px 10px;border-bottom:1px solid #ddd;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
button{padding:3px 9px;border:1px solid #888;border-radius:4px;background:#f4f4f4;cursor:pointer;font:inherit}
button.on{background:#2b6cb0;color:#fff;border-color:#2b6cb0}
#list{flex:1;overflow:auto}
.it{padding:7px 12px;border-bottom:1px solid #eee;cursor:pointer;display:flex;gap:8px;align-items:flex-start}
.it:hover{background:#f0f4f8}
.it.act{background:#fff3cd}
.it.dis{opacity:.45}
.it b{display:block;font-weight:600}
.it small{color:#666;word-break:break-word}
.it .ok{margin-left:auto;flex:none}
.more{padding:10px;text-align:center;color:#2b6cb0;cursor:pointer}
#empty{padding:20px;color:#555}
#keys{padding:5px 12px;border-top:1px solid #ddd;color:#666;font-size:11px}
</style></head><body>
<div id="app">
 <div id="side">
  <div id="fileWarn" style="display:none;background:#fff3cd;padding:6px 12px;font-size:12px;border-bottom:1px solid #e0c36a">
   Opened as a file: OpenStreetMap blocks map tiles here. Run <code>python rail_check.py</code> and use the localhost address it prints.</div>
  <div id="head"><h1>Rail network check</h1><div id="sum"></div></div>
  <div id="cats"></div>
  <div id="tools">
   <button id="bMap" class="on">Map (M)</button>
   <button id="bNet" class="on">Network (N)</button>
   <button id="bDis">Show dismissed</button>
  </div>
  <div id="list"></div>
  <div id="keys">↑/↓ or J/K: next/previous · X: dismiss (OK) and go next · M: map · N: network</div>
 </div>
 <div id="map"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const LS = 'railcheck.dismissed.v1';
let dismissed = new Set();
try { dismissed = new Set(JSON.parse(localStorage.getItem(LS) || '[]')); } catch (e) {}
const saveDismissed = () => { try { localStorage.setItem(LS, JSON.stringify([...dismissed])); } catch (e) {} };

if (location.protocol === 'file:') $('fileWarn').style.display = 'block';
const map = L.map('map', {preferCanvas: true}).setView([50.6, 4.5], 8);
const base = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution: '&copy; OpenStreetMap contributors', maxZoom: 19}).addTo(map);
let net = null;
if (D.network.length) net = L.polyline(D.network, {color: '#8a98a8', weight: 2, opacity: .8, interactive: false}).addTo(map);
let allPts = [];
D.network.forEach(g => g.forEach(p => allPts.push(p)));
if (allPts.length) map.fitBounds(L.latLngBounds(allPts));

const issues = D.issues, catOn = {};
Object.keys(D.cats).forEach(c => catOn[c] = D.cats[c].on);
let showDis = false, cur = -1, limit = 300, hl = [];

function makeLayers(it) {
  const col = D.cats[it.c].color, ls = [];
  (it.g || []).forEach(g => ls.push(L.polyline(g, {color: col, weight: 5, opacity: .9})));
  if (it.p) ls.push(L.circleMarker(it.p, {radius: 6, color: '#fff', weight: 1.5, fillColor: col, fillOpacity: 1}));
  ls.forEach(l => l.on('click', ev => { L.DomEvent.stopPropagation(ev); select(it.i, false); }));
  return ls;
}
issues.forEach(it => { it.layers = makeLayers(it); it.vis = false; });
const visible = it => catOn[it.c] && (showDis || !dismissed.has(it.k));
function applyVis() {
  issues.forEach(it => {
    const v = visible(it);
    if (v !== it.vis) { it.layers.forEach(l => v ? l.addTo(map) : map.removeLayer(l)); it.vis = v; }
  });
}

function bounds(it) {
  const pts = [];
  (it.g || []).forEach(g => g.forEach(p => pts.push(p)));
  if (it.p) pts.push(it.p);
  return L.latLngBounds(pts);
}
function clearHl() { hl.forEach(l => map.removeLayer(l)); hl = []; }
function select(i, fly) {
  cur = i;
  const it = issues[i];
  clearHl();
  (it.g || []).forEach(g => hl.push(L.polyline(g, {color: '#ffd400', weight: 14, opacity: .55, interactive: false}).addTo(map)));
  if (it.p) hl.push(L.circleMarker(it.p, {radius: 13, color: '#ffd400', weight: 4, fillOpacity: 0, interactive: false}).addTo(map));
  (it.x || []).forEach(g => hl.push(L.polyline(g, {color: '#000', weight: 2, dashArray: '5 5', interactive: false}).addTo(map)));
  it.layers.forEach(l => { if (l.bringToFront) l.bringToFront(); });
  if (fly) {
    const b = bounds(it);
    if (b.isValid() && !b.getSouthWest().equals(b.getNorthEast())) map.fitBounds(b.pad(0.8), {maxZoom: 17});
    else if (it.p) map.setView(it.p, Math.max(map.getZoom(), 16));
  }
  const vis = issues.filter(visible), idx = vis.findIndex(x => x.i === i);
  if (idx >= limit) limit = idx + 50;
  render();
  const el = document.querySelector(`.it[data-i="${i}"]`);
  if (el) el.scrollIntoView({block: 'nearest'});
}
function render() {
  const vis = issues.filter(visible);
  $('list').innerHTML = vis.length ? vis.slice(0, limit).map(it => {
    const d = dismissed.has(it.k);
    return `<div class="it${it.i === cur ? ' act' : ''}${d ? ' dis' : ''}" data-i="${it.i}">` +
      `<span class="dot" style="background:${D.cats[it.c].color}"></span>` +
      `<div><b>${esc(it.t)}</b><small>${esc(it.s || '')}</small></div>` +
      `<button class="ok" data-ok="${it.i}">${d ? 'undo' : 'OK'}</button></div>`;
  }).join('') + (vis.length > limit ? `<div class="more" id="more">Show more (${vis.length - limit} hidden)</div>` : '')
    : '<div id="empty">Nothing to show. Turn on more categories above, or all issues here are dismissed.</div>';
  const open = {};
  issues.forEach(it => { if (!dismissed.has(it.k)) open[it.c] = (open[it.c] || 0) + 1; });
  document.querySelectorAll('.chip').forEach(ch => { ch.querySelector('span:last-child').textContent = `${D.cats[ch.dataset.c].label} ${open[ch.dataset.c] || 0}`; });
  const nOpen = issues.filter(it => catOn[it.c] && !dismissed.has(it.k)).length;
  const s = D.stats;
  $('sum').textContent = `${s.edges} edges · ${s.nodes} nodes · ${s.km} km · ${s.components} connected piece(s) · ${nOpen} open issue(s) in shown categories`;
}
function dismiss(i) {
  const k = issues[i].k;
  dismissed.has(k) ? dismissed.delete(k) : dismissed.add(k);
  saveDismissed(); applyVis(); render();
}
function step(d) {
  const vis = issues.filter(visible);
  if (!vis.length) return;
  const idx = vis.findIndex(x => x.i === cur);
  const nxt = vis[Math.max(0, Math.min(vis.length - 1, idx < 0 ? (d > 0 ? 0 : vis.length - 1) : idx + d))];
  select(nxt.i, true);
}

$('list').addEventListener('click', e => {
  if (e.target.id === 'more') { limit += 300; render(); return; }
  const ok = e.target.closest('[data-ok]');
  if (ok) { dismiss(+ok.dataset.ok); return; }
  const it = e.target.closest('.it');
  if (it) select(+it.dataset.i, true);
});
Object.keys(D.cats).forEach(c => {
  const ch = document.createElement('div');
  ch.className = 'chip' + (catOn[c] ? '' : ' off');
  ch.dataset.c = c; ch.title = D.cats[c].desc;
  ch.innerHTML = `<span class="dot" style="background:${D.cats[c].color}"></span><span></span>`;
  ch.onclick = () => { catOn[c] = !catOn[c]; ch.classList.toggle('off', !catOn[c]); applyVis(); render(); };
  $('cats').appendChild(ch);
});
$('bMap').onclick = () => {
  const on = map.hasLayer(base);
  on ? map.removeLayer(base) : base.addTo(map);
  $('bMap').classList.toggle('on', !on);
};
$('bNet').onclick = () => {
  if (!net) return;
  const on = map.hasLayer(net);
  on ? map.removeLayer(net) : (net.addTo(map), net.bringToBack());
  $('bNet').classList.toggle('on', !on);
};
$('bDis').onclick = () => { showDis = !showDis; $('bDis').classList.toggle('on', showDis); applyVis(); render(); };
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'arrowdown' || k === 'j') { e.preventDefault(); step(1); }
  else if (k === 'arrowup' || k === 'k') { e.preventDefault(); step(-1); }
  else if (k === 'x' && cur >= 0) { const vis = issues.filter(visible), idx = vis.findIndex(v => v.i === cur); dismiss(cur); if (!showDis) { const v2 = issues.filter(visible); const nx = v2.find(v => v.i > cur) || v2[v2.length - 1]; if (nx) select(nx.i, true); } else step(1); }
  else if (k === 'm') $('bMap').click();
  else if (k === 'n') $('bNet').click();
});
applyVis(); render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
