"""Stage 2+4 -- recovering the relations IFC omits.

The test set contains zero IfcRelSpaceBoundary instances, so every wall-room,
door-room and room-room relation has to be derived from geometry. This module
does that, plus the storey repair needed when the spatial tree itself is broken
(model_2 has no IfcRelContainedInSpatialStructure at all and its spaces are
orphaned from every storey).
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

log = logging.getLogger(__name__)

ADJACENCY_GAP = 0.60       # m: max wall thickness for two rooms to count as adjacent
DOOR_PROBE = 0.85          # m: how far past the door leaf to look for a room
SHARED_EDGE_MIN = 0.40     # m: ignore rooms touching only at a corner
STAIR_REACH = 0.90         # m: how close a room must be for a stair to land in it
MIN_PASSAGE_WIDTH = 0.60   # m: a contiguous gap at least this wide is traversable


# --------------------------------------------------------------------------
# Storey repair
# --------------------------------------------------------------------------

def assign_storeys(model) -> dict:
    """Fill in missing storey assignments from elevation.

    Returns counts of what had to be repaired, so the pipeline can report how
    much of the hierarchy was actually stated by the file.
    """
    if not model.storeys:
        return {"spaces_repaired": 0, "products_repaired": 0}

    elevs = np.array([s.elevation for s in model.storeys])
    gids = [s.gid for s in model.storeys]

    def storey_for(z):
        # Highest storey whose floor is at or below z (with a small tolerance
        # for elements modelled slightly below their own floor level).
        idx = np.searchsorted(elevs, z + 0.35, side="right") - 1
        return gids[max(0, idx)]

    sp_fixed = 0
    for sp in model.spaces:
        if sp.storey_gid is None:
            sp.storey_gid = storey_for(sp.zmin)
            sp_fixed += 1

    pr_fixed = 0
    for p in model.products:
        if p.storey_gid is None:
            # Use the base of the element: a wall belongs to the floor it rises
            # from, not the one its top happens to reach.
            p.storey_gid = storey_for(p.zmin)
            pr_fixed += 1

    # Rebuild each storey's space list from the (now complete) assignments.
    for st in model.storeys:
        st.space_gids = [s.gid for s in model.spaces if s.storey_gid == st.gid]

    log.info("storey repair: %d spaces, %d products assigned by elevation",
             sp_fixed, pr_fixed)
    return {"spaces_repaired": sp_fixed, "products_repaired": pr_fixed}


def storey_ceiling(model, storey) -> float:
    """Elevation of the storey above, or a default height."""
    higher = [s.elevation for s in model.storeys if s.elevation > storey.elevation + 0.1]
    return min(higher) if higher else storey.elevation + 3.0


# --------------------------------------------------------------------------
# Containment: which room is each element in
# --------------------------------------------------------------------------

def assign_products_to_spaces(model, spaces) -> dict:
    """Geometric containment, per storey. Returns product gid -> space gid."""
    by_storey = defaultdict(list)
    for sp in spaces:
        if sp.footprint is not None:
            by_storey[sp.storey_gid].append(sp)

    result = {}
    stated = 0
    for st_gid, sps in by_storey.items():
        polys = [s.footprint for s in sps]
        tree = STRtree(polys)
        cands = [p for p in model.products if p.storey_gid == st_gid]
        for p in cands:
            if p.space_gid:            # the file told us; trust it
                result[p.gid] = p.space_gid
                stated += 1
                continue
            if p.footprint is None:
                continue
            pt = Point(p.centroid[0], p.centroid[1])
            hits = tree.query(pt)
            best, best_a = None, -1.0
            for i in np.atleast_1d(hits):
                sp = sps[int(i)]
                if sp.footprint.contains(pt):
                    # Prefer the smallest containing room (handles a room
                    # polygon nested inside a corridor component).
                    if best is None or sp.footprint.area < best_a:
                        best, best_a = sp, sp.footprint.area
            if best is None:
                # Fall back to footprint overlap for elements whose centroid
                # falls inside a wall (doors, built-ins).
                hits = tree.query(p.footprint)
                ov_best, ov_a = None, 0.0
                for i in np.atleast_1d(hits):
                    sp = sps[int(i)]
                    try:
                        a = sp.footprint.intersection(p.footprint).area
                    except Exception:
                        continue
                    if a > ov_a:
                        ov_best, ov_a = sp, a
                best = ov_best
            if best is not None:
                result[p.gid] = best.gid

    log.info("containment: %d products placed in a room (%d stated by IFC)",
             len(result), stated)
    return result


# --------------------------------------------------------------------------
# Room-to-room adjacency
# --------------------------------------------------------------------------

def space_adjacency(spaces) -> list:
    """Pairs of rooms separated by no more than one wall thickness.

    Uses a shared-boundary length test so that rooms meeting only at a corner
    are not reported as adjacent.
    """
    by_storey = defaultdict(list)
    for sp in spaces:
        if sp.footprint is not None:
            by_storey[sp.storey_gid].append(sp)

    edges = []
    for sps in by_storey.values():
        if len(sps) < 2:
            continue
        grown = [s.footprint.buffer(ADJACENCY_GAP / 2.0) for s in sps]
        tree = STRtree(grown)
        for i, a in enumerate(grown):
            for j in np.atleast_1d(tree.query(a)):
                j = int(j)
                if j <= i:
                    continue
                inter = a.intersection(grown[j])
                if inter.is_empty:
                    continue
                # A genuine shared wall produces a sliver with real extent.
                minx, miny, maxx, maxy = inter.bounds
                if max(maxx - minx, maxy - miny) < SHARED_EDGE_MIN:
                    continue
                edges.append((sps[i].gid, sps[j].gid,
                              round(float(sps[i].footprint.distance(sps[j].footprint)), 3)))
    log.info("adjacency: %d room-room pairs", len(edges))
    return edges


# --------------------------------------------------------------------------
# Door-mediated connectivity  (the replacement for IfcRelSpaceBoundary)
# --------------------------------------------------------------------------

def door_connections(model, spaces) -> list:
    """Resolve, for each door, the pair of rooms it joins.

    Probes outward along the door's placement normal. Falls back to the minor
    principal axis of the door footprint when the placement is unusable.
    """
    by_storey = defaultdict(list)
    for sp in spaces:
        if sp.footprint is not None:
            by_storey[sp.storey_gid].append(sp)
    trees = {k: STRtree([s.footprint for s in v]) for k, v in by_storey.items()}

    def room_at(st_gid, x, y):
        sps = by_storey.get(st_gid)
        if not sps:
            return None
        pt = Point(x, y)
        best, best_a = None, -1.0
        for i in np.atleast_1d(trees[st_gid].query(pt)):
            sp = sps[int(i)]
            if sp.footprint.contains(pt):
                if best is None or sp.footprint.area < best_a:
                    best, best_a = sp, sp.footprint.area
        return best

    out = []
    doors = [p for p in model.products if p.ifc_type == "IfcDoor"]
    for d in doors:
        n = _door_normal(d)
        if n is None:
            continue
        cx, cy = d.centroid[0], d.centroid[1]
        a = b = None
        # Step outward until a room is found on each side.
        for dist in (DOOR_PROBE, DOOR_PROBE + 0.5, DOOR_PROBE + 1.1):
            if a is None:
                a = room_at(d.storey_gid, cx + n[0] * dist, cy + n[1] * dist)
            if b is None:
                b = room_at(d.storey_gid, cx - n[0] * dist, cy - n[1] * dist)
            if a is not None and b is not None:
                break
        if a is not None and b is not None and a.gid != b.gid:
            out.append({"door": d.gid, "a": a.gid, "b": b.gid,
                        "width": round(_door_width(d), 2), "exterior": False})
        elif (a is None) ^ (b is None):
            inner = a or b
            out.append({"door": d.gid, "a": inner.gid, "b": None,
                        "width": round(_door_width(d), 2), "exterior": True})

    interior = sum(1 for c in out if not c["exterior"])
    log.info("doors: %d resolved (%d interior, %d exterior) of %d",
             len(out), interior, len(out) - interior, len(doors))
    return out


def _door_normal(d):
    """Unit XY normal through the door opening."""
    if d.matrix is not None:
        n = np.array(d.matrix[:3, 1], dtype=float)   # IFC local Y = through-swing
        if np.linalg.norm(n[:2]) > 1e-6:
            n = n[:2] / np.linalg.norm(n[:2])
            return n
    # Fallback: minor axis of the footprint, i.e. across the thin dimension.
    if d.footprint is None:
        return None
    coords = np.array(d.footprint.exterior.coords)[:, :2]
    c = coords - coords.mean(axis=0)
    try:
        _, _, vt = np.linalg.svd(c, full_matrices=False)
        return vt[1] / np.linalg.norm(vt[1])
    except Exception:
        return None


def _door_width(d) -> float:
    if d.footprint is None:
        return 0.0
    minx, miny, maxx, maxy = d.footprint.bounds
    return max(maxx - minx, maxy - miny)


# --------------------------------------------------------------------------
# Open passages: traversable thresholds that have no door at all
# --------------------------------------------------------------------------

def open_passages(model, spaces, adjacency) -> list:
    """Adjacent rooms with no wall between them.

    Not every transition is a door. Open stairs, archways and open-plan
    thresholds carry no IfcDoor, and relying on doors alone severs them: in
    model_0 the ground-floor stairwells open straight onto the deck, which left
    the whole ground floor unreachable from the upper storeys.

    For each adjacent pair we take the strip of space between the two rooms and
    measure how much of it is actually filled by wall solids. A gap that is
    mostly empty is a passage.
    """
    # Doors count as blockers here, exactly as they do during room recovery.
    # A doorway is a gap in the wall, so without this every door is *also*
    # reported as an open passage -- the room pair is right either way, which
    # set-based edge scoring cannot detect, but the edge type is wrong and a
    # planner would treat a closable door as permanently open.
    walls_by_storey = defaultdict(list)
    for p in model.products:
        if p.ifc_type in ("IfcWall", "IfcWallStandardCase", "IfcCurtainWall",
                          "IfcDoor") and p.footprint is not None:
            walls_by_storey[p.storey_gid].append(p.footprint)

    trees = {}
    for k, v in walls_by_storey.items():
        if v:
            trees[k] = (STRtree(v), v)

    # Door solids, kept separately: they block the gap test (a doorway is not an
    # open passage) but they are also the evidence that a barrier between two
    # recovered rooms is in fact a doorway whose orientation could not be read.
    doors_by_storey = defaultdict(list)
    for p in model.products:
        if p.ifc_type == "IfcDoor" and p.footprint is not None:
            doors_by_storey[p.storey_gid].append(p)

    by_gid = {s.gid: s for s in spaces}
    out = []
    skipped_recovered = 0
    # A door joins exactly two rooms. Its footprint can straddle several
    # candidate strips, so each door may be claimed only once -- otherwise the
    # same door is emitted for several pairs and the graph asserts a door
    # opening into three rooms.
    claimed_doors = set()
    for a_gid, b_gid, _gap in adjacency:
        a, b = by_gid.get(a_gid), by_gid.get(b_gid)
        if a is None or b is None or a.footprint is None or b.footprint is None:
            continue

        # The gap test assumes room polygons stop at the wall face, which holds
        # for stated IfcSpace but not for recovered rooms: those *are* the free
        # space, traced on a 10 cm raster, so the eroded boundary between two of
        # them reads as an opening even through solid masonry. But recovery
        # already answered this question -- two rooms in different free-space
        # components are separated by a barrier, or the flood fill would have
        # merged them. Trust that instead of re-deriving it badly.
        recovered_pair = (a.source == "inferred" and b.source == "inferred"
                          and a.component and b.component
                          and a.component != b.component)
        try:
            strip = a.footprint.buffer(ADJACENCY_GAP / 2.0).intersection(
                b.footprint.buffer(ADJACENCY_GAP / 2.0))
            # Restrict to the gap that actually separates the two rooms.
            # The buffered band reaches into both interiors, where no wall can
            # ever be, so a solid partition would appear only ~50% covered and
            # no threshold could distinguish it from a real opening.
            strip = strip.difference(a.footprint).difference(b.footprint)
        except Exception:
            continue
        if strip.is_empty or strip.area < 0.05:
            continue

        entry = trees.get(a.storey_gid) or trees.get(b.storey_gid)
        if entry is None:
            continue
        tree, polys = entry
        # Union before subtracting: summing areas would double-count wherever
        # walls overlap (coincident partitions, or the two faces of a
        # double-sided wall) and mask genuine openings.
        hits = [polys[int(i)] for i in np.atleast_1d(tree.query(strip))]
        try:
            free = strip.difference(unary_union(hits)) if hits else strip
        except Exception:
            continue
        if free.is_empty:
            continue

        # A passage is a *contiguous* gap wide enough to walk through. Testing
        # aggregate wall coverage instead would miss a 1.6 m doorway in a 6 m
        # partition, because the wall still covers most of the boundary.
        pieces = [free] if free.geom_type == "Polygon" else [
            p for p in getattr(free, "geoms", []) if p.geom_type == "Polygon"]
        if not pieces:
            continue
        best = max(pieces, key=lambda p: p.area)
        minx, miny, maxx, maxy = best.bounds
        width = max(maxx - minx, maxy - miny)

        if recovered_pair:
            # Both rooms were recovered and segmentation put them in different
            # free-space components, so a barrier stands between them and the
            # eroded raster boundary must not be read as an opening. A door
            # sitting in that barrier is the exception: it is a genuine
            # connection whose placement normal was unusable, so recover it as
            # a door rather than discarding it or mislabelling it a passage.
            door, best_ov = None, 0.0
            for d in doors_by_storey.get(a.storey_gid, []):
                if d.gid in claimed_doors:
                    continue
                try:
                    ov = d.footprint.intersection(strip).area
                except Exception:
                    continue
                if ov > best_ov:
                    door, best_ov = d, ov
            if door is None or best_ov <= 0:
                skipped_recovered += 1
                continue
            claimed_doors.add(door.gid)
            out.append({"a": a_gid, "b": b_gid, "kind": "door",
                        "door": door.gid,
                        "width": round(float(_door_width(door)), 2),
                        "openness": 0.0})
            continue

        if width >= MIN_PASSAGE_WIDTH:
            out.append({"a": a_gid, "b": b_gid, "kind": "passage",
                        "width": round(float(width), 2),
                        "openness": round(best.area / strip.area, 2)})

    log.info("open passages: %d wall-free thresholds (%d recovered-room pairs "
             "resolved by segmentation)", len(out), skipped_recovered)
    return out


# --------------------------------------------------------------------------
# Vertical connectivity between storeys
# --------------------------------------------------------------------------


def stair_metrics(stair) -> dict:
    """Geometry a robot profile needs to judge a stair.

    Reports only what the geometry actually supports: total rise, and clear
    width across the flight.

    Slope is deliberately NOT reported. Stairs in this corpus are compact
    switchback or spiral cores -- 4.89 m of rise inside a 2.5 x 2.5 m footprint
    -- so the bounding box measures the shaft, not the travel. Dividing rise by
    box extent yields ~1.95 (63 degrees), which no stair is, and using it as a
    gradient would make every stair impassable for every platform. Tread
    dimensions are not recoverable from a tessellated solid, so the honest
    answer is that this file cannot tell us the gradient.

    Headroom is absent for the same reason: it depends on the underside of the
    structure above, and a fabricated clearance is worse than a missing one.
    """
    if stair is None:
        return {"rise": None, "footprint_extent": None, "clear_width": None}
    rise = float(stair.zmax - stair.zmin)
    if stair.footprint is None:
        return {"rise": round(rise, 2), "footprint_extent": None,
                "clear_width": None}
    minx, miny, maxx, maxy = stair.footprint.bounds
    dx, dy = maxx - minx, maxy - miny
    return {"rise": round(rise, 2),
            # The shaft footprint, not the travel distance -- named so that it
            # cannot be mistaken for a run and turned into a slope.
            "footprint_extent": round(float(max(dx, dy)), 2),
            "clear_width": round(float(min(dx, dy)), 2)}


def vertical_connections(model, spaces) -> list:
    """Link rooms across storeys through stairs.

    A stair solid spans two storeys; the rooms it overlaps on the lower and
    upper level are joined. Rooms whose label names a stairwell are also linked
    when they overlap vertically, which covers models where the stair geometry
    is a single flight assigned to one storey only.

    Each connection records the evidence it rests on in `basis`: "stair_solid"
    for geometry, "stacked_label" for the name-driven fallback. They are not
    interchangeable. A label-driven link is derived from the very field a
    label-prediction experiment holds out, and without this marker it carries
    that field back to the classifier as a graph feature.
    """
    stairs = [p for p in model.products
              if p.ifc_type in ("IfcStair", "IfcStairFlight") and p.footprint is not None]
    by_storey = defaultdict(list)
    for sp in spaces:
        if sp.footprint is not None:
            by_storey[sp.storey_gid].append(sp)

    order = sorted(model.storeys, key=lambda s: s.elevation)
    out = []
    seen = set()

    for s in stairs:
        touched = []
        for st in order:
            # Does the stair pass through this storey band?
            top = storey_ceiling(model, st)
            if s.zmax < st.elevation - 0.3 or s.zmin > top + 0.3:
                continue
            # A stair is itself an obstacle, so it is carved *out* of the free
            # space and can never overlap a room polygon. Match by proximity:
            # the room a stair lands in is the one it touches.
            best, best_d = None, 1e9
            for sp in by_storey.get(st.gid, []):
                try:
                    d = sp.footprint.distance(s.footprint)
                except Exception:
                    continue
                if d < best_d:
                    best, best_d = sp, d
            if best is not None and best_d <= STAIR_REACH:
                touched.append(best)
        for i in range(len(touched) - 1):
            k = tuple(sorted((touched[i].gid, touched[i + 1].gid)))
            if k not in seen:
                seen.add(k)
                out.append({"a": k[0], "b": k[1], "via": s.gid, "kind": "stair",
                            # Carved by an actual stair solid, so this link
                            # survives having the room labels taken away.
                            "basis": "stair_solid",
                            **stair_metrics(s)})

    # Stairwell rooms stacked vertically.
    def is_stairwell(sp):
        t = f"{sp.long_name} {sp.name}".lower()
        return "stair" in t or "mumti" in t

    for i, st in enumerate(order[:-1]):
        upper = order[i + 1]
        lo = [s for s in by_storey.get(st.gid, []) if is_stairwell(s)]
        hi = [s for s in by_storey.get(upper.gid, []) if is_stairwell(s)]
        for a in lo:
            for b in hi:
                try:
                    if a.footprint.intersection(b.footprint).area > 1.0:
                        k = tuple(sorted((a.gid, b.gid)))
                        if k not in seen:
                            seen.add(k)
                            # Stacked stairwell rooms with no stair solid to
                            # measure: the link is real but its geometry is not
                            # stated, so the metrics stay None.
                            out.append({"a": k[0], "b": k[1], "via": None,
                                        "kind": "stairwell",
                                        # This link exists *because* both rooms
                                        # are named "stair". Anything that
                                        # masks a room's label to predict it
                                        # must drop this edge too, or the label
                                        # reaches the classifier through the
                                        # topology it induced.
                                        "basis": "stacked_label",
                                        **stair_metrics(None)})
                except Exception:
                    continue

    log.info("vertical: %d inter-storey connections", len(out))
    return out
