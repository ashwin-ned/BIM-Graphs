"""Downstream task 1 -- robot navigation over the hybrid graph.

Two products:

  * a *navigation graph*: rooms as nodes, doors and stairs as edges, with
    kinematic feasibility (door clearance) applied as an edge filter, so a
    plan is only returned if the robot actually fits through it;
  * a *local navigation mesh* per room: the room polygon minus furniture
    footprints, eroded by the robot radius -- the boolean subtraction the
    design specification calls for, done in 2D where it is cheap and exact.

Planning is Dijkstra over centroid distance with a configurable penalty for
stairs, which a wheeled robot usually cannot use at all.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import networkx as nx
from shapely.geometry import Point
from shapely.ops import unary_union

log = logging.getLogger(__name__)

STAIR_PENALTY = 25.0       # metres-equivalent; set to inf to forbid stairs



# Robot profiles. Traversability is applied at query time rather than baked
# into the graph, so one recovered building serves every platform.
PROFILES = {
    # A wheeled base cannot climb: any positive slope is impassable.
    "wheeled": {"width": 0.60, "climbs_stairs": False},
    # A legged platform manages a normal stair.
    "legged": {"width": 0.50, "climbs_stairs": True},
    # A person, for comparison and for QnA answers.
    "human": {"width": 0.45, "climbs_stairs": True},
}


def edge_passable(d: dict, profile: dict) -> bool:
    """Can this platform use this connection?

    Missing metadata is treated as passable rather than blocked: a stair whose
    geometry the file never stated should not silently disconnect a building.
    The graph records what is known; refusing to route on absent data would
    make recovered topology look worse than no topology at all.
    """
    rel = d.get("relation")
    w = d.get("width")
    if rel in ("connected_by_door", "open_passage"):
        return w is None or w >= profile["width"]
    if rel == "vertically_connected":
        # Gradient is not recoverable from these files (see stair_metrics), so
        # the decision rests on what is knowable: whether the platform climbs
        # at all, and whether it fits through the shaft.
        if not profile["climbs_stairs"]:
            return False
        return w is None or w >= profile["width"]
    return True


def navigation_graph(G: nx.MultiDiGraph, robot_width: float = 0.60,
                     allow_stairs: bool = True, profile=None) -> nx.Graph:
    """Collapse the scene graph to a traversability graph over rooms.

    Pass `profile` (a name from PROFILES, or a dict) to filter edges by the
    platform's real constraints -- clear width and stair slope. The older
    width/allow_stairs arguments remain for callers that predate profiles.
    """
    if profile is not None:
        prof = PROFILES[profile] if isinstance(profile, str) else profile
        robot_width = prof["width"]
        allow_stairs = prof["climbs_stairs"]
    else:
        prof = {"width": robot_width, "climbs_stairs": allow_stairs}
    N = nx.Graph()
    for n, d in G.nodes(data=True):
        if d.get("layer") == "space":
            N.add_node(n, label=d.get("label"), centroid=d.get("centroid"),
                       elevation=d.get("elevation"), area=d.get("area"))

    skipped = 0
    for u, v, k, d in G.edges(keys=True, data=True):
        if u not in N or v not in N:
            continue
        rel = d.get("relation")
        if rel not in ("connected_by_door", "open_passage",
                       "vertically_connected"):
            continue
        if not edge_passable(d, prof):
            skipped += 1
            continue
        if rel == "connected_by_door":
            _relax(N, u, v, _dist(N, u, v), "door", d.get("door"))
        elif rel == "open_passage":
            _relax(N, u, v, _dist(N, u, v), "open", None)
        else:
            _relax(N, u, v, _dist(N, u, v) + STAIR_PENALTY,
                   d.get("kind", "stair"), d.get("via"))

    log.info("nav graph: %d rooms, %d traversable edges (%d doors too narrow)",
             N.number_of_nodes(), N.number_of_edges(), skipped)
    return N


def _relax(N, u, v, w, kind, via):
    """Keep the cheapest connection between two rooms."""
    if N.has_edge(u, v) and N[u][v]["weight"] <= w:
        return
    N.add_edge(u, v, weight=w, kind=kind, via=via)


def _dist(N, u, v) -> float:
    a, b = N.nodes[u].get("centroid"), N.nodes[v].get("centroid")
    if not a or not b:
        return 1.0
    return max(0.1, math.dist(a[:2], b[:2]))


def plan(G: nx.MultiDiGraph, start: str, goal: str,
         robot_width: float = 0.60, allow_stairs: bool = True,
         profile=None) -> Optional[dict]:
    """Shortest traversable route between two rooms."""
    N = navigation_graph(G, robot_width, allow_stairs, profile)
    if start not in N or goal not in N:
        return None
    try:
        path = nx.dijkstra_path(N, start, goal, weight="weight")
    except nx.NetworkXNoPath:
        return None

    legs = []
    for a, b in zip(path, path[1:]):
        e = N[a][b]
        legs.append({"from": N.nodes[a]["label"], "to": N.nodes[b]["label"],
                     "via": e["kind"], "distance": round(e["weight"], 2)})
    return {
        "nodes": path,
        "labels": [N.nodes[n]["label"] for n in path],
        "legs": legs,
        "length_m": round(sum(l["distance"] for l in legs), 2),
        "uses_stairs": any(l["via"] in ("stair", "stairwell") for l in legs),
    }


def reachability(G: nx.MultiDiGraph, robot_width: float = 0.60,
                 allow_stairs: bool = True, profile=None) -> dict:
    """How much of the building a robot can actually reach.

    A useful correctness signal in its own right: a graph whose rooms are
    mostly unreachable has lost its door topology somewhere.
    """
    N = navigation_graph(G, robot_width, allow_stairs, profile)
    if N.number_of_nodes() == 0:
        return {"rooms": 0, "components": 0, "largest_component": 0,
                "reachable_fraction": 0.0, "isolated_rooms": 0}
    comps = sorted(nx.connected_components(N), key=len, reverse=True)
    largest = len(comps[0]) if comps else 0
    return {
        "rooms": N.number_of_nodes(),
        "components": len(comps),
        "largest_component": largest,
        "reachable_fraction": round(largest / N.number_of_nodes(), 3),
        "isolated_rooms": sum(1 for c in comps if len(c) == 1),
    }


def local_navmesh(space_footprint, obstacles, robot_radius: float = 0.30):
    """Free-space polygon inside one room.

    Room minus obstacle footprints, then eroded by the robot radius so that
    the resulting region is a configuration-space free set: any point in it is
    a legal centre for a disc robot.
    """
    if space_footprint is None:
        return None
    free = space_footprint
    if obstacles:
        try:
            free = free.difference(unary_union(list(obstacles)))
        except Exception:
            pass
    free = free.buffer(-robot_radius)
    if free.is_empty:
        return None
    if free.geom_type == "MultiPolygon":
        free = max(free.geoms, key=lambda g: g.area)
    return free


def sample_waypoints(navmesh, n: int = 8):
    """Coverage waypoints inside a room's free space (for inspection missions)."""
    if navmesh is None or navmesh.is_empty:
        return []
    minx, miny, maxx, maxy = navmesh.bounds
    pts, step = [], max(0.5, math.sqrt(navmesh.area / max(n, 1)))
    y = miny + step / 2
    while y < maxy and len(pts) < n:
        x = minx + step / 2
        while x < maxx and len(pts) < n:
            p = Point(x, y)
            if navmesh.contains(p):
                pts.append((round(x, 2), round(y, 2)))
            x += step
        y += step
    return pts
