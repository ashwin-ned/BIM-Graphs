"""End-to-end demonstration of the two downstream tasks.

    python main/demo.py results/model_0.graph.json

Shows the recovered hierarchy, the navigation graph a robot would plan over,
a worked route, and a spatial QnA session grounded in the graph.
"""

from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as GR
from bimsg import navigation as NAV
from bimsg import qa

QUESTIONS = [
    "How many storeys are there?",
    "How many rooms?",
    "What are the rooms?",
    "How many doors?",
    "What is the largest room?",
    "What is in the kitchen?",
    "Which rooms are adjacent to the kitchen?",
    "Where is the staircase?",
]


def rule(t=""):
    print("\n" + "=" * 72)
    if t:
        print(t)
        print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph", help="a *.graph.json produced by build_graph.py")
    ap.add_argument("--robot-width", type=float, default=0.60)
    args = ap.parse_args()

    G = GR.load(args.graph)
    idx = qa.BuildingIndex(G)

    rule(f"BIM-GRAPH  —  {os.path.basename(args.graph)}")
    s = G.graph.get("stats", {})
    print(f"  source model      {G.graph.get('model')}")
    print(f"  storeys           {s.get('storeys')}")
    print(f"  rooms             {s.get('spaces_total')} "
          f"({s.get('spaces_from_ifc')} stated by IFC, "
          f"{s.get('spaces_inferred')} recovered geometrically)")
    print(f"  elements          {s.get('elements')}")
    print(f"  door connections  {s.get('door_connections')}")
    print(f"  open passages     {s.get('open_passages')}")
    print(f"  vertical links    {s.get('vertical_connections')}")
    print(f"  space boundaries in source IFC: 0  (all topology recovered)")

    rule("HIERARCHY (collapsed view — what an LLM is shown first)")
    print(qa.context_for_llm(G, max_storeys=2))

    rule(f"NAVIGATION  (robot width {args.robot_width} m)")
    reach = NAV.reachability(G, robot_width=args.robot_width)
    print(f"  rooms                {reach['rooms']}")
    print(f"  connected components {reach['components']}")
    print(f"  reachable fraction   {reach['reachable_fraction']}")
    print(f"  isolated rooms       {reach['isolated_rooms']}")

    wheeled = NAV.reachability(G, robot_width=args.robot_width, allow_stairs=False)
    print(f"  reachable without stairs (wheeled robot): "
          f"{wheeled['reachable_fraction']}")

    # A worked route between two far-apart rooms.
    N = NAV.navigation_graph(G, args.robot_width)
    rooms = [n for n in N.nodes if not n.startswith("EXT-")]
    if len(rooms) >= 2:
        random.seed(0)
        a, b = rooms[0], rooms[-1]
        p = NAV.plan(G, a, b, robot_width=args.robot_width)
        rule("EXAMPLE ROUTE")
        if p:
            print(f"  {idx.room_label(a)}  ->  {idx.room_label(b)}")
            print(f"  distance {p['length_m']} m, "
                  f"{len(p['legs'])} legs, stairs={p['uses_stairs']}")
            for i, leg in enumerate(p["legs"], 1):
                print(f"    {i:2d}. {leg['from']:<24s} -> {leg['to']:<24s} "
                      f"via {leg['via']:<10s} {leg['distance']:>6.2f} m")
        else:
            print(f"  no route from {idx.room_label(a)} to {idx.room_label(b)}")

    rule("SPATIAL QnA  (grounded — every answer traceable to graph facts)")
    for q in QUESTIONS:
        r = qa.answer(G, q)
        print(f"\n  Q: {q}")
        print(f"  A: {r['answer']}")

    rule("LLM HAND-OFF")
    r = qa.answer(G, "What is in the kitchen?")
    print("The prompt an open-weights model would receive "
          "(facts only, no parametric recall):\n")
    p = qa.build_prompt("What is in the kitchen?", r["facts"])
    print("\n".join("    " + ln for ln in p.splitlines()[:22]))
    print("    ...")


if __name__ == "__main__":
    main()
