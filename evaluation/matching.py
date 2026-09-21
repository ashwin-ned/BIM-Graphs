"""Assign predicted rooms to reference rooms, one to one.

The old scorer mapped a recovered room to whichever reference rectangle
contained its centroid, with nothing stopping five recovered rooms from all
claiming the same reference room -- or an invented room in the car park from
claiming none and quietly leaving the scoring set along with every edge that
touched it. A method could then add rooms and edges at no cost.

Matching here is explicit and total:

* every reference room is matched to at most one predicted room, and vice
  versa;
* predicted rooms that match nothing are kept in `unmatched_pred`, so their
  edges can still be counted against the method;
* reference rooms that nothing matched are kept in `unmatched_gt`;
* where several predictions compete for one reference room, or one prediction
  spans several reference rooms, the losing associations are reported as
  `merges` and `splits` rather than discarded. A room recovered as one region
  where the building has two is a specific, nameable failure, and it should not
  read the same as a clean hit plus a miss.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .schema import IDENTITY_NODE_ID, IDENTITY_ROOM_NUMBER

# A synthetic storey is 3.20 m; a centroid may sit anywhere in the band.
STOREY_HEIGHT = 3.20
STOREY_TOL = 1.6


@dataclass
class Matching:
    assign: dict = field(default_factory=dict)        # node -> rid
    reverse: dict = field(default_factory=dict)       # rid  -> node
    unmatched_pred: set = field(default_factory=set)
    unmatched_gt: set = field(default_factory=set)
    merges: dict = field(default_factory=dict)        # rid  -> [losing nodes]
    splits: dict = field(default_factory=dict)        # node -> [losing rids]
    by_identity: int = 0
    by_geometry: int = 0
    # Set when matching could not have succeeded for a reason that is not the
    # prediction's fault -- see `_diagnose`. A score computed over such a
    # matching is meaningless and must be reported as unscorable instead.
    unscorable: str | None = None

    def counts(self) -> dict:
        return {
            "matched": len(self.assign),
            "matched_by_identity": self.by_identity,
            "matched_by_geometry": self.by_geometry,
            "unmatched_pred": len(self.unmatched_pred),
            "unmatched_gt": len(self.unmatched_gt),
            "merged_predictions": sum(len(v) for v in self.merges.values()),
            "split_predictions": sum(len(v) for v in self.splits.values()),
        }


def predicted_rooms(g) -> list:
    return [n for n, d in g.nodes(data=True) if d.get("layer") == "space"]


def _identity_key(g, node, identity: str):
    d = g.nodes[node]
    if identity == IDENTITY_NODE_ID:
        return node
    if identity == IDENTITY_ROOM_NUMBER:
        return d.get("room_number") or d.get("label")
    return None


def _bbox_score(gt_room, d) -> float:
    """How well a predicted room's centroid sits inside a reference rectangle.

    1.0 at the centre, falling to just above 0 at the corner; 0.0 outside. Used
    only to rank competing candidates, never to decide that a match exists --
    containment does that.
    """
    c = d.get("centroid")
    if not c or not gt_room.bbox:
        return 0.0
    x0, y0, x1, y1 = gt_room.bbox
    if not (x0 <= c[0] <= x1 and y0 <= c[1] <= y1):
        return 0.0
    if gt_room.storey_index is not None:
        z = d.get("elevation")
        if z is not None and abs(gt_room.storey_index * STOREY_HEIGHT - z) > STOREY_TOL:
            return 0.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = math.hypot((x1 - x0) / 2, (y1 - y0) / 2) or 1.0
    return max(0.0, 1.0 - math.dist((c[0], c[1]), (cx, cy)) / half) * 0.999 + 0.001


def match_rooms(g, gt) -> Matching:
    """One-to-one assignment of predicted space nodes to reference rooms."""
    m = Matching()
    nodes = predicted_rooms(g)

    # ---- exact identity first -------------------------------------------
    # An id join is the only match that carries no matching error of its own,
    # so it is never overridden by geometry.
    claimed_rid, claimed_node = {}, {}
    for n in sorted(nodes, key=str):
        k = _identity_key(g, n, gt.identity)
        if k is not None and k in gt.rooms and k not in claimed_rid:
            claimed_rid[k] = n
            claimed_node[n] = k
    for n, rid in claimed_node.items():
        m.assign[n] = rid
        m.reverse[rid] = n
        m.by_identity += 1

    # ---- geometry for the rest ------------------------------------------
    candidates = []
    for n in sorted(nodes, key=str):
        if n in m.assign:
            continue
        d = g.nodes[n]
        for rid, room in gt.rooms.items():
            s = _bbox_score(room, d)
            if s > 0:
                candidates.append((s, str(n), rid))
    # Greedy on score, ties broken by name so the assignment is reproducible.
    associations = {}
    for s, n, rid in sorted(candidates, key=lambda t: (-t[0], t[1], t[2])):
        associations.setdefault(rid, []).append(n)
        if n in m.assign or rid in m.reverse:
            continue
        m.assign[n] = rid
        m.reverse[rid] = n
        m.by_geometry += 1

    # ---- what did not match, and what lost -------------------------------
    m.unmatched_pred = {n for n in nodes if n not in m.assign}
    m.unmatched_gt = {rid for rid in gt.rooms if rid not in m.reverse}
    for rid, comps in associations.items():
        losers = [n for n in comps if m.assign.get(n) != rid]
        if losers and rid in m.reverse:
            m.merges[rid] = losers
    node_claims = {}
    for s, n, rid in candidates:
        node_claims.setdefault(n, []).append(rid)
    for n, rids in node_claims.items():
        losers = [r for r in rids if m.assign.get(n) != r]
        if losers and n in m.assign:
            m.splits[n] = losers
    m.unscorable = _diagnose(g, gt, m, nodes)
    return m


def _diagnose(g, gt, m, nodes) -> str | None:
    """Why zero matches happened, when it was not the prediction's fault.

    A model that invents its own room ids cannot match by identity, so it falls
    through to geometry -- and `_bbox_score` needs `gt_room.bbox`, which **only
    the generator dialect states**. The deriver and the annotator record a
    centroid and an area but no extent, deliberately (`schema.Room.bbox`:
    inventing one from a centroid would make geometric matching look better
    than it is).

    So scoring an image-conditioned prediction against the real corpus matches
    nothing, every predicted edge lands on an unmatched endpoint, and precision
    reads 0.000 -- a number that describes the reference's missing geometry and
    not the model at all. This project has already published one metric that
    measured its own scaffolding instead of a method. Say so here instead.
    """
    if m.assign or not nodes:
        return None
    if not gt.rooms:
        return "reference states no rooms"
    keyed = sum(1 for n in nodes if _identity_key(g, n, gt.identity) in gt.rooms)
    if keyed:
        return None
    ref_has_geometry = any(gt.rooms[r].bbox for r in gt.rooms)
    anchored = sum(1 for n in nodes if g.nodes[n].get("centroid"))
    if ref_has_geometry and not anchored:
        # The mirror of the case below, and the one a harness bug produces: the
        # reference could have matched geometrically, but not one prediction
        # carries a world-metre centroid to match with. A model asked for pixel
        # coordinates whose transform never arrived looks exactly like this, and
        # it reads as precision 0.000 for a method that may have been right.
        stray = sum(1 for n in nodes if g.nodes[n].get("centroid_px")
                    or g.nodes[n].get("centroid_unknown_units"))
        return (f"the reference states room extents but none of the "
                f"{len(nodes)} predicted rooms carries a world-metre centroid"
                + (f" ({stray} carry unlifted pixel or unit-less coordinates)"
                   if stray else "")
                + " -- nothing could have matched, so this is a harness or "
                  "prompt fault, not a prediction failure")
    if ref_has_geometry:
        return None                        # geometry was available and missed
    return (f"no predicted room shares an id with the reference "
            f"(identity={gt.identity!r}) and the {gt.provenance!r} dialect "
            f"states no room extent to match a centroid against, so geometric "
            f"matching is unavailable -- not a prediction failure")
