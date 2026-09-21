"""Stage 5 -- assembly of the hybrid hierarchical scene graph.

Hierarchy (vertical edges):   building -> storey -> space -> element
Topology  (lateral edges):    space <-> space   (adjacent / connected_by_door)
                              space <-> space   (vertically_connected)

"Hybrid" is meant in two senses: the graph mixes layers that were *read* from
IFC with layers that had to be *recovered* geometrically, and every node records
which of the two it was. Downstream consumers can therefore reason about their
own provenance -- a navigation planner may want to trust only stated geometry,
while a QA system is happy to use inferred rooms.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import networkx as nx
from shapely.ops import unary_union

from . import topology
from .parser import CLUTTER_TYPES
from .spaces import infer_rooms_for_storey

log = logging.getLogger(__name__)

# Bumped when the serialised shape changes. v2 gave every edge an explicit key.
SCHEMA_VERSION = 2

# Element classes worth a node. Curtain-wall mullions and similar are dropped:
# a single model yields tens of thousands of them and they carry no semantics
# a robot or a QA system can use (cf. Clio's task-driven pruning).
KEEP_ELEMENTS = {
    "IfcDoor", "IfcWindow", "IfcWall", "IfcStair", "IfcStairFlight",
    "IfcColumn", "IfcFurniture", "IfcSanitaryTerminal", "IfcRailing",
    "IfcSlab", "IfcCurtainWall", "IfcTransportElement", "IfcLightFixture",
}


def build(model, infer_missing_rooms: bool = True) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    stats = {}

    repair = topology.assign_storeys(model)

    # ---- building ----
    bid = "building"
    G.add_node(bid, layer="building", label=model.building_name or "Building",
               project=model.project_name, source="ifc")

    # ---- storeys ----
    for st in model.storeys:
        G.add_node(st.gid, layer="storey", label=st.name,
                   elevation=round(st.elevation, 3), source="ifc")
        G.add_edge(bid, st.gid, key="contains", relation="contains")

    # ---- spaces: stated, plus inferred to fill what the modeller left out ----
    #
    # Running inference only on storeys with *no* IfcSpace is not enough. Rooms
    # are typically drawn for lettable areas but not for the circulation that
    # joins them, so a storey can be "covered" and still have every corridor
    # missing -- which severs door connectivity between otherwise adjacent
    # rooms. We therefore infer on every storey and keep whatever does not
    # already overlap a stated room.
    spaces = list(model.spaces)
    inferred = 0
    if infer_missing_rooms:
        for st in model.storeys:
            ceil = topology.storey_ceiling(model, st)
            prods = [p for p in model.products if p.storey_gid == st.gid]
            stated = [s.footprint for s in model.spaces
                      if s.storey_gid == st.gid and s.footprint is not None]
            for cand in infer_rooms_for_storey(prods, st.elevation, ceil, st.gid):
                for part in _subtract_stated(cand, stated):
                    spaces.append(part)
                    inferred += 1

    for sp in spaces:
        G.add_node(sp.gid, layer="space",
                   label=sp.long_name or sp.name or "room",
                   room_number=sp.name,
                   area=round(sp.footprint.area, 2) if sp.footprint else None,
                   height=round(sp.zmax - sp.zmin, 2),
                   centroid=([round(c, 3) for c in sp.footprint.centroid.coords[0]]
                             if sp.footprint else None),
                   elevation=round(sp.zmin, 3),
                   source=sp.source)
        if sp.storey_gid and G.has_node(sp.storey_gid):
            G.add_edge(sp.storey_gid, sp.gid, key="contains", relation="contains")

    # ---- elements ----
    containment = topology.assign_products_to_spaces(model, spaces)
    kept = 0
    for p in model.products:
        if p.ifc_type not in KEEP_ELEMENTS:
            continue
        G.add_node(p.gid, layer="element", ifc_type=p.ifc_type,
                   label=_clean_name(p.name) or p.ifc_type,
                   centroid=[round(c, 3) for c in p.centroid],
                   zmin=round(p.zmin, 3), zmax=round(p.zmax, 3),
                   clutter=p.ifc_type in CLUTTER_TYPES,
                   source="ifc")
        kept += 1
        host = containment.get(p.gid)
        if host and G.has_node(host):
            G.add_edge(host, p.gid, key="contains", relation="contains",
                       provenance="inferred" if not p.space_gid else "ifc")
        elif p.storey_gid and G.has_node(p.storey_gid):
            G.add_edge(p.storey_gid, p.gid, key="contains", relation="contains",
                       provenance="fallback")

    # ---- lateral topology ----
    #
    # Every lateral edge carries its own identity. Keying a MultiDiGraph on the
    # relation name collapsed two doors between the same pair of rooms into a
    # single edge, so which of them survived depended on the order the parser
    # emitted products in -- and with it whether a route wide enough for the
    # robot existed at all. The key is the portal id, shared by the two
    # directed edges standing for the same physical transition, so one portal
    # stays one thing while the graph stays directed.
    portals = {}
    adj = topology.space_adjacency(spaces)
    for a, b, gap in adj:
        eid = f"adj:{_pair(a, b)}"
        for u, v in ((a, b), (b, a)):
            G.add_edge(u, v, key=eid, edge_id=eid, relation="adjacent_to",
                       gap=gap, provenance="inferred")

    doors = topology.door_connections(model, spaces)
    for c in sorted(doors, key=_candidate_order):
        if c["exterior"] or c["b"] is None:
            if G.has_node(c["a"]):
                G.nodes[c["a"]]["has_exterior_door"] = True
            continue
        pid = _portal_id("door", c.get("door"), c["a"], c["b"], portals)
        for u, v in ((c["a"], c["b"]), (c["b"], c["a"])):
            G.add_edge(u, v, key=pid, edge_id=pid, portal_id=pid,
                       relation="connected_by_door",
                       door=c["door"], width=c["width"], provenance="inferred")

    passages = topology.open_passages(model, spaces, adj)
    existing = {frozenset((c["a"], c["b"])) for c in doors
                if not c["exterior"] and c["b"] is not None}
    # Doors already resolved by normal-probing must not be re-used by the
    # geometric recovery for a different pair, or one door would open into
    # more than two rooms.
    used_doors = {c["door"] for c in doors if c.get("door")}
    n_passage = n_recovered_door = 0
    for c in sorted(passages, key=_candidate_order):
        if frozenset((c["a"], c["b"])) in existing:
            continue                     # door probing already resolved this pair
        if c.get("kind") == "door":
            if c.get("door") in used_doors:
                continue
            used_doors.add(c.get("door"))
            n_recovered_door += 1
            pid = _portal_id("door", c.get("door"), c["a"], c["b"], portals)
            for u, v in ((c["a"], c["b"]), (c["b"], c["a"])):
                G.add_edge(u, v, key=pid, edge_id=pid, portal_id=pid,
                           relation="connected_by_door", door=c.get("door"),
                           width=c.get("width"), provenance="inferred")
        else:
            n_passage += 1
            pid = _portal_id("passage", None, c["a"], c["b"], portals)
            for u, v in ((c["a"], c["b"]), (c["b"], c["a"])):
                G.add_edge(u, v, key=pid, edge_id=pid, portal_id=pid,
                           relation="open_passage",
                           width=c.get("width"), openness=c["openness"],
                           provenance="inferred")

    vert = topology.vertical_connections(model, spaces)
    for c in sorted(vert, key=_candidate_order):
        pid = _portal_id("vconn", c.get("via"), c["a"], c["b"], portals)
        for u, v in ((c["a"], c["b"]), (c["b"], c["a"])):
            if G.has_node(u) and G.has_node(v):
                G.add_edge(u, v, key=pid, edge_id=pid, portal_id=pid,
                           relation="vertically_connected",
                           via=c["via"], kind=c["kind"],
                           rise=c.get("rise"),
                           # What the link rests on. A connection induced by two
                           # rooms both being called "Stair" is evidence of a
                           # different kind from one carved by a stair solid,
                           # and anything masking labels has to be able to tell
                           # them apart -- otherwise a masked label survives in
                           # the topology it produced.
                           basis=c.get("basis"),
                           footprint_extent=c.get("footprint_extent"),
                           width=c.get("clear_width"),
                           provenance="inferred")

    _label_circulation(G)

    stats.update({
        "storeys": len(model.storeys),
        "spaces_total": len(spaces),
        "spaces_from_ifc": len(model.spaces),
        "spaces_inferred": inferred,
        "elements": kept,
        "adjacency_edges": len(adj),
        "door_connections": sum(1 for c in doors if not c["exterior"]),
        "exterior_doors": sum(1 for c in doors if c["exterior"]),
        "open_passages": n_passage,
        "doors_recovered_geometrically": n_recovered_door,
        "vertical_connections": len(vert),
        "containment_edges": len(containment),
        **repair,
    })
    G.graph["stats"] = stats
    G.graph["model"] = model.path
    # Retained in-memory only (shapely geometry, never serialised) so callers
    # such as the plan exporter can reach room footprints without re-running
    # the whole recovery.
    G.graph["spaces_obj"] = spaces
    return G


def _pair(a: str, b: str) -> str:
    """Endpoint token that does not depend on which direction is being added."""
    return f"{a}|{b}" if str(a) <= str(b) else f"{b}|{a}"


def _candidate_order(c: dict):
    """Deterministic order over recovery candidates.

    Identity must be a property of the building, not of the order IfcProduct
    happened to appear in the file. Sorting the candidates before they are
    given ids is what makes a reordered input produce the same graph.
    """
    return (str(c.get("door") or c.get("via") or ""),
            _pair(c.get("a") or "", c.get("b") or ""),
            str(c.get("width") if c.get("width") is not None else ""))


def _portal_id(kind: str, via, a: str, b: str, seen: dict) -> str:
    """A stable identity for one physical transition.

    Two doors joining the same two rooms are two portals, and a robot that fits
    through one may not fit through the other. Identity therefore comes from
    the element that makes the transition -- the door's GlobalId -- and falls
    back to an occurrence counter only where the recovery had no element to
    point at.
    """
    pair = _pair(a, b)
    if via:
        pid = f"{kind}:{via}"
        owner = seen.get(pid)
        if owner is None or owner == pair:
            seen[pid] = pair
            return pid
        # The same element resolved to a second pair of rooms. That is a
        # conflict worth keeping visible, not a reason to drop one of them.
    n = seen.get((kind, pair), 0)
    seen[(kind, pair)] = n + 1
    return f"{kind}:{pair}:{n}"


def _label_circulation(G) -> None:
    """Name inferred rooms by the function their connectivity reveals.

    A recovered region has no IfcSpace label to inherit, so it starts as
    "room_N". But a large inferred space that many rooms open onto is not a
    room -- it is the corridor, deck or lobby that serves them, and calling it
    so is what makes a route legible ("via Circulation", not "via room_1").
    """
    for n, d in G.nodes(data=True):
        if d.get("layer") != "space" or d.get("source") != "inferred":
            continue
        if not str(d.get("label", "")).startswith("room_"):
            continue           # already named (exterior deck, open floor plate)
        degree = sum(1 for _, _, e in G.out_edges(n, data=True)
                     if e.get("relation") in ("connected_by_door", "open_passage"))
        area = d.get("area") or 0
        if degree >= 4 and area >= 25.0:
            d["label"] = "Circulation"
        elif area >= 120.0:
            d["label"] = "Open Area"


def _subtract_stated(cand, stated, min_area: float = 2.0):
    """Keep only the part of an inferred region the IFC does not already state.

    Discarding any component that overlaps a stated room is too blunt. Where a
    doorway is open at slice height, the free-space component spans the corridor
    *and* the rooms off it; dropping it wholesale deletes the corridor, which is
    precisely the node that makes those rooms mutually reachable. Subtracting
    the stated rooms leaves the corridor behind instead.
    """
    if cand.footprint is None:
        return []
    if not stated:
        return [cand]
    try:
        rest = cand.footprint.difference(unary_union(stated))
    except Exception:
        return []
    if rest.is_empty:
        return []

    parts = [rest] if rest.geom_type == "Polygon" else [
        g for g in getattr(rest, "geoms", []) if g.geom_type == "Polygon"]
    out = []
    for i, p in enumerate(parts):
        # Erode/dilate to drop the slivers left along shared wall faces.
        p = p.buffer(-0.25).buffer(0.25)
        for q in ([p] if p.geom_type == "Polygon"
                  else [g for g in getattr(p, "geoms", []) if g.geom_type == "Polygon"]):
            if q.is_empty or q.area < min_area:
                continue
            out.append(replace(cand, gid=f"{cand.gid}-r{i}{len(out)}",
                               footprint=q))
    return out


def _clean_name(n: str) -> str:
    """Revit exports names like 'Basic Wall:Generic - 200mm:1595769'."""
    if not n:
        return ""
    parts = [p for p in n.split(":") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        parts = parts[:-1]
    return ":".join(parts[:2])


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

def to_json(G: nx.MultiDiGraph) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "model": G.graph.get("model"),
        "stats": G.graph.get("stats", {}),
        # Endpoints and key are written last: an attribute called "source",
        # "target" or "key" must never be able to overwrite the edge's own
        # identity.
        "nodes": [{**d, "id": n} for n, d in G.nodes(data=True)],
        "edges": [{**d, "key": k, "source": u, "target": v}
                  for u, v, k, d in G.edges(keys=True, data=True)],
    }


def load(path: str) -> nx.MultiDiGraph:
    """Reload a serialised graph (inverse of `save`)."""
    with open(path) as fh:
        d = json.load(fh)
    G = nx.MultiDiGraph()
    G.graph["stats"] = d.get("stats", {})
    G.graph["model"] = d.get("model")
    G.graph["schema"] = d.get("schema")
    for n in d["nodes"]:
        n = dict(n)
        G.add_node(n.pop("id"), **n)
    for i, e in enumerate(d["edges"]):
        e = dict(e)
        u, v = e.pop("source"), e.pop("target")
        k = e.pop("key", None)
        if k is None:
            # Written before edges carried identity. Keying on the relation
            # would merge the parallel edges that did survive into the file, so
            # each gets a distinct key instead: a graph must reload as
            # faithfully as it was written, never less.
            k = f"{e.get('relation')}:{i}"
        G.add_edge(u, v, key=k, **e)
    return G


def save(G: nx.MultiDiGraph, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(to_json(G), fh, indent=1)
    log.info("wrote %s", path)


def summary(G: nx.MultiDiGraph) -> str:
    s = G.graph.get("stats", {})
    lines = [
        f"  storeys              {s.get('storeys')}",
        f"  spaces               {s.get('spaces_total')} "
        f"({s.get('spaces_from_ifc')} from IFC, {s.get('spaces_inferred')} inferred)",
        f"  elements             {s.get('elements')}",
        f"  room-room adjacency  {s.get('adjacency_edges')}",
        f"  door connections     {s.get('door_connections')} "
        f"(+{s.get('exterior_doors')} exterior)",
        f"  open passages        {s.get('open_passages')}",
        f"  vertical links       {s.get('vertical_connections')}",
        f"  nodes / edges        {G.number_of_nodes()} / {G.number_of_edges()}",
    ]
    return "\n".join(lines)
