"""Derive room-connectivity reference data from IFC geometry, without annotation.

The files state no `IfcRelSpaceBoundary` (0 in 208 models), so room-room
connectivity is not readable from the schema.

Manual annotation is no longer blocked -- 15 buildings have been reviewed, 10
with complete verdict coverage -- so this deriver is now a **cross-check** on a
human reference rather than a substitute for one. Two unrelated mechanisms
agreeing is evidence; this one alone is not ground truth, and its negatives are
weaker than a human verdict wherever a doorway's void was never authored. But the *rooms* are stated: 188/208 files carry
`IfcSpace` with real footprints. Only the edges between them are missing, and an
edge is a geometric fact -- two rooms are joined iff free space at door height
runs continuously from one into the other.

This module recovers those edges by a mechanism deliberately unlike the
pipeline's. `topology.py` reasons in vector space, probing outward along each
door's placement normal to name the two rooms it joins. Here we rasterise the
wall solids and flood-fill free space, never looking at a door's orientation at
all. Two unrelated methods agreeing is evidence the reference is real; the
pipeline cannot score well merely by sharing a misconception with it.

Door *leaves* are excluded from the occupancy raster on purpose: a closed door
is still a doorway, and the leaf is not what decides whether the opening exists.
The opening itself, however, must be added back. A wall footprint is the XY
projection of the whole solid, and a doorway stops under a lintel at about
2.1 m, so in plan the projection seals it: on real files this alone made whole
buildings read as a set of disconnected rooms. `doorway_voids` recovers the
passage from `IfcOpeningElement` -- the void the modeller authored -- and
classifies it through the native `IfcRelFillsElement` chain, which is present in
the corpus even though `IfcRelSpaceBoundary` is not.

Because `IfcSpace` footprints stop at the inner wall face, two adjacent rooms
are separated by a band of wall thickness that belongs to neither polygon. The
flood therefore runs over `A | B | (A.buffer(t) & B.buffer(t))` -- the two rooms
plus only the strip between them. Confining the crossing to that shared strip
stops the fill from wrapping around a wall end and inventing a connection.

Every pair the raster cannot decide is emitted as `unknown` and excluded from
scoring rather than guessed. Real IFC is messy; a reference that guesses is
worse than one that abstains.

    # calibrate against known-true synthetic ground truth first
    python main/derive_real_gt.py --dir dataset/synthetic_data/synthetic --calibrate

    # then derive for the real corpus
    python main/derive_real_gt.py --dir dataset/2025_BIMfundamentals --out results/real_gt
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import Polygon

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import parser as P

RES = 0.05           # m per raster cell
# The bridging buffer is not a fixed constant: it is read off each pair's own
# geometry. Two IfcSpace footprints stop at opposite inner wall faces, so the
# distance between them *is* the wall thickness there. Buffering each polygon by
# that distance (plus a cell of slack) makes the two buffers overlap across
# exactly the wall band and no further. A fixed constant cannot do this -- on
# the synthetic corpus anything below 0.20 m fails to bridge a 0.20 m wall (69
# false negatives) and anything above 0.25 m wraps around wall ends (4 false
# positives); real walls vary far more than that.
BAND_SLACK = 0.04    # m added to the measured gap
BAND_MIN = 0.10      # m floor, for footprints that already abut
ADJ_TOL = 0.60       # m: two rooms are candidate-adjacent within this distance
DOOR_H = 1.00        # m above the room floor: the slice we flood
# Minimum footprint area, per class. One threshold for all of them discarded
# the geometry the reference depends on: a normal 0.9 x 0.2 m doorway void is
# 0.18 m^2, so every authored opening was thrown away and the flood then found
# every room sealed. A wall segment and a room are not the same kind of object
# and cannot share a validity test.
MIN_AREA = 0.5       # m^2: ignore degenerate IfcSpace footprints
MIN_BLOCKER_AREA = 0.01   # m^2: a thin wall stub still blocks passage
MIN_VOID_AREA = 0.02      # m^2: a doorway void is narrow by construction
MAX_SPACES_PER_STOREY = 200   # guard against pathological files

# Elements that block passage. Doors are deliberately absent (see module docstring);
# so are furnishings, which a robot can route around and which do not define topology.
BLOCKING = ("IfcWall", "IfcWallStandardCase", "IfcColumn", "IfcSlab", "IfcRoof")


def _clean(poly, min_area: float = MIN_AREA):
    """Repair a self-intersecting footprint, or drop it.

    `min_area` is per class and must be passed deliberately. The default is the
    room threshold, which is the largest of the three.
    """
    if poly is None or poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area < min_area:
        return None
    return poly


def _storey_of(model):
    """Group spaces by the storey the file assigns them to.

    Falls back to banding on elevation when containment is missing, which is
    common in the corpus. Grouping only has to be consistent, not correct:
    it exists to keep the raster small and to avoid pairing rooms on different
    floors, and a vertical z-overlap test guards each pair regardless.
    """
    groups = defaultdict(list)
    for s in model.spaces:
        if _clean(s.footprint) is None:
            continue
        key = s.storey_gid
        if key is None:
            key = f"z{round(s.zmin / 3.0)}"
        groups[key].append(s)
    return groups


def doorway_voids(ifc_path: str, scale: float = 1.0) -> list:
    """Footprints of the wall voids a person can walk through.

    A wall footprint is the XY projection of the whole solid, so a doorway --
    which stops at about 2.1 m under a lintel -- does not appear as a gap in
    plan. Projected, every wall reads as continuous and every room reads as
    sealed. The opening has to be put back explicitly.

    We take it from `IfcOpeningElement`, the void the modeller actually cut,
    and classify it through the native `IfcRelFillsElement` chain: an opening
    filled by an `IfcDoor` is a doorway, one filled by an `IfcWindow` is not.
    An unfilled opening counts only if it starts near the floor, which
    distinguishes an archway from a clerestory.

    Using the authored void rather than the door leaf is what keeps this
    reference independent of `topology.py`, which resolves rooms by probing
    along each door's placement normal. Nothing here reads that normal.

    `scale` is the **empirical** unit repair only (`model.scale_correction`),
    never `model.unit_scale`. IfcOpenShell emits metres whatever the file
    declares, so multiplying tessellated vertices by the declared unit scales
    them a second time: a millimetre file came out 1000x too large and every
    one of its openings was then discarded, while the metre-declared copy of
    the same building kept them. Two files describing one building disagreed
    about whether it had any doors.
    """
    import ifcopenshell
    import ifcopenshell.geom

    f = ifcopenshell.open(ifc_path)
    filled_by = {}
    for r in f.by_type("IfcRelFillsElement"):
        try:
            filled_by[r.RelatingOpeningElement.id()] = r.RelatedBuildingElement.is_a()
        except Exception:
            continue

    settings = ifcopenshell.geom.settings()
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except (AttributeError, TypeError):
        settings.set("use-world-coords", True)

    out = []
    for op in f.by_type("IfcOpeningElement"):
        kind = filled_by.get(op.id())
        if kind == "IfcWindow":
            continue                      # a window is not a passage
        try:
            sh = ifcopenshell.geom.create_shape(settings, op)
        except Exception:
            continue
        v = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3) * scale
        if len(v) < 3:
            continue
        poly = _clean(shapely.MultiPoint(v[:, :2]).convex_hull, MIN_VOID_AREA)
        if poly is None or not isinstance(poly, Polygon):
            continue
        out.append({"poly": poly, "zmin": float(v[:, 2].min()),
                    "zmax": float(v[:, 2].max()), "door": kind == "IfcDoor"})
    return out


def _wall_union(model, zlo, zhi, voids=None):
    """Union of blocking footprints in the z band, minus walk-through voids."""
    polys = []
    for p in model.products:
        if p.ifc_type not in BLOCKING or p.footprint is None:
            continue
        # A wall belonging to another storey must not occupy this one.
        if p.zmax <= zlo + 0.05 or p.zmin >= zhi - 0.05:
            continue
        q = _clean(p.footprint, MIN_BLOCKER_AREA)
        if q is not None:
            polys.append(q)
    if not polys:
        return None
    occ = shapely.union_all(polys)

    cut = []
    for v in (voids or []):
        # The void must span the slice we flood, and an unfilled one must also
        # start near the floor to count as a passage rather than a high vent.
        if not (v["zmin"] <= zlo + DOOR_H <= v["zmax"]):
            continue
        if not v["door"] and v["zmin"] > zlo + 0.35:
            continue
        cut.append(v["poly"])
    if cut:
        occ = occ.difference(shapely.union_all(cut))
    return occ if not occ.is_empty else None


def _raster(geom, x0, y0, nx, ny):
    """Boolean mask of cells whose centre falls inside geom."""
    xs = x0 + (np.arange(nx) + 0.5) * RES
    ys = y0 + (np.arange(ny) + 0.5) * RES
    gx, gy = np.meshgrid(xs, ys)
    return shapely.contains_xy(geom, gx.ravel(), gy.ravel()).reshape(ny, nx)


def _seed(mask, occ):
    """A free cell inside mask, or None if the room is fully occluded."""
    free = mask & ~occ
    if not free.any():
        return None
    js, is_ = np.nonzero(free)
    # Prefer a cell far from the boundary so the seed is robustly interior.
    dist = ndimage.distance_transform_edt(free)
    k = int(np.argmax(dist[js, is_]))
    return int(js[k]), int(is_[k])


def derive(ifc_path: str) -> dict:
    """Return the connectivity reference for one model."""
    model = P.parse(ifc_path)
    name = os.path.splitext(os.path.basename(ifc_path))[0]
    try:
        # The empirical repair only: the mesh is already in metres.
        voids = doorway_voids(ifc_path, model.scale_correction)
    except Exception:
        voids = []

    rooms, edges, negatives, unknown = [], [], [], []
    seen_rooms = set()

    for st_key, spaces in _storey_of(model).items():
        if len(spaces) < 2 or len(spaces) > MAX_SPACES_PER_STOREY:
            continue
        polys = {s.gid: _clean(s.footprint) for s in spaces}
        polys = {k: v for k, v in polys.items() if v is not None}
        if len(polys) < 2:
            continue

        zlo = float(np.median([s.zmin for s in spaces]))
        zhi = float(np.median([s.zmax for s in spaces]))
        if zhi <= zlo:
            zhi = zlo + 2.4
        occ_geom = _wall_union(model, zlo + 0.05, min(zhi, zlo + DOOR_H + 0.4), voids)

        # One raster per storey, sized to the rooms plus a margin for the band.
        allp = shapely.union_all(list(polys.values()))
        minx, miny, maxx, maxy = allp.bounds
        x0, y0 = minx - 1.0, miny - 1.0
        nx = int(np.ceil((maxx + 1.0 - x0) / RES))
        ny = int(np.ceil((maxy + 1.0 - y0) / RES))
        if nx <= 0 or ny <= 0 or nx * ny > 40_000_000:
            continue

        occ = (_raster(occ_geom, x0, y0, nx, ny) if occ_geom is not None
               else np.zeros((ny, nx), dtype=bool))
        room_mask = {g: _raster(p, x0, y0, nx, ny) for g, p in polys.items()}

        for s in spaces:
            if s.gid in polys and s.gid not in seen_rooms:
                seen_rooms.add(s.gid)
                rooms.append({
                    "rid": s.gid, "name": s.name, "long_name": s.long_name,
                    "storey": str(st_key), "area": round(polys[s.gid].area, 2),
                })

        gids = sorted(polys)
        for i in range(len(gids)):
            for j in range(i + 1, len(gids)):
                ga, gb = gids[i], gids[j]
                pa, pb = polys[ga], polys[gb]
                gap = pa.distance(pb)
                if gap > ADJ_TOL:
                    continue     # not adjacent: no edge is even a candidate

                band = max(gap + BAND_SLACK, BAND_MIN)
                strip = pa.buffer(band).intersection(pb.buffer(band))
                region = shapely.union_all([pa, pb, strip])
                reg = _raster(region, x0, y0, nx, ny)
                free = reg & ~occ

                sa = _seed(room_mask[ga], occ)
                sb = _seed(room_mask[gb], occ)
                pair = {"a": ga, "b": gb}
                if sa is None or sb is None:
                    pair["reason"] = "room fully occluded by blocking geometry"
                    unknown.append(pair)
                    continue

                lbl, _ = ndimage.label(free)
                la, lb = lbl[sa], lbl[sb]
                if not la or not lb:
                    pair["reason"] = "seed outside flood region"
                    unknown.append(pair)
                    continue
                (edges if la == lb else negatives).append(pair)

    # Whether the *file* encodes passages at all, decided per model.
    #
    # A doorway reaches the raster in one of two ways: the modeller cut an
    # IfcOpeningElement, or the walls are drawn as segments with a physical gap.
    # Some files do neither -- they place IfcDoor products against unbroken wall
    # solids -- and there the geometry simply does not contain the answer. The
    # flood then separates every pair and would emit a long list of confident
    # negatives that are artifacts of the modelling style, not facts about the
    # building. Those models are marked and excluded from scoring instead.
    n_doors = sum(1 for p in model.products if p.ifc_type == "IfcDoor")
    reason = None
    if len(rooms) < 2:
        reason = "fewer than two IfcSpace with usable footprints"
    elif not edges and not negatives:
        reason = "no candidate-adjacent room pairs"
    elif n_doors and not edges:
        reason = (f"{n_doors} doors but no traversable opening: walls are "
                  f"unbroken solids and {len(voids)} voids were authored")
    elif len(rooms) > 2 and not negatives:
        reason = "no separated pair among candidates: blocking geometry may be absent"

    return {
        "building": name,
        "source": "derived:flood-fill",
        "derivable": reason is None,
        "excluded_because": reason,
        "n_doors": n_doors,
        "n_voids": len(voids),
        "params": {"res": RES, "band_slack": BAND_SLACK, "band_min": BAND_MIN, "adj_tol": ADJ_TOL,
                   "door_h": DOOR_H, "min_room_area": MIN_AREA,
                   "min_blocker_area": MIN_BLOCKER_AREA,
                   "min_void_area": MIN_VOID_AREA,
                   "void_scale": model.scale_correction,
                   "declared_unit_scale": model.declared_unit_scale},
        "rooms": rooms,
        "edges": edges,
        "adjacent_not_connected": negatives,
        "unknown": unknown,
    }


def calibrate(ifc_path: str, gt_path: str) -> dict:
    """Score the deriver against known-true generator ground truth."""
    truth = json.load(open(gt_path))
    got = derive(ifc_path)

    # The generator names rooms by rid ("S0R000"); the deriver names them by
    # IfcSpace gid. The generator writes the rid into the IfcSpace *name*
    # (long_name carries the room label, e.g. "Bedroom"), so join on name.
    pos = {frozenset((e["a"], e["b"])) for e in truth["edges"] if e["type"] != "vertical"}
    neg = {frozenset((e["a"], e["b"])) for e in truth["adjacent_not_connected"]}

    by_gid = {r["rid"]: r["name"] for r in got["rooms"]}

    def key(p):
        a, b = by_gid.get(p["a"]), by_gid.get(p["b"])
        return frozenset((a, b)) if a and b else None

    tp = fp = fn = tn = 0
    for p in got["edges"]:
        k = key(p)
        if k is None:
            continue
        if k in pos:
            tp += 1
        elif k in neg:
            fp += 1
    for p in got["adjacent_not_connected"]:
        k = key(p)
        if k is None:
            continue
        if k in pos:
            fn += 1
        elif k in neg:
            tn += 1

    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"building": got["building"], "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3),
            "unknown": len(got["unknown"])}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="directory of .ifc files")
    ap.add_argument("--out", default="results/real_gt", help="output directory")
    ap.add_argument("--calibrate", action="store_true",
                    help="score against .gt.json instead of emitting (synthetic only)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.dir, "*.ifc")))
    if args.limit:
        paths = paths[: args.limit]

    if args.calibrate:
        rows, agg = [], {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unknown": 0}
        for p in paths:
            gt = os.path.splitext(p)[0] + ".gt.json"
            if not os.path.exists(gt):
                continue
            try:
                r = calibrate(p, gt)
            except Exception as e:
                print(f"  {os.path.basename(p)}: ERROR {e}")
                continue
            rows.append(r)
            for k in agg:
                agg[k] += r[k]
            print(f"  {r['building']:<16} P {r['precision']:.3f}  R {r['recall']:.3f}"
                  f"  F1 {r['f1']:.3f}  (tp{r['tp']} fp{r['fp']} fn{r['fn']} tn{r['tn']}"
                  f" unk{r['unknown']})")
        p_ = agg["tp"] / (agg["tp"] + agg["fp"]) if agg["tp"] + agg["fp"] else 0.0
        r_ = agg["tp"] / (agg["tp"] + agg["fn"]) if agg["tp"] + agg["fn"] else 0.0
        f_ = 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0
        print(f"\nCALIBRATION over {len(rows)} buildings")
        print(f"  pooled precision {p_:.3f}  recall {r_:.3f}  F1 {f_:.3f}")
        print(f"  tp {agg['tp']}  fp {agg['fp']}  fn {agg['fn']}  tn {agg['tn']}"
              f"  unknown {agg['unknown']}")
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        json.dump({"per_building": rows, "pooled": {
            "precision": round(p_, 3), "recall": round(r_, 3), "f1": round(f_, 3), **agg}},
            open("results/real_gt_calibration.json", "w"), indent=2)
        print("  -> results/real_gt_calibration.json")
        return

    os.makedirs(args.out, exist_ok=True)
    tot = {"rooms": 0, "edges": 0, "neg": 0, "unknown": 0, "models": 0, "usable": 0}
    excluded = defaultdict(list)
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            r = derive(p)
        except Exception as e:
            print(f"  {name:<16} ERROR {str(e)[:70]}")
            excluded["parse error"].append(name)
            continue
        json.dump(r, open(os.path.join(args.out, f"{name}.realgt.json"), "w"), indent=2)
        tot["models"] += 1
        flag = ""
        if r["derivable"]:
            tot["usable"] += 1
            tot["rooms"] += len(r["rooms"])
            tot["edges"] += len(r["edges"])
            tot["neg"] += len(r["adjacent_not_connected"])
            tot["unknown"] += len(r["unknown"])
        else:
            key = r["excluded_because"].split(":")[0].split(" but ")[0]
            excluded[key].append(name)
            flag = f"  EXCLUDED ({r['excluded_because']})"
        print(f"  {name:<16} {len(r['rooms']):3d} rooms  {len(r['edges']):3d} edges"
              f"  {len(r['adjacent_not_connected']):3d} neg  {len(r['unknown']):2d} unk{flag}")

    print(f"\n{tot['models']} models parsed -> {args.out}")
    print(f"  usable reference: {tot['usable']} models")
    print(f"  rooms {tot['rooms']}  edges {tot['edges']}  negatives {tot['neg']}"
          f"  unknown {tot['unknown']}")
    if excluded:
        print("  excluded:")
        for k, v in sorted(excluded.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(v):3d}  {k}")
    json.dump({"totals": tot, "excluded": {k: v for k, v in excluded.items()}},
              open(os.path.join(args.out, "_summary.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
