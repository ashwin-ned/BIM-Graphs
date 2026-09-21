"""Validation harness for the recovered graphs.

There is no ground-truth scene graph for these models -- that is the whole
problem -- so validation is by *necessary conditions* a correct building graph
must satisfy. Each check is something that can only pass if the recovery stage
did its job:

  R1a horizontal    rooms on a storey are mutually reachable *within* that
                    storey -- this is what the recovery stage controls
  R1b vertical      storeys are linked to each other, but only where the file
                    actually models vertical circulation. model_4 contains
                    zero IfcStair, IfcRamp and IfcTransportElement across 7
                    storeys, so its floors cannot be joined by any method; the
                    check is skipped rather than failed, because a missing
                    staircase is a property of the data, not a pipeline defect.
  R2 door degree    every interior door joins exactly two rooms
  R3 hierarchy      every room sits under exactly one storey
  R4 plausibility   room areas and storey heights are physically sensible
  R5 coverage       elements are assigned to rooms, not stranded on storeys
  R6 containment    a room's elements actually lie inside its footprint
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import networkx as nx
from bimsg import graph as GR
from bimsg import navigation as NAV
from bimsg import qa


def st_has_circulation(G) -> bool:
    """Does the model contain any element that could join two storeys?"""
    kinds = {"IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight",
             "IfcTransportElement"}
    return any(d.get("ifc_type") in kinds for _, d in G.nodes(data=True))


def check(path: str) -> dict:
    G = GR.load(path)
    idx = qa.BuildingIndex(G)
    st = G.graph.get("stats", {})
    r = {"model": os.path.basename(path).replace(".graph.json", "")}

    # R1 -- connectivity, split into what the pipeline controls and what the
    # source file has to provide.
    reach = NAV.reachability(G)
    r["rooms"] = reach["rooms"]
    r["reachable"] = reach["reachable_fraction"]
    r["components"] = reach["components"]

    NG = NAV.navigation_graph(G)
    # R1a: connectivity restricted to each storey in turn.
    fracs = []
    for _storey, rooms in idx.rooms_in_storey.items():
        rooms = [x for x in rooms if x in NG]
        if len(rooms) < 2:
            continue
        sub = NG.subgraph(rooms)
        comps = sorted(nx.connected_components(sub), key=len, reverse=True)
        fracs.append(len(comps[0]) / len(rooms))
    r["intra_storey"] = round(sum(fracs) / len(fracs), 3) if fracs else 1.0
    r["R1a_horizontal"] = r["intra_storey"] >= 0.90

    # R1b: are the storeys joined? Only meaningful if the file models any
    # vertical circulation at all.
    # Build a graph over storeys and ask whether it is connected: every storey
    # holding rooms should be reachable from every other.
    SG = nx.Graph()
    populated = {s for s, rooms in idx.rooms_in_storey.items() if rooms}
    SG.add_nodes_from(populated)
    vert = 0
    for u, v, d in G.edges(data=True):
        if d.get("relation") != "vertically_connected":
            continue
        vert += 1
        su, sv = idx.storey_of.get(u), idx.storey_of.get(v)
        if su in populated and sv in populated and su != sv:
            SG.add_edge(su, sv)
    r["vertical_edges"] = vert
    r["storeys_populated"] = len(populated)
    r["has_vertical_circulation"] = st_has_circulation(G)
    if not r["has_vertical_circulation"] or len(populated) < 2:
        r["R1b_vertical"] = None          # not applicable: nothing to recover
    else:
        r["R1b_vertical"] = nx.number_connected_components(SG) == 1

    # R2 -- every door joins exactly two rooms
    per_door = defaultdict(set)
    for u, v, d in G.edges(data=True):
        if d.get("relation") == "connected_by_door" and d.get("door"):
            per_door[d["door"]].update((u, v))
    bad = sum(1 for s in per_door.values() if len(s) != 2)
    r["doors_resolved"] = len(per_door)
    r["R2_doors_binary"] = bad == 0

    # R3 -- one storey per room
    multi = 0
    for s in idx.spaces:
        hosts = [u for u, _, d in G.in_edges(s, data=True)
                 if d.get("relation") == "contains" and u in idx.storeys]
        if len(hosts) != 1:
            multi += 1
    r["rooms_without_unique_storey"] = multi
    r["R3_hierarchy"] = multi == 0

    # R4 -- physical plausibility
    areas = [d["area"] for d in idx.spaces.values() if d.get("area")]
    elevs = sorted({d.get("elevation") or 0 for d in idx.storeys.values()})
    heights = [b - a for a, b in zip(elevs, elevs[1:])]
    r["median_area"] = round(sorted(areas)[len(areas) // 2], 1) if areas else 0
    r["median_storey_h"] = round(sorted(heights)[len(heights) // 2], 2) if heights else 0
    r["R4_plausible"] = (
        bool(areas) and 1.0 <= r["median_area"] <= 500.0
        and (not heights or 2.0 <= r["median_storey_h"] <= 6.5))

    # R5 -- element coverage
    total = len(idx.elements)
    placed = sum(len(v) for v in idx.elements_in_room.values())
    r["elements"] = total
    r["element_coverage"] = round(placed / total, 3) if total else 0.0
    r["R5_coverage"] = r["element_coverage"] >= 0.50

    # R6 -- containment is geometrically true
    ok = tested = 0
    for room, els in idx.elements_in_room.items():
        c = idx.spaces[room].get("centroid")
        area = idx.spaces[room].get("area") or 0
        if not c or area <= 0:
            continue
        radius = (area ** 0.5) * 1.5 + 2.0
        for e in els[:40]:
            ec = idx.elements[e].get("centroid")
            if not ec:
                continue
            tested += 1
            if ((ec[0] - c[0]) ** 2 + (ec[1] - c[1]) ** 2) ** 0.5 <= radius:
                ok += 1
    r["containment_sane"] = round(ok / tested, 3) if tested else 1.0
    r["R6_containment"] = r["containment_sane"] >= 0.90

    r["inferred_rooms"] = st.get("spaces_inferred", 0)
    r["ifc_rooms"] = st.get("spaces_from_ifc", 0)
    r["passed"] = sum(1 for k, v in r.items() if k.startswith("R") and v is True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default="results/*.graph.json")
    args = ap.parse_args()

    rows = [check(p) for p in sorted(glob.glob(args.pattern))]
    if not rows:
        print("no graphs found")
        return

    cols = ["model", "rooms", "ifc_rooms", "inferred_rooms", "elements",
            "doors_resolved", "intra_storey", "vertical_edges", "reachable",
            "element_coverage", "median_area", "median_storey_h", "passed"]
    w = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))

    checks = ["R1a_horizontal", "R1b_vertical", "R2_doors_binary",
              "R3_hierarchy", "R4_plausible", "R5_coverage", "R6_containment"]
    print("\n--- CHECKS PASSED (of %d models) ---" % len(rows))
    for c in checks:
        applicable = [r for r in rows if r.get(c) is not None]
        n = sum(1 for r in applicable if r.get(c))
        na = len(rows) - len(applicable)
        suffix = f"  ({na} n/a)" if na else ""
        flag = "" if n == len(applicable) else "   <-- FAILURES"
        print(f"  {c:20s} {n}/{len(applicable)}{suffix}{flag}")
        if n != len(applicable):
            print("       failing: " +
                  ", ".join(r["model"] for r in applicable if not r.get(c)))

    tot = len(rows)
    print(f"\n  mean intra-storey     "
          f"{sum(r['intra_storey'] for r in rows)/tot:.3f}")
    print(f"  mean reachability     {sum(r['reachable'] for r in rows)/tot:.3f}")
    print(f"  mean element coverage {sum(r['element_coverage'] for r in rows)/tot:.3f}")
    print(f"  total rooms           {sum(r['rooms'] for r in rows)} "
          f"({sum(r['ifc_rooms'] for r in rows)} IFC + "
          f"{sum(r['inferred_rooms'] for r in rows)} inferred)")

    with open("results/validation.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    print("\nwrote results/validation.json")


if __name__ == "__main__":
    main()
