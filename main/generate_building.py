"""Procedural IFC building generator with connectivity ground truth.

The evaluation problem for connectivity recovery is that no real IFC states the
answer. Here the answer is the *input*: we choose the room layout and decide
which neighbouring rooms are joined, then emit an IFC that expresses the
geometry. Edge precision/recall is therefore exact and free.

One property matters more than architectural realism: **some adjacent rooms must
not be connected**. If every neighbouring pair has a door, naive adjacency
scores a perfect result and the benchmark cannot distinguish any two methods.
`door_ratio` controls that directly.

The emitted IFC is deliberately clean -- proper IfcSpace, containment and units
-- so that damage can be applied afterwards as a controlled variable
(`damage_ifc.py`) rather than being baked in.

    python main/generate_building.py --out dataset/synthetic_data/synthetic --n 20
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass, field, asdict

import numpy as np

import ifcopenshell
import ifcopenshell.api
from ifcopenshell.api import run

WALL_T = 0.20          # wall thickness, m
STOREY_H = 3.20        # floor-to-floor, m
DOOR_W = 0.90
DOOR_H = 2.10
OPENING_W = 1.60       # door-less passage
MIN_ROOM = 2.6         # m, minimum room edge


# --------------------------------------------------------------------------
# Layout: recursive subdivision of a rectangular floor plate
# --------------------------------------------------------------------------

@dataclass
class Room:
    rid: str
    x0: float
    y0: float
    x1: float
    y1: float
    storey: int
    name: str = "Room"

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def d(self):
        return self.y1 - self.y0

    @property
    def area(self):
        return self.w * self.d

    @property
    def centre(self):
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


def subdivide(x0, y0, x1, y1, depth, rng, out, storey):
    """Split a rectangle until cells are small enough, then emit them."""
    w, h = x1 - x0, y1 - y0
    too_small = w < 2 * MIN_ROOM and h < 2 * MIN_ROOM
    if depth <= 0 or too_small or (rng.random() < 0.12 and depth < 3):
        out.append(Room(f"S{storey}R{len(out):03d}", x0, y0, x1, y1, storey))
        return
    # Split the longer axis, at a jittered midpoint so rooms differ in size.
    if w >= h:
        f = rng.uniform(0.38, 0.62)
        xs = x0 + w * f
        subdivide(x0, y0, xs, y1, depth - 1, rng, out, storey)
        subdivide(xs, y0, x1, y1, depth - 1, rng, out, storey)
    else:
        f = rng.uniform(0.38, 0.62)
        ys = y0 + h * f
        subdivide(x0, y0, x1, ys, depth - 1, rng, out, storey)
        subdivide(x0, ys, x1, y1, depth - 1, rng, out, storey)


def shared_wall(a: Room, b: Room, tol=1e-6):
    """Return (axis, position, lo, hi) of the wall shared by two rooms, or None.

    Rooms produced by subdivision touch exactly, so a shared wall is a genuine
    coincident edge with non-zero overlap -- corner contact is excluded.
    """
    # Vertical wall: a.x1 == b.x0 (or vice versa)
    for p, q in ((a, b), (b, a)):
        if abs(p.x1 - q.x0) < tol:
            lo, hi = max(p.y0, q.y0), min(p.y1, q.y1)
            if hi - lo > MIN_ROOM * 0.4:
                return ("x", p.x1, lo, hi)
        if abs(p.y1 - q.y0) < tol:
            lo, hi = max(p.x0, q.x0), min(p.x1, q.x1)
            if hi - lo > MIN_ROOM * 0.4:
                return ("y", p.y1, lo, hi)
    return None


ROOM_NAMES = ["Office", "Meeting Room", "Store", "Lab", "Bedroom", "Kitchen",
              "Living Room", "Bathroom", "Corridor", "Reception", "Utility"]


@dataclass
class Building:
    name: str
    rooms: list = field(default_factory=list)
    doors: list = field(default_factory=list)     # dicts: a, b, x, y, axis, kind
    stairs: list = field(default_factory=list)    # dicts: rooms=[rid...], x, y
    storeys: int = 1
    seed: int = 0

    def gt_edges(self):
        """Ground-truth connectivity: the edges we chose to create."""
        e = []
        for d in self.doors:
            e.append({"a": d["a"], "b": d["b"], "type": d["kind"]})
        for s in self.stairs:
            rs = s["rooms"]
            for i in range(len(rs) - 1):
                e.append({"a": rs[i], "b": rs[i + 1], "type": "vertical"})
        return e


def make_building(seed: int, storeys: int = 2, plate=(18.0, 12.0),
                  depth: int = 3, door_ratio: float = 0.6,
                  open_ratio: float = 0.15) -> Building:
    """Generate a layout and decide which neighbours are actually joined."""
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    b = Building(name=f"synth_{seed:04d}", storeys=storeys, seed=seed)
    W, D = plate

    for s in range(storeys):
        rooms = []
        subdivide(0.0, 0.0, W, D, depth, rng, rooms, s)
        for r in rooms:
            r.name = ROOM_NAMES[rng.randrange(len(ROOM_NAMES))]
        b.rooms.extend(rooms)

        # Candidate adjacencies, then keep only a fraction as real openings.
        # The discarded ones are the negatives that make the benchmark
        # discriminative: adjacent, but separated by solid wall.
        cands = []
        for i in range(len(rooms)):
            for j in range(i + 1, len(rooms)):
                sw = shared_wall(rooms[i], rooms[j])
                if sw:
                    cands.append((rooms[i], rooms[j], sw))
        rng.shuffle(cands)
        n_open = int(len(cands) * door_ratio)
        for k, (ra, rb, (axis, pos, lo, hi)) in enumerate(cands):
            if k >= n_open:
                break                      # solid wall: a true negative
            mid = (lo + hi) / 2
            is_open = rng.random() < open_ratio
            x, y = (pos, mid) if axis == "x" else (mid, pos)
            b.doors.append({
                "a": ra.rid, "b": rb.rid, "x": x, "y": y, "axis": axis,
                "storey": s,
                "kind": "open_passage" if is_open else "door",
                "width": OPENING_W if is_open else DOOR_W,
            })

    # A stair core: pick a room present on every storey by position.
    if storeys > 1:
        per = [[r for r in b.rooms if r.storey == s] for s in range(storeys)]
        anchor = min(per[0], key=lambda r: (r.centre[0] ** 2 + r.centre[1] ** 2))
        cx, cy = anchor.centre
        chain = []
        for s in range(storeys):
            r = min(per[s], key=lambda r: (r.centre[0] - cx) ** 2
                    + (r.centre[1] - cy) ** 2)
            r.name = "Stair"
            chain.append(r.rid)
        b.stairs.append({"rooms": chain, "x": cx, "y": cy})

    return b


# --------------------------------------------------------------------------
# IFC emission
# --------------------------------------------------------------------------

def _ctx(f):
    return run("context.add_context", f, context_type="Model")


def _body_ctx(f, parent):
    return run("context.add_context", f, context_type="Model",
               context_identifier="Body", target_view="MODEL_VIEW",
               parent=parent)


def _box_rot(f, body, product, cx, cy, z, dx, dy, dz, angle):
    """Box centred on (cx,cy), rotated about Z, placed via its own transform.

    Doors must carry a real orientation: a recovery pipeline reads the door's
    through-direction from its placement (local +Y), so emitting every door with
    an identity rotation would make half of them unprobeable and would measure
    the generator rather than the method.
    """
    hx, hy = dx / 2, dy / 2
    pts = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    prof = f.create_entity(
        "IfcArbitraryClosedProfileDef", ProfileType="AREA",
        OuterCurve=f.create_entity(
            "IfcPolyline",
            Points=[f.create_entity("IfcCartesianPoint", Coordinates=p)
                    for p in pts + [pts[0]]]))
    solid = f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=prof,
        Position=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint",
                                     Coordinates=(0.0, 0.0, 0.0))),
        ExtrudedDirection=f.create_entity("IfcDirection",
                                          DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=dz)
    rep = f.create_entity("IfcShapeRepresentation", ContextOfItems=body,
                          RepresentationIdentifier="Body",
                          RepresentationType="SweptSolid", Items=[solid])
    run("geometry.assign_representation", f, product=product, representation=rep)
    c, s = float(np.cos(angle)), float(np.sin(angle))
    run("geometry.edit_object_placement", f, product=product,
        matrix=np.array([[c, -s, 0.0, cx],
                         [s,  c, 0.0, cy],
                         [0.0, 0.0, 1.0, z],
                         [0.0, 0.0, 0.0, 1.0]]))


def _box(f, body, product, x, y, z, dx, dy, dz):
    """Attach an axis-aligned extruded box as the product's Body shape."""
    pts = [(0.0, 0.0), (dx, 0.0), (dx, dy), (0.0, dy)]
    prof = f.create_entity(
        "IfcArbitraryClosedProfileDef", ProfileType="AREA",
        OuterCurve=f.create_entity(
            "IfcPolyline",
            Points=[f.create_entity("IfcCartesianPoint", Coordinates=p)
                    for p in pts + [pts[0]]]))
    solid = f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=prof,
        Position=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint",
                                     Coordinates=(0.0, 0.0, 0.0))),
        ExtrudedDirection=f.create_entity("IfcDirection",
                                          DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=dz)
    rep = f.create_entity("IfcShapeRepresentation", ContextOfItems=body,
                          RepresentationIdentifier="Body",
                          RepresentationType="SweptSolid", Items=[solid])
    run("geometry.assign_representation", f, product=product, representation=rep)
    run("geometry.edit_object_placement", f, product=product,
        matrix=np.array([[1.0, 0, 0, x], [0, 1.0, 0, y], [0, 0, 1.0, z],
                         [0, 0, 0, 1.0]]))


def emit_ifc(b: Building, path: str) -> None:
    f = ifcopenshell.file(schema="IFC4")
    run("root.create_entity", f, ifc_class="IfcProject", name=b.name)
    run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    model = _ctx(f)
    body = _body_ctx(f, model)

    site = run("root.create_entity", f, ifc_class="IfcSite", name="Site")
    bldg = run("root.create_entity", f, ifc_class="IfcBuilding", name=b.name)
    run("aggregate.assign_object", f, products=[site],
        relating_object=f.by_type("IfcProject")[0])
    run("aggregate.assign_object", f, products=[bldg], relating_object=site)

    storey_ents = []
    for s in range(b.storeys):
        st = run("root.create_entity", f, ifc_class="IfcBuildingStorey",
                 name=f"Level {s}")
        st.Elevation = float(s * STOREY_H)
        run("aggregate.assign_object", f, products=[st], relating_object=bldg)
        storey_ents.append(st)

    rid_to_space = {}
    for r in b.rooms:
        z = r.storey * STOREY_H
        sp = run("root.create_entity", f, ifc_class="IfcSpace", name=r.rid)
        sp.LongName = r.name
        run("aggregate.assign_object", f, products=[sp],
            relating_object=storey_ents[r.storey])
        # Inset to the inner wall faces. Real IfcSpace volumes stop at the
        # wall surface, so the gap between two room polygons equals the wall
        # thickness. Emitting rooms that share an exact edge would leave the
        # wall straddling the boundary and half-filling the space between them,
        # which makes every solid wall look like an opening.
        _box(f, body, sp, r.x0 + WALL_T / 2, r.y0 + WALL_T / 2, z,
             r.w - WALL_T, r.d - WALL_T, STOREY_H - 0.2)
        rid_to_space[r.rid] = sp

        # Floor slab, so a slab-bounded room recovery has something to stand on.
        slab = run("root.create_entity", f, ifc_class="IfcSlab",
                   name=f"Slab-{r.rid}")
        run("spatial.assign_container", f, products=[slab],
            relating_structure=storey_ents[r.storey])
        _box(f, body, slab, r.x0, r.y0, z - 0.2, r.w, r.d, 0.2)

    # Walls: one per room edge, split around openings so that a door or
    # passage is a genuine gap in the geometry rather than a tagged annotation.
    # Deduplicate coincident wall segments. Two rooms sharing an edge would
    # otherwise each emit a wall in the same place; real IFC states one wall
    # between two rooms, and duplicates distort any area-based occupancy test.
    seen_walls = set()
    for r in b.rooms:
        z = r.storey * STOREY_H
        gaps = [d for d in b.doors
                if d["storey"] == r.storey and (d["a"] == r.rid or d["b"] == r.rid)]
        for side in ("x0", "x1", "y0", "y1"):
            for (sx, sy, dx, dy) in _wall_segments(r, side, gaps):
                key = (r.storey, round(sx, 3), round(sy, 3),
                       round(dx, 3), round(dy, 3))
                if key in seen_walls:
                    continue
                seen_walls.add(key)
                w = run("root.create_entity", f, ifc_class="IfcWall",
                        name=f"Wall-{r.rid}-{side}")
                run("spatial.assign_container", f, products=[w],
                    relating_structure=storey_ents[r.storey])
                _box(f, body, w, sx, sy, z, dx, dy, STOREY_H - 0.2)

    # Doors sit in their gaps; passages are left as bare openings.
    for d in b.doors:
        if d["kind"] != "door":
            continue
        z = d["storey"] * STOREY_H
        dr = run("root.create_entity", f, ifc_class="IfcDoor",
                 name=f"Door-{d['a']}-{d['b']}")
        run("spatial.assign_container", f, products=[dr],
            relating_structure=storey_ents[d["storey"]])
        # Local frame: +X along the door width, +Y through the opening. A door
        # in an "x" wall (wall running along y) is therefore rotated 90 degrees,
        # which puts its local +Y along world X -- the direction a recovery
        # method must probe to find the rooms on either side.
        angle = np.pi / 2 if d["axis"] == "x" else 0.0
        _box_rot(f, body, dr, d["x"], d["y"], z, DOOR_W, WALL_T, DOOR_H, angle)

    # Stairs, spanning consecutive storeys.
    for s in b.stairs:
        for i in range(len(s["rooms"]) - 1):
            z = i * STOREY_H
            stair = run("root.create_entity", f, ifc_class="IfcStair",
                        name=f"Stair-{i}")
            run("spatial.assign_container", f, products=[stair],
                relating_structure=storey_ents[i])
            _box(f, body, stair, s["x"] - 1.0, s["y"] - 0.6, z, 2.0, 1.2,
                 STOREY_H)

    f.write(path)


def _wall_segments(r: Room, side: str, gaps: list):
    """Wall pieces along one room edge, with openings removed."""
    if side in ("x0", "x1"):
        pos = r.x0 if side == "x0" else r.x1
        lo, hi = r.y0, r.y1
        cuts = [(g["y"] - g["width"] / 2, g["y"] + g["width"] / 2)
                for g in gaps if g["axis"] == "x" and abs(g["x"] - pos) < 1e-6]
        segs = _cut(lo, hi, cuts)
        return [(pos - WALL_T / 2, a, WALL_T, bb - a) for a, bb in segs]
    pos = r.y0 if side == "y0" else r.y1
    lo, hi = r.x0, r.x1
    cuts = [(g["x"] - g["width"] / 2, g["x"] + g["width"] / 2)
            for g in gaps if g["axis"] == "y" and abs(g["y"] - pos) < 1e-6]
    segs = _cut(lo, hi, cuts)
    return [(a, pos - WALL_T / 2, bb - a, WALL_T) for a, bb in segs]


def _cut(lo, hi, cuts):
    """Interval [lo,hi] minus the cut intervals."""
    out, cur = [], lo
    for a, b in sorted(cuts):
        a, b = max(a, lo), min(b, hi)
        if b <= cur:
            continue
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return [(a, b) for a, b in out if b - a > 0.05]


def write_gt(b: Building, path: str) -> None:
    gt = {
        "building": b.name, "seed": b.seed, "storeys": b.storeys,
        "rooms": [asdict(r) for r in b.rooms],
        "edges": b.gt_edges(),
        "n_rooms": len(b.rooms),
        "n_edges": len(b.gt_edges()),
    }
    # Adjacent-but-unconnected pairs: the negatives that make the metric
    # discriminative. Recorded so a baseline's false positives can be
    # attributed rather than merely counted.
    connected = {frozenset((e["a"], e["b"])) for e in gt["edges"]}
    negatives = []
    for s in range(b.storeys):
        rs = [r for r in b.rooms if r.storey == s]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if shared_wall(rs[i], rs[j]) and \
                        frozenset((rs[i].rid, rs[j].rid)) not in connected:
                    negatives.append({"a": rs[i].rid, "b": rs[j].rid})
    gt["adjacent_not_connected"] = negatives
    gt["n_negatives"] = len(negatives)
    with open(path, "w") as fh:
        json.dump(gt, fh, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dataset/synthetic_data/synthetic")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--storeys", type=int, default=2)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--door-ratio", type=float, default=0.6)
    ap.add_argument("--seed0", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    tot_r = tot_e = tot_n = 0
    for i in range(args.n):
        seed = args.seed0 + i
        b = make_building(seed, storeys=args.storeys, depth=args.depth,
                          door_ratio=args.door_ratio)
        ifc_path = os.path.join(args.out, f"{b.name}.ifc")
        gt_path = os.path.join(args.out, f"{b.name}.gt.json")
        emit_ifc(b, ifc_path)
        write_gt(b, gt_path)
        gt = json.load(open(gt_path))
        tot_r += gt["n_rooms"]; tot_e += gt["n_edges"]; tot_n += gt["n_negatives"]
        print(f"  {b.name}: {gt['n_rooms']} rooms, {gt['n_edges']} edges, "
              f"{gt['n_negatives']} adjacent-but-unconnected, "
              f"{os.path.getsize(ifc_path)/1e3:.0f} kB")
    print(f"\n{args.n} buildings: {tot_r} rooms, {tot_e} GT edges, "
          f"{tot_n} negatives -> {args.out}")


if __name__ == "__main__":
    main()
