"""Per-storey architectural floorplans (PNG + SVG) from our own parsed model.

A colleague's standalone `floorplan.py` (repo root) does this well for one
half of the job and badly for the other. Checked before integrating anything
(see README.md, "Things that cost real debugging time"): its storey/unit
detection trusts `ifcopenshell.util.unit.calculate_unit_scale()` with no
empirical check and reports `model_2` as a 2.15 km-tall building -- worse
than what `bimsg.parser`/`bimsg.topology.assign_storeys` already do for the
exact same file. Its horizontal-section geometry (cut a plane through the
triangle mesh, chain the resulting segments into polylines, simplify with
Douglas-Peucker) and its PNG/SVG rendering, by contrast, are a genuine
capability this project didn't have -- clean architectural line-plan output,
not a footprint-polygon fill. This module keeps that half only, ported
in `_section_segments`/`_chain_segments`/`_rdp`/render functions below, and
throws away the storey/unit detection entirely in favour of our own already
unit-corrected, empirically-repaired `ParsedModel`.

    python main/render_floorplan.py dataset/test_set/model_2.ifc -o out/
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import parser as P

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# --------------------------------------------------------------------------
# styling
# --------------------------------------------------------------------------

DEFAULT_INCLUDE = {
    "IfcWall", "IfcWallStandardCase", "IfcColumn", "IfcCurtainWall",
    "IfcMember", "IfcPlate", "IfcDoor", "IfcWindow", "IfcStair",
    "IfcStairFlight", "IfcRailing", "IfcSlab", "IfcRamp", "IfcBeam",
}
STYLE_HEAVY = {"IfcWall", "IfcWallStandardCase", "IfcColumn",
               "IfcCurtainWall", "IfcPlate", "IfcMember"}
STYLE_MEDIUM = {"IfcDoor", "IfcWindow"}

STYLES = {
    "heavy":  ((20, 20, 20),   2.6),
    "medium": ((90, 90, 95),   1.5),
    "light":  ((150, 150, 155), 1.0),
    "space":  ((175, 190, 205), 1.0),
    "space_inferred": ((175, 190, 205), 1.0),
}
SPACE_FILL = (243, 246, 249)
SPACE_FILL_INFERRED = (247, 240, 227)   # a room IFC never stated, recovered geometrically
BG = (255, 255, 255)


def style_for(cls: str) -> str:
    if cls in STYLE_HEAVY:
        return "heavy"
    if cls in STYLE_MEDIUM:
        return "medium"
    return "light"


# --------------------------------------------------------------------------
# horizontal-section geometry
# (ported from floorplan.py; pure mesh math, independent of IFC parsing)
# --------------------------------------------------------------------------

def _section_segments(verts, faces, z):
    """Intersect a triangle soup with the horizontal plane z. -> (N,2,2) XY segments."""
    if len(faces) == 0:
        return np.zeros((0, 2, 2))

    z = z + 1e-7  # nudge so no vertex lands exactly on the plane
    d = verts[:, 2] - z
    td = d[faces]

    crossing = (td > 0).any(axis=1) & (td < 0).any(axis=1)
    if not crossing.any():
        return np.zeros((0, 2, 2))

    tf, tdc = faces[crossing], td[crossing]
    n = len(tf)
    masks = np.zeros((n, 3), dtype=bool)
    pts = np.zeros((n, 3, 2))

    for k, (a, b) in enumerate(((0, 1), (1, 2), (2, 0))):
        da, db = tdc[:, a], tdc[:, b]
        masks[:, k] = (da * db) < 0
        denom = da - db
        t = np.zeros(n)
        safe = np.abs(denom) > 1e-15
        t[safe] = da[safe] / denom[safe]
        va = verts[tf[:, a]][:, :2]
        vb = verts[tf[:, b]][:, :2]
        pts[:, k, :] = va + t[:, None] * (vb - va)

    keep = masks.sum(axis=1) == 2
    if not keep.any():
        return np.zeros((0, 2, 2))
    masks, pts = masks[keep], pts[keep]

    sel = np.argsort(~masks, axis=1, kind="stable")[:, :2]
    p0 = np.take_along_axis(pts, sel[:, 0][:, None, None], axis=1)[:, 0, :]
    p1 = np.take_along_axis(pts, sel[:, 1][:, None, None], axis=1)[:, 0, :]
    segs = np.stack([p0, p1], axis=1)
    return segs[np.linalg.norm(segs[:, 0] - segs[:, 1], axis=1) > 1e-9]


def _chain_segments(segs, tol=1e-3):
    """Stitch loose section segments into polylines."""
    if len(segs) == 0:
        return []
    segs = np.asarray(segs)
    q = np.round(segs / tol).astype(np.int64)

    adj, coord = {}, {}
    for i, s in enumerate(q):
        a, b = tuple(s[0]), tuple(s[1])
        if a == b:
            continue
        coord.setdefault(a, segs[i][0])
        coord.setdefault(b, segs[i][1])
        adj.setdefault(a, []).append((b, i))
        adj.setdefault(b, []).append((a, i))

    used, polys = set(), []

    def walk(start):
        chain, cur = [start], start
        while True:
            nxt = None
            for other, ei in adj.get(cur, ()):
                if ei not in used:
                    nxt = (other, ei)
                    break
            if nxt is None:
                return chain
            used.add(nxt[1])
            chain.append(nxt[0])
            cur = nxt[0]

    starts = [k for k, v in adj.items() if len(v) % 2 == 1] + list(adj.keys())
    for st in starts:
        while any(ei not in used for _o, ei in adj.get(st, ())):
            ch = walk(st)
            if len(ch) > 1:
                polys.append([[float(coord[p][0]), float(coord[p][1])] for p in ch])
    return polys


def _rdp(points, eps):
    """Iterative Douglas-Peucker. Strips collinear triangulation noise."""
    n = len(points)
    if n < 3 or eps <= 0:
        return points
    pts = np.asarray(points, dtype=float)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        ab = b - a
        L = float(np.hypot(*ab))
        seg = pts[i + 1:j]
        if L < 1e-12:
            dist = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            dist = np.abs(ab[0] * (a[1] - seg[:, 1]) - (a[0] - seg[:, 0]) * ab[1]) / L
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    out = [points[i] for i in range(n) if keep[i]]
    return out if len(out) >= 2 else points


def _polygon_area(pts):
    a = 0.0
    for i in range(len(pts) - 1):
        a += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(a) / 2.0


# --------------------------------------------------------------------------
# extraction: our model -> per-storey plan dict
# --------------------------------------------------------------------------

def extract(model, spaces_obj, cut_height=1.2, include=None, simplify=0.01,
            min_len=0.02, chain_tol=1e-3):
    """Build a plan dict from an already-parsed, storey-repaired ParsedModel.

    `spaces_obj` is the full recovered room list (stated + inferred), e.g.
    `graph.build(model).graph["spaces_obj"]` -- reusing the same room
    recovery the rest of the pipeline uses, not re-deriving rooms from
    IfcSpace the way the colleague's script does (which finds none on files
    where IfcSpace is sparse or absent).
    """
    include = set(include or DEFAULT_INCLUDE)
    by_storey_products = {}
    for p in model.products:
        if p.ifc_type not in include or p.verts is None:
            continue
        by_storey_products.setdefault(p.storey_gid, []).append(p)

    by_storey_spaces = {}
    for sp in spaces_obj:
        by_storey_spaces.setdefault(sp.storey_gid, []).append(sp)

    storeys_out = []
    for i, st in enumerate(model.storeys):
        pz = st.elevation + cut_height
        elements, spaces_json, xs, ys = [], [], [], []

        for sp in by_storey_spaces.get(st.gid, []):
            if sp.footprint is None or sp.footprint.is_empty:
                continue
            ring = [[round(x, 4), round(y, 4)] for x, y in sp.footprint.exterior.coords]
            cx, cy = sp.footprint.centroid.x, sp.footprint.centroid.y
            spaces_json.append({
                "global_id": sp.gid, "name": sp.long_name or sp.name or "room",
                "source": sp.source,
                "polygon": ring,
                "area_m2": round(sp.footprint.area, 3),
                "centroid": [round(cx, 4), round(cy, 4)],
            })
            xs += [p[0] for p in ring]
            ys += [p[1] for p in ring]

        for p in by_storey_products.get(st.gid, []):
            segs = _section_segments(p.verts, p.faces, pz)
            polys = _chain_segments(segs, chain_tol)
            clean = []
            for pl in polys:
                s = _rdp(pl, simplify)
                if len(s) < 2:
                    continue
                if sum(math.dist(s[k], s[k + 1]) for k in range(len(s) - 1)) >= min_len:
                    clean.append([[round(x, 4), round(y, 4)] for x, y in s])
            if not clean:
                continue
            for pl in clean:
                xs += [pt[0] for pt in pl]
                ys += [pt[1] for pt in pl]
            elements.append({
                "global_id": p.gid, "ifc_class": p.ifc_type, "name": p.name,
                "style": style_for(p.ifc_type), "polylines": clean,
            })

        bounds = None
        if xs:
            bounds = {"min": [round(min(xs), 4), round(min(ys), 4)],
                      "max": [round(max(xs), 4), round(max(ys), 4)]}

        storeys_out.append({
            "index": i, "name": st.name or f"Storey {i}", "global_id": st.gid,
            "elevation_m": round(st.elevation, 4), "cut_plane_z_m": round(pz, 4),
            "bounds": bounds,
            "element_count": len(elements), "space_count": len(spaces_json),
            "elements": elements, "spaces": spaces_json,
        })

    return {
        "source_file": model.path, "schema": model.schema,
        "project": model.project_name, "units": "m",
        "params": {"cut_height": cut_height, "include": sorted(include),
                   "simplify": simplify},
        "level_count": len(storeys_out), "storeys": storeys_out,
    }


# --------------------------------------------------------------------------
# rendering (ported from floorplan.py; generic over the plan-dict shape above)
# --------------------------------------------------------------------------

def _plan_bounds(storey, pad=0.5):
    b = storey["bounds"]
    if not b:
        return None
    return (b["min"][0] - pad, b["min"][1] - pad, b["max"][0] + pad, b["max"][1] + pad)


def view_transform(storey, width=1800, margin=40, pad=0.5):
    """World metres -> pixels.  px = s*x + tx ,  py = -s*y + ty"""
    bb = _plan_bounds(storey, pad)
    if not bb:
        return None
    minx, miny, maxx, maxy = bb
    w, h = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    s = (width - 2 * margin) / max(w, h)
    W = int(round(w * s + 2 * margin))
    H = int(round(h * s + 2 * margin))
    tx = margin - minx * s
    ty = H - margin + miny * s
    return {"px_per_m": s, "tx": tx, "ty": ty, "width": W, "height": H}


def _font(size):
    if not HAVE_PIL:
        return None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def render_png(storey, path, tr, title=None, labels=True, scale_bar=True, ss=2):
    """Raster floorplan via Pillow, supersampled for antialiasing.

    Inferred rooms (recovered geometrically, never stated by the file) are
    filled a different tint and outlined dashed, matching the convention
    already used in `annotator/` -- dashed = recovered, solid = IFC-stated.
    """
    if not HAVE_PIL:
        raise RuntimeError("Pillow is required for PNG output (pip install Pillow)")

    W, H = tr["width"] * ss, tr["height"] * ss
    s, tx, ty = tr["px_per_m"] * ss, tr["tx"] * ss, tr["ty"] * ss
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    def P(pt):
        return (pt[0] * s + tx, -pt[1] * s + ty)

    def dashed_polygon(dr, pts, fill, width, dash=10, gap=6):
        for a, b in zip(pts, pts[1:] + pts[:1]):
            ax, ay = a
            bx, by = b
            length = math.hypot(bx - ax, by - ay)
            if length < 1e-6:
                continue
            n = max(1, int(length // (dash + gap)))
            for k in range(n + 1):
                t0 = k * (dash + gap) / length
                t1 = min(1.0, t0 + dash / length)
                if t0 >= 1.0:
                    break
                p0 = (ax + (bx - ax) * t0, ay + (by - ay) * t0)
                p1 = (ax + (bx - ax) * t1, ay + (by - ay) * t1)
                dr.line([p0, p1], fill=fill, width=width)

    for sp in storey["spaces"]:
        if len(sp["polygon"]) >= 3:
            fill = SPACE_FILL_INFERRED if sp["source"] == "inferred" else SPACE_FILL
            dr.polygon([P(p) for p in sp["polygon"]], fill=fill)
    for sp in storey["spaces"]:
        col, w = STYLES["space"]
        pts = [P(p) for p in sp["polygon"]]
        if len(pts) < 2:
            continue
        if sp["source"] == "inferred":
            dashed_polygon(dr, pts, fill=col, width=max(1, int(w * ss)))
        else:
            dr.line(pts + [pts[0]], fill=col, width=max(1, int(w * ss)), joint="curve")

    for bucket in ("light", "medium", "heavy"):
        col, w = STYLES[bucket]
        for el in storey["elements"]:
            if el["style"] != bucket:
                continue
            for pl in el["polylines"]:
                if len(pl) >= 2:
                    dr.line([P(p) for p in pl], fill=col, width=max(1, int(w * ss)), joint="curve")

    if labels and storey["spaces"]:
        f_room = _font(int(13 * ss))
        f_area = _font(int(10 * ss))
        for sp in storey["spaces"]:
            if sp["area_m2"] < 1.5:
                continue
            x, y = P(sp["centroid"])
            name = sp["name"] or ""
            suffix = "?" if sp["source"] == "inferred" else ""
            if name:
                dr.text((x, y - 7 * ss), name + suffix, fill=(60, 70, 85), font=f_room, anchor="mm")
            dr.text((x, y + 7 * ss), f"{sp['area_m2']:.1f} m2",
                    fill=(130, 140, 155), font=f_area, anchor="mm")

    if title:
        dr.text((14 * ss, 12 * ss), title, fill=(30, 30, 35), font=_font(int(15 * ss)))

    if scale_bar:
        target = 5.0
        while target * tr["px_per_m"] > tr["width"] * 0.28:
            target /= 2.0
        while target * tr["px_per_m"] < tr["width"] * 0.08:
            target *= 2.0
        L = target * s
        x0, y0 = 14 * ss, H - 20 * ss
        dr.line([(x0, y0), (x0 + L, y0)], fill=(60, 60, 65), width=max(1, 2 * ss))
        for xx in (x0, x0 + L):
            dr.line([(xx, y0 - 4 * ss), (xx, y0 + 4 * ss)], fill=(60, 60, 65), width=max(1, 2 * ss))
        dr.text((x0 + L + 8 * ss, y0), f"{target:g} m", fill=(60, 60, 65),
                font=_font(int(11 * ss)), anchor="lm")

    img = img.resize((tr["width"], tr["height"]), Image.LANCZOS)
    img.save(path)
    return path


def render_svg(storey, path, tr, title=None, labels=True):
    """Vector floorplan. No dependencies. Inferred rooms get a dashed outline."""
    s, tx, ty = tr["px_per_m"], tr["tx"], tr["ty"]

    def P(pt):
        return f"{pt[0] * s + tx:.2f},{-pt[1] * s + ty:.2f}"

    def rgb(c):
        return "#%02x%02x%02x" % c

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{tr["width"]}" '
           f'height="{tr["height"]}" viewBox="0 0 {tr["width"]} {tr["height"]}">',
           f'<rect width="100%" height="100%" fill="{rgb(BG)}"/>',
           '<g fill="none" stroke-linecap="round" stroke-linejoin="round">']

    for sp in storey["spaces"]:
        if len(sp["polygon"]) >= 3:
            fill = SPACE_FILL_INFERRED if sp["source"] == "inferred" else SPACE_FILL
            out.append(f'<polygon points="{" ".join(P(p) for p in sp["polygon"])}" '
                       f'fill="{rgb(fill)}" stroke="none"/>')
    col, w = STYLES["space"]
    for sp in storey["spaces"]:
        if len(sp["polygon"]) >= 2:
            dash = ' stroke-dasharray="6,4"' if sp["source"] == "inferred" else ""
            out.append(f'<polygon points="{" ".join(P(p) for p in sp["polygon"])}" '
                       f'stroke="{rgb(col)}" stroke-width="{w}" fill="none"{dash}/>')

    for bucket in ("light", "medium", "heavy"):
        col, w = STYLES[bucket]
        out.append(f'<g stroke="{rgb(col)}" stroke-width="{w}">')
        for el in storey["elements"]:
            if el["style"] != bucket:
                continue
            for pl in el["polylines"]:
                if len(pl) >= 2:
                    out.append(f'<polyline points="{" ".join(P(p) for p in pl)}"/>')
        out.append("</g>")
    out.append("</g>")

    if labels:
        for sp in storey["spaces"]:
            if sp["area_m2"] < 1.5:
                continue
            x, y = sp["centroid"][0] * s + tx, -sp["centroid"][1] * s + ty
            suffix = "?" if sp["source"] == "inferred" else ""
            if sp["name"]:
                out.append(f'<text x="{x:.1f}" y="{y - 3:.1f}" font-family="sans-serif" '
                           f'font-size="13" fill="#3c4655" text-anchor="middle">{sp["name"]}{suffix}</text>')
            out.append(f'<text x="{x:.1f}" y="{y + 12:.1f}" font-family="sans-serif" '
                       f'font-size="10" fill="#828c9b" text-anchor="middle">'
                       f'{sp["area_m2"]:.1f} m²</text>')
    if title:
        out.append(f'<text x="14" y="26" font-family="sans-serif" font-size="15" '
                   f'fill="#1e1e23">{title}</text>')
    out.append("</svg>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def slug(name):
    s = re.sub(r"[^A-Za-z0-9]+", "_", (name or "level")).strip("_").lower()
    return s or "level"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Per-storey architectural floorplans (PNG+SVG) from our own pipeline.")
    ap.add_argument("ifc", help="input .ifc file")
    ap.add_argument("-o", "--out", default="floorplans", help="output directory")
    ap.add_argument("--cut-height", type=float, default=1.2,
                    help="cut plane height above each storey, in metres")
    ap.add_argument("--format", choices=["png", "svg", "both"], default="both")
    ap.add_argument("--width", type=int, default=1800)
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    def log(msg):
        if not a.quiet:
            print(msg, file=sys.stderr)

    t0 = time.time()
    model = P.parse(a.ifc, with_meshes=True)
    full = G.build(model)
    spaces_obj = full.graph.get("spaces_obj", model.spaces)
    log(f"parsed {a.ifc}: {len(model.storeys)} storeys, {len(spaces_obj)} rooms, "
        f"{time.time()-t0:.1f}s")

    plan = extract(model, spaces_obj, cut_height=a.cut_height)

    stem = os.path.splitext(os.path.basename(a.ifc))[0]
    out_dir = os.path.join(a.out, stem)
    os.makedirs(out_dir, exist_ok=True)

    for st in plan["storeys"]:
        tr = view_transform(st, width=a.width)
        st["transform"] = tr
        if not tr:
            log(f"  [{st['index']}] {st['name']}: no geometry, skipped")
            continue
        base = os.path.join(out_dir, f"{stem}_{st['index']:02d}_{slug(st['name'])}")
        title = f"{st['name']}   +{st['elevation_m']:.2f} m"
        st["images"] = {}
        if a.format in ("png", "both"):
            st["images"]["png"] = render_png(st, base + ".png", tr, title=title,
                                              labels=not a.no_labels)
        if a.format in ("svg", "both"):
            st["images"]["svg"] = render_svg(st, base + ".svg", tr, title=title,
                                              labels=not a.no_labels)
        log(f"  [{st['index']}] {st['name']}: {st['element_count']} elements, "
            f"{st['space_count']} rooms")

    jpath = os.path.join(out_dir, f"{stem}_floorplans.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)
    log(f"wrote {jpath} and drawings in {out_dir}{os.sep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
