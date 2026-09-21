"""Connectivity-recovery baselines, evaluated on identical inputs.

Every method here receives the *same* parsed model and the *same* room set, and
differs only in how it decides which rooms are connected. That isolates the
contribution being claimed: if room recovery were allowed to vary too, a
difference in the score could come from either stage.

Methods:

  schema      Read connectivity from the schema, as most BIM-to-graph work does.
              On real files this recovers nothing at all -- there are zero
              IfcRelSpaceBoundary instances in 257 MB -- which is the point.
  adjacency   Join every pair of rooms separated by at most a wall thickness.
              The bar any real method must clear: it achieves perfect recall by
              construction, so its precision is the number that matters.
  door_near   Attach each door to its two nearest rooms by centroid distance,
              without probing along the door normal. Isolates what the
              orientation-aware probe actually buys.
  ours        The full pipeline: normal-probed doors, contiguous-gap passages,
              proximity-matched vertical circulation.
  ifc2graph   External baseline (Bjornskov, published, MIT --
              github.com/JBjoernskov/Ifc2Graph): voxelises the union of
              IfcSpace meshes and reports two spaces adjacent if they share a
              voxel boundary. Genuinely independent code, not our
              reimplementation. It sees only stated IfcSpace -- it has no
              room-recovery stage at all -- so rooms our pipeline recovers
              geometrically get no edges from this method, which correctly
              represents what the published tool can actually see.

    python main/baselines.py --src dataset/synthetic_data/synthetic_L2 --out results/baselines
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import networkx as nx
from shapely.geometry import Point

from bimsg import graph as G
from bimsg import parser as P
from bimsg import topology as T


def _base_graph(model, spaces) -> nx.MultiDiGraph:
    """Nodes only: identical room set for every method."""
    g = nx.MultiDiGraph()
    for st in model.storeys:
        g.add_node(st.gid, layer="storey", label=st.name,
                   elevation=round(st.elevation, 3))
    for sp in spaces:
        g.add_node(sp.gid, layer="space", label=sp.long_name or sp.name or "room",
                   room_number=sp.name,
                   area=round(sp.footprint.area, 2) if sp.footprint else None,
                   centroid=([round(c, 3) for c in sp.footprint.centroid.coords[0]]
                             if sp.footprint else None),
                   elevation=round(sp.zmin, 3), source=sp.source)
        if sp.storey_gid and g.has_node(sp.storey_gid):
            g.add_edge(sp.storey_gid, sp.gid, key="contains", relation="contains")
    return g


def _add(g, a, b, rel, **kw):
    """Add one transition, in both directions, under its own key.

    Keying on the relation name would collapse two doors between the same pair
    of rooms into one -- the same defect the main graph had, and a baseline that
    loses parallel doors is not being compared on equal terms.
    """
    pair = f"{a}|{b}" if str(a) <= str(b) else f"{b}|{a}"
    token = kw.get("door") or kw.get("via")
    if token:
        eid = f"{rel}:{token}"
    else:
        n = 0
        while g.has_edge(a, b, key=f"{rel}:{pair}:{n}"):
            n += 1
        eid = f"{rel}:{pair}:{n}"
    for u, v in ((a, b), (b, a)):
        g.add_edge(u, v, key=eid, edge_id=eid, relation=rel,
                   provenance="inferred", **kw)


# --------------------------------------------------------------------------

def m_schema(model, spaces, ifc_path):
    """Connectivity as stated by the schema. Recovers nothing when absent.

    Only boundaries a person could walk through are read. A shared `IfcWall` is
    a boundary, not a passage, and counting it made this baseline invent a door
    in every party wall -- which flattered the comparison it exists to provide.
    """
    import ifcopenshell
    f = ifcopenshell.open(ifc_path)
    g = _base_graph(model, spaces)
    by_gid = {s.gid: s for s in spaces}

    n = 0
    for cls in ("IfcRelSpaceBoundary", "IfcRelSpaceBoundary1stLevel",
                "IfcRelSpaceBoundary2ndLevel"):
        try:
            rels = f.by_type(cls)
        except Exception:
            continue
        # A door or virtual element shared by two space boundaries joins them.
        #
        # A *wall* shared by two space boundaries does not. The boundary
        # relation says the two rooms meet along that element, and IfcWall is by
        # far the commonest thing two rooms meet along -- so grouping every
        # shared element made this baseline report a solid party wall as a door.
        # buildingSMART's own definition admits physical and virtual boundaries;
        # only the passable kinds are read here, and a virtual boundary is
        # reported as an open passage rather than a door, because there is no
        # door element to point at.
        PASSABLE = ("IfcDoor", "IfcOpeningElement", "IfcVirtualElement")
        by_element = defaultdict(set)
        kinds = {}
        for r in rels:
            try:
                el, sp = r.RelatedBuildingElement, r.RelatingSpace
                if el is None or sp is None:
                    continue
                virtual = str(getattr(r, "PhysicalOrVirtualBoundary", "")
                              or "").upper() == "VIRTUAL"
                if not (virtual or el.is_a() in PASSABLE):
                    continue
                by_element[el.GlobalId].add(sp.GlobalId)
                kinds[el.GlobalId] = ("open_passage" if virtual and
                                      el.is_a() not in ("IfcDoor",)
                                      else "connected_by_door")
            except Exception:
                continue
        for el, sps in by_element.items():
            sps = [s for s in sps if s in by_gid]
            if len(sps) == 2:
                rel = kinds.get(el, "connected_by_door")
                _add(g, sps[0], sps[1], rel,
                     **({"door": el} if rel == "connected_by_door" else {}))
                n += 1
    return g


def m_adjacency(model, spaces, ifc_path):
    """Join every adjacent pair. Perfect recall by construction."""
    g = _base_graph(model, spaces)
    for a, b, gap in T.space_adjacency(spaces):
        _add(g, a, b, "open_passage", width=None, openness=1.0)
    return g


def m_door_near(model, spaces, ifc_path):
    """Each door joins its two nearest rooms, ignoring orientation."""
    g = _base_graph(model, spaces)
    by_storey = defaultdict(list)
    for sp in spaces:
        if sp.footprint is not None:
            by_storey[sp.storey_gid].append(sp)

    for d in model.products:
        if d.ifc_type != "IfcDoor":
            continue
        cand = by_storey.get(d.storey_gid) or []
        if len(cand) < 2:
            continue
        pt = Point(d.centroid[0], d.centroid[1])
        near = sorted(cand, key=lambda s: s.footprint.distance(pt))[:2]
        if near[1].footprint.distance(pt) < 2.0:
            _add(g, near[0].gid, near[1].gid, "connected_by_door", door=d.gid,
                 width=None)

    # Same vertical handling as ours, so the comparison isolates door logic.
    for c in T.vertical_connections(model, spaces):
        if g.has_node(c["a"]) and g.has_node(c["b"]):
            _add(g, c["a"], c["b"], "vertically_connected", via=c["via"],
                 kind=c["kind"])
    return g


_IFC2GRAPH_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "third_party", "Ifc2Graph")


def m_ifc2graph(model, spaces, ifc_path):
    """External baseline: JBjoernskov/Ifc2Graph, run as its own published code.

    This library does its own independent IfcOpenShell extraction and 3D
    voxel-grid adjacency test over IfcSpace meshes -- it shares no code path
    with our parser. It has no room-recovery stage: a room our pipeline
    infers geometrically (no IfcSpace at all) is simply invisible to it, so
    such rooms correctly get zero edges here rather than being faked.

    It also answers a different question than our GT: physical mesh
    adjacency, not door-mediated connectivity. Two rooms stacked directly on
    top of each other with a floor slab between them and no stair share a
    voxel boundary and are reported adjacent, which our GT (correctly) does
    not consider connected. That is not a bug in the adapter -- it is a
    property of the published method being measured, and is exactly the kind
    of thing worth reporting rather than filtering out.
    """
    if _IFC2GRAPH_ROOT not in sys.path:
        sys.path.insert(0, _IFC2GRAPH_ROOT)
    import ifc2graph.ifcgeometry as ifcgeometry

    g = _base_graph(model, spaces)
    by_name = defaultdict(list)
    for sp in spaces:
        if sp.name:
            by_name[sp.name].append(sp.gid)

    name = os.path.splitext(os.path.basename(ifc_path))[0]
    try:
        geo = ifcgeometry.IfcGeometry(ifc_path, name, force_init=True)
        adj = geo.get_adjacent_spaces_dict()
    except Exception as e:
        g.graph["ifc2graph_error"] = f"{type(e).__name__}: {e}"
        return g

    seen = set()
    for room_name, neighbours in adj.items():
        for gid_a in by_name.get(room_name, []):
            for other_name in neighbours:
                if other_name == "Outside":
                    continue
                for gid_b in by_name.get(other_name, []):
                    key = tuple(sorted((gid_a, gid_b)))
                    if key in seen or gid_a == gid_b:
                        continue
                    seen.add(key)
                    _add(g, gid_a, gid_b, "open_passage", width=None,
                         openness=1.0, source="ifc2graph")
    return g


def m_ours(model, spaces, ifc_path, built=None):
    """The actual pipeline output -- never a reimplementation of it.

    An earlier version of this function duplicated graph.build()'s connectivity
    logic, and silently went stale when the real pipeline gained door/passage
    deduplication. Every reported score for "ours" was then measuring code that
    no longer existed. The baseline harness must call the method, not imitate
    it.
    """
    return built if built is not None else G.build(model)


METHODS = {"schema": m_schema, "adjacency": m_adjacency,
           "door_near": m_door_near, "ours": m_ours,
           "ifc2graph": m_ifc2graph}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="dataset/synthetic_data/synthetic")
    ap.add_argument("--out", default="results/baselines")
    ap.add_argument("--methods", nargs="*", default=list(METHODS))
    args = ap.parse_args()

    for m in args.methods:
        os.makedirs(os.path.join(args.out, m), exist_ok=True)

    for ifc in sorted(glob.glob(os.path.join(args.src, "*.ifc"))):
        name = os.path.splitext(os.path.basename(ifc))[0]
        model = P.parse(ifc)
        # One room set, shared by every method.
        full = G.build(model)
        spaces = full.graph["spaces_obj"]
        for m in args.methods:
            g = (m_ours(model, spaces, ifc, built=full) if m == "ours"
                 else METHODS[m](model, spaces, ifc))
            g.graph["stats"] = {"method": m}
            g.graph["model"] = ifc
            G.save(g, os.path.join(args.out, m, f"{name}.graph.json"))
        print(f"  {name}: {len(spaces)} rooms -> {len(args.methods)} methods")


if __name__ == "__main__":
    main()
