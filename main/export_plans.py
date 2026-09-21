"""Pre-extract per-storey floor plans for the annotation tool.

Tessellating an IFC takes 10-60 s, which is far too slow to do while an
annotator waits. This exports everything the browser needs -- wall outlines,
door positions, room polygons and the predicted topology -- into one JSON per
model, so the tool opens a plan instantly.

    python main/export_plans.py 'dataset/test_set/*.ifc' --out annotator/data
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import parser as P
from bimsg import topology

# Simplify tolerance for display geometry: keeps plans legible and the payload
# small without moving anything by more than a few centimetres.
SIMPLIFY = 0.04
MAX_WALL_POLYS = 4000


def _coords(seq):
    return [[round(x, 3), round(y, 3)] for x, y in seq]


def ring(poly, tol=SIMPLIFY):
    """Exterior ring only. Kept for walls, which never have holes."""
    r = rings(poly, tol)
    return r[0] if r else None


def rings(poly, tol=SIMPLIFY):
    """Exterior ring first, then any holes.

    A recovered circulation region is the free space *between* rooms, so it is
    a polygon with a hole punched out for each room it surrounds. Exporting
    only the exterior ring drew it as a solid slab covering every room on the
    floor: on model_0's ground floor the stored area is 360 m2 while the
    exterior ring encloses 599 m2, and all fifteen rooms sat underneath it.
    They stayed clickable only because rooms are drawn largest-first, which is
    luck rather than design, and the plan misrepresented what was recovered.
    """
    if poly is None or poly.is_empty:
        return None
    p = poly.simplify(tol, preserve_topology=True)
    if p.is_empty or p.geom_type != "Polygon":
        if p.geom_type == "MultiPolygon":
            p = max(p.geoms, key=lambda g: g.area)
        else:
            return None
    return [_coords(p.exterior.coords)] + [_coords(i.coords) for i in p.interiors]


def _persisted_predictions(name: str) -> dict:
    """Predicted labels already written by `label_corpus.py`, keyed by room GID.

    Room footprints only exist on freshly-parsed Space objects -- graph.save()
    deliberately drops that geometry -- so the plan export must still re-parse
    the IFC. But re-parsing gives fresh nodes with no `predicted_label`
    attribute, since that was written onto the persisted graph by a separate
    stage. Without this merge the annotator would show recovered rooms as
    "room_1" instead of the model's actual guess, making the correction
    workflow meaningless for judging the semantic-injection stage.
    """
    p = os.path.join("results", f"{name}.graph.json")
    if not os.path.exists(p):
        return {}
    with open(p) as fh:
        doc = json.load(fh)
    return {n["id"]: (n.get("predicted_label"), n.get("label_source"))
           for n in doc.get("nodes", []) if n.get("predicted_label")}


def export(path: str, out_dir: str, quiet: bool = False) -> dict:
    t0 = time.time()
    model = P.parse(path)
    g = G.build(model)
    spaces = g.graph.get("spaces_obj", [])
    by_gid = {s.gid: s for s in spaces}

    name = os.path.splitext(os.path.basename(path))[0]
    predictions = _persisted_predictions(name)
    doc = {"model": name, "source": path,
           "stats": g.graph.get("stats", {}), "storeys": []}

    # Lateral topology, grouped by storey for the viewer.
    edges = []
    for u, v, d in g.edges(data=True):
        rel = d.get("relation")
        if rel in ("connected_by_door", "open_passage", "vertically_connected"):
            if u < v:            # multigraph stores both directions
                edges.append({"a": u, "b": v, "type": rel,
                              "door": d.get("door"), "width": d.get("width"),
                              "kind": d.get("kind")})

    for st in sorted(model.storeys, key=lambda s: s.elevation):
        z = st.elevation + 1.2
        # Sorted by GUID, because model.products arrives in whatever order the
        # threaded IfcOpenShell iterator finished in -- which differs run to
        # run. Without this the same file exports different bytes each time,
        # and the MAX_WALL_POLYS cut below keeps an arbitrary subset of walls.
        prods = sorted((p for p in model.products if p.storey_gid == st.gid),
                       key=lambda p: p.gid)

        walls = []
        for p in prods:
            if p.footprint is None or not p.is_occluding:
                continue
            if not (p.zmin <= z <= p.zmax):
                continue
            r = ring(p.footprint)
            if r:
                walls.append(r)
            if len(walls) >= MAX_WALL_POLYS:
                break

        doors = []
        for p in prods:
            if p.ifc_type != "IfcDoor":
                continue
            n = topology._door_normal(p)
            doors.append({
                "id": p.gid,
                "x": round(p.centroid[0], 3), "y": round(p.centroid[1], 3),
                "nx": round(float(n[0]), 3) if n is not None else 0.0,
                "ny": round(float(n[1]), 3) if n is not None else 0.0,
            })

        rooms = []
        for s in spaces:
            if s.storey_gid != st.gid or s.footprint is None:
                continue
            rs = rings(s.footprint)
            if not rs:
                continue
            r, holes = rs[0], rs[1:]
            c = s.footprint.centroid
            base_label = (g.nodes[s.gid].get("label") if g.has_node(s.gid)
                         else (s.long_name or s.name)) or "room"
            pred_label, pred_source = predictions.get(s.gid, (None, None))
            rooms.append({
                "id": s.gid,
                # `label` stays the ground truth IFC gave (or "room_N" if
                # none); `predicted_label` is what the model guessed, kept
                # separate so the annotator judges a claim rather than being
                # shown it pre-merged as if it were fact.
                "label": base_label,
                "predicted_label": pred_label,
                "label_source": pred_source,
                "source": s.source,
                "area": round(s.footprint.area, 2),
                # `polygon` stays the exterior ring so older plan files and
                # anything reading them keep working; `holes` is additive.
                "polygon": r,
                **({"holes": holes} if holes else {}),
                "centroid": [round(c.x, 3), round(c.y, 3)],
            })

        ids = {r["id"] for r in rooms}
        st_edges = sorted((e for e in edges
                           if e["a"] in ids and e["b"] in ids
                           and e["type"] != "vertically_connected"),
                          key=lambda e: (e["a"], e["b"]))

        doc["storeys"].append({
            "gid": st.gid,
            "name": st.name or "(unnamed)",
            "elevation": round(st.elevation, 3),
            "walls": walls,
            "doors": doors,
            "rooms": rooms,
            "edges": st_edges,
        })

    # Cross-storey links live at document level.
    room_storey = {s.gid: s.storey_gid for s in spaces}
    # Sorted for the same reason as prods: topology builds these from sets, so
    # the order varies between runs of the same file.
    doc["vertical"] = sorted(
        ({**e, "storey_a": room_storey.get(e["a"]),
          "storey_b": room_storey.get(e["b"])}
         for e in edges if e["type"] == "vertically_connected"),
        key=lambda e: (e["a"], e["b"]))

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{name}.plan.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))

    if not quiet:
        n_rooms = sum(len(s["rooms"]) for s in doc["storeys"])
        print(f"  {name}: {len(doc['storeys'])} storeys, {n_rooms} rooms, "
              f"{sum(len(s['edges']) for s in doc['storeys'])} edges, "
              f"{os.path.getsize(out)/1e6:.1f} MB, {time.time()-t0:.1f}s")
    return doc


def _one(job):
    """One model, in its own process. Returns (name, ok, message)."""
    path, out_dir = job
    name = os.path.basename(path)
    t0 = time.time()
    try:
        doc = export(path, out_dir, quiet=True)
    except Exception as e:
        return (name, False, f"{type(e).__name__}: {e}")
    except BaseException as e:                    # MemoryError, OCC aborts
        return (name, False, f"{type(e).__name__}: {e}")
    n_rooms = sum(len(s["rooms"]) for s in doc["storeys"])
    return (name, True, f"{len(doc['storeys'])} storeys, {n_rooms} rooms, "
                        f"{time.time() - t0:.1f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="annotator/data")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="models to tessellate at once. Each worker is a separate "
                         "process holding one model's geometry -- allow ~1.5 GB each")
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave models that already have a plan alone, so an "
                         "interrupted run can be resumed")
    args = ap.parse_args()

    paths = []
    for p in args.inputs:
        paths.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])

    if args.skip_existing:
        before = len(paths)
        paths = [p for p in paths if not os.path.exists(os.path.join(
            args.out, os.path.basename(p).rsplit(".", 1)[0] + ".plan.json"))]
        if before != len(paths):
            print(f"skipping {before - len(paths)} model(s) that already have a plan")

    print(f"exporting {len(paths)} model(s) -> {args.out}"
          f"{f' with {args.jobs} workers' if args.jobs > 1 else ''}")
    os.makedirs(args.out, exist_ok=True)

    failed = []
    done = 0
    if args.jobs > 1:
        import concurrent.futures as cf
        # Processes, not threads: the tessellation is in OpenCASCADE and each
        # model wants its own address space. A worker that dies (a corrupt file
        # can abort the C++ side outright) must not take the run with it, so
        # results are consumed as they land and a broken pool is reported.
        jobs = [(p, args.out) for p in paths]
        with cf.ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(_one, j): j[0] for j in jobs}
            for fut in cf.as_completed(futs):
                path = futs[fut]
                name = os.path.basename(path)
                try:
                    name, good, msg = fut.result()
                except Exception as e:
                    good, msg = False, f"worker died: {type(e).__name__}: {e}"
                done += 1
                if good:
                    print(f"  [{done}/{len(jobs)}] {name}: {msg}")
                else:
                    failed.append((name, msg))
                    print(f"  [{done}/{len(jobs)}] !! {name}: {msg}")
    else:
        for p in paths:
            name = os.path.basename(p)
            try:
                export(p, args.out)
            except Exception as e:
                failed.append((name, f"{type(e).__name__}: {e}"))
                print(f"  !! {name}: {type(e).__name__}: {e}")

    ok = len(paths) - len(failed)
    print(f"\n{ok} plan(s) written, {len(failed)} failed")
    if failed:
        # Named, not just counted: a model that never exported is a model no one
        # can annotate, and it is silent otherwise.
        for name, msg in sorted(failed):
            print(f"  {name}: {msg}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
