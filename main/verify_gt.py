"""Independently verify that generator ground truth matches emitted geometry.

The connectivity score is only meaningful if the ground truth is true. Since the
same author wrote both the generator and the recovery pipeline, a shared
misconception would produce a perfect score that means nothing.

This check therefore uses a *different mechanism* from the pipeline. The
pipeline reasons about polygon strips between room footprints; this rasterises
the wall solids and flood-fills free space at door height, asking whether one
room's interior actually reaches another's. Agreement between two unrelated
methods is evidence the ground truth is real; disagreement localises the bug.

Doors are deliberately excluded from the raster: a doorway is a gap in the wall,
and the leaf that fills it is not what determines whether the opening exists.

    python main/verify_gt.py --dir dataset/synthetic_data/synthetic
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import parser as P

RES = 0.05          # m per cell; finer than the pipeline's 0.10 grid
SLICE_DZ = 1.20     # m above storey floor


def verify(ifc_path: str, gt_path: str) -> dict:
    gt = json.load(open(gt_path))
    model = P.parse(ifc_path)

    rooms = {r["rid"]: r for r in gt["rooms"]}
    storeys = sorted({r["storey"] for r in gt["rooms"]})
    st_elev = {s: s * 3.20 for s in storeys}

    # Wall footprints per storey, by elevation band (the generator writes one
    # storey per 3.2 m and we do not rely on IFC containment here).
    walls = {s: [] for s in storeys}
    for p in model.products:
        if p.ifc_type != "IfcWall" or p.footprint is None:
            continue
        s = int(round(p.zmin / 3.20))
        if s in walls:
            walls[s].append(p.footprint)

    checked = agree = 0
    disagreements = []

    truth = {frozenset((e["a"], e["b"])) for e in gt["edges"]
             if e["type"] != "vertical"}
    negatives = {frozenset((e["a"], e["b"])) for e in gt["adjacent_not_connected"]}

    for s in storeys:
        rs = [r for r in gt["rooms"] if r["storey"] == s]
        if not rs or not walls[s]:
            continue
        x0 = min(r["x0"] for r in rs) - 1.0
        y0 = min(r["y0"] for r in rs) - 1.0
        x1 = max(r["x1"] for r in rs) + 1.0
        y1 = max(r["y1"] for r in rs) + 1.0
        nx = int(np.ceil((x1 - x0) / RES))
        ny = int(np.ceil((y1 - y0) / RES))

        occ = np.zeros((ny, nx), dtype=bool)
        for poly in walls[s]:
            minx, miny, maxx, maxy = poly.bounds
            i0 = max(0, int((minx - x0) / RES)); i1 = min(nx, int(np.ceil((maxx - x0) / RES)))
            j0 = max(0, int((miny - y0) / RES)); j1 = min(ny, int(np.ceil((maxy - y0) / RES)))
            occ[j0:j1, i0:i1] = True

        def cell(px, py):
            return (int((py - y0) / RES), int((px - x0) / RES))

        # For each candidate pair, flood-fill from A restricted to A|B interiors.
        # Reaching B means a genuine opening joins them directly, rather than a
        # path looping through some third room.
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                key = frozenset((a["rid"], b["rid"]))
                if key not in truth and key not in negatives:
                    continue
                mask = np.zeros((ny, nx), dtype=bool)
                for r in (a, b):
                    ja, ia = cell(r["x0"], r["y0"])
                    jb, ib = cell(r["x1"], r["y1"])
                    mask[ja:jb, ia:ib] = True
                free = mask & ~occ
                lbl, _ = ndimage.label(free)
                cb = cell((b["x0"] + b["x1"]) / 2, (b["y0"] + b["y1"]) / 2)
                ca = cell((a["x0"] + a["x1"]) / 2, (a["y0"] + a["y1"]) / 2)
                la, lb = lbl[ca], lbl[cb]
                reachable = bool(la and la == lb)
                expected = key in truth
                checked += 1
                if reachable == expected:
                    agree += 1
                else:
                    disagreements.append({
                        "a": a["rid"], "b": b["rid"],
                        "gt_says": "connected" if expected else "separated",
                        "geometry_says": "connected" if reachable else "separated",
                    })

    return {"building": gt["building"], "checked": checked, "agree": agree,
            "disagreements": disagreements}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="dataset/synthetic_data/synthetic")
    args = ap.parse_args()

    tot = ok = 0
    bad = []
    for gt_path in sorted(glob.glob(os.path.join(args.dir, "*.gt.json"))):
        ifc = gt_path.replace(".gt.json", ".ifc")
        if not os.path.exists(ifc):
            continue
        r = verify(ifc, gt_path)
        tot += r["checked"]; ok += r["agree"]
        flag = "" if not r["disagreements"] else f"  <-- {len(r['disagreements'])} disagree"
        print(f"  {r['building']:<16} {r['agree']}/{r['checked']}{flag}")
        bad.extend([(r["building"], d) for d in r["disagreements"]])

    print(f"\nGROUND TRUTH VERIFICATION: {ok}/{tot} pairs agree "
          f"({100*ok/max(tot,1):.1f}%)")
    if bad:
        print("\nDisagreements (GT vs independent flood-fill):")
        for b, d in bad[:15]:
            print(f"  {b} {d['a']}-{d['b']}: gt={d['gt_says']}, "
                  f"geometry={d['geometry_says']}")
    else:
        print("Ground truth is consistent with the emitted geometry.")


if __name__ == "__main__":
    main()
