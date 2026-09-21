"""One ground-truth record, read from any of the three dialects we produce.

Three things write ground truth and none of them agreed on a shape:

* the **generator** (`generate_building.py`) knows the truth exactly, names
  rooms by a synthetic id it also writes into `IfcSpace.Name`, and lists both
  connected and adjacent-but-separated pairs;
* the **deriver** (`derive_real_gt.py`) flood-fills real geometry, names rooms
  by IFC GlobalId, and abstains on pairs it cannot decide;
* the **annotator** (`compose.connectivity_gt`) records human verdicts, names
  rooms by GlobalId, and -- until this contract existed -- threw its negative
  verdicts away during composition, leaving an export that the evaluator it was
  written for could not read at all.

The differences that matter are not cosmetic and are kept, not flattened:

**Identity.** How a predicted room is joined to a reference room differs by
dialect, so the record says which (`identity`). Guessing it is how a GUID-keyed
human export silently scored zero against a name-keyed evaluator.

**Scope.** A pair can be positive, negative, *unknown*, or never examined, and
those are four different things. An unreviewed pair is not a negative: the
annotator saw a list of proposed links, so the negatives cover proposed links
only, and a pair nobody proposed was never judged. `decided()` is the set a
precision figure may be computed over; everything else is reported apart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

GT_SCHEMA_VERSION = 1

# How a reference room id is found on a predicted graph node.
IDENTITY_NODE_ID = "node_id"          # rid is the graph node id (IFC GlobalId)
IDENTITY_ROOM_NUMBER = "room_number"  # rid was written into IfcSpace.Name


def pair(a: str, b: str) -> frozenset:
    return frozenset((a, b))


@dataclass
class Room:
    rid: str
    label: Optional[str] = None
    storey: Optional[str] = None
    area: Optional[float] = None
    # Axis-aligned extent, where the dialect states one. The generator does;
    # neither the deriver nor the annotator does, and inventing one from a
    # centroid would make geometric matching look better than it is.
    bbox: Optional[tuple] = None
    storey_index: Optional[int] = None


@dataclass
class GroundTruth:
    building: str
    provenance: str                       # generator | derived | annotated
    identity: str
    rooms: dict = field(default_factory=dict)          # rid -> Room
    positives: dict = field(default_factory=dict)      # pair -> declared type
    negatives: set = field(default_factory=set)
    unknown: set = field(default_factory=set)
    # Regions a reviewer looked at and said were not rooms. A predicted room
    # that matches nothing is otherwise ambiguous: it may be a confirmed false
    # positive, or a real room nobody has judged yet. Only the first is a
    # method's fault.
    rooms_rejected: set = field(default_factory=set)
    usable: bool = True
    excluded_because: Optional[str] = None
    scope: dict = field(default_factory=dict)
    schema: int = GT_SCHEMA_VERSION
    path: Optional[str] = None

    def decided(self) -> set:
        """Pairs this reference actually ruled on, in either direction."""
        return set(self.positives) | self.negatives

    def in_scope(self) -> set:
        """Pairs the reference examined, including the ones it abstained on."""
        return self.decided() | self.unknown

    def restrict(self, rids, reason: str) -> "GroundTruth":
        """A view of this reference covering only `rids`, saying what it lost.

        Needed because a model is asked for one storey at a time (see
        `bimsg.graph_extract`), and a reference covering the whole building
        would charge it with every cross-storey link it was told not to emit.
        Restricting the prediction's scope without restricting the reference's
        is how a task definition turns into a measured failure.

        A pair with one endpoint inside `rids` and one outside is neither kept
        nor forgotten: it is counted in `scope["dropped_straddling"]`. Those
        pairs are the real boundary of the restriction, and a restriction that
        does not report them is indistinguishable from one that lost pairs by
        accident.
        """
        keep = set(rids)
        kept_rooms = {r: v for r, v in self.rooms.items() if r in keep}

        def split(pairs):
            inside, straddling = [], 0
            for p in pairs:
                a, b = tuple(p)
                n = (a in keep) + (b in keep)
                if n == 2:
                    inside.append(p)
                elif n == 1:
                    straddling += 1
            return inside, straddling

        pos, s1 = split(self.positives)
        neg, s2 = split(self.negatives)
        unk, s3 = split(self.unknown)
        scope = dict(self.scope)
        scope["restricted_to"] = reason
        scope["rooms_before"] = len(self.rooms)
        scope["rooms_after"] = len(kept_rooms)
        scope["dropped_straddling"] = s1 + s2 + s3
        scope["dropped_outside"] = (
            (len(self.positives) - len(pos) - s1)
            + (len(self.negatives) - len(neg) - s2)
            + (len(self.unknown) - len(unk) - s3))
        return GroundTruth(
            building=self.building, provenance=self.provenance,
            identity=self.identity, rooms=kept_rooms,
            positives={p: self.positives[p] for p in pos},
            negatives=set(neg), unknown=set(unk),
            rooms_rejected={r for r in self.rooms_rejected if r in keep},
            usable=self.usable, excluded_because=self.excluded_because,
            scope=scope, schema=self.schema, path=self.path)

    def rooms_on_storey(self, storey) -> set:
        """Room ids the reference places on one storey, by either field.

        The generator writes an integer storey index, the deriver and the
        annotator write a name. Both are compared as strings so a caller does
        not have to know which dialect it holds.
        """
        want = str(storey)
        return {rid for rid, r in self.rooms.items()
                if str(r.storey) == want or str(r.storey_index) == want}

    def summary(self) -> dict:
        return {
            "building": self.building, "provenance": self.provenance,
            "identity": self.identity, "usable": self.usable,
            "excluded_because": self.excluded_because,
            "rooms": len(self.rooms),
            "rooms_rejected": len(self.rooms_rejected),
            "positives": len(self.positives),
            "negatives": len(self.negatives), "unknown": len(self.unknown),
            "scope": self.scope,
        }


# --------------------------------------------------------------------------
# Dialects
# --------------------------------------------------------------------------

def detect_dialect(d: dict) -> str:
    src = str(d.get("source") or "")
    if src.startswith("derived"):
        return "derived"
    if src == "annotated":
        return "annotated"
    if "seed" in d or any("x0" in r for r in d.get("rooms", [])[:1]):
        return "generator"
    raise ValueError("cannot identify ground-truth dialect; no 'source' field "
                     "and no generator room geometry")


def _pairs(items) -> set:
    return {pair(e["a"], e["b"]) for e in (items or [])}


def load_gt(path: str) -> GroundTruth:
    with open(path) as fh:
        d = json.load(fh)
    dialect = detect_dialect(d)
    building = d.get("building") or os.path.basename(path).split(".")[0]

    if dialect == "generator":
        rooms = {
            r["rid"]: Room(rid=r["rid"], label=r.get("name"),
                           storey=str(r.get("storey")),
                           storey_index=r.get("storey"),
                           bbox=(r["x0"], r["y0"], r["x1"], r["y1"]))
            for r in d["rooms"]
        }
        return GroundTruth(
            building=building, provenance="generator",
            identity=IDENTITY_ROOM_NUMBER, rooms=rooms,
            positives={pair(e["a"], e["b"]): e.get("type") for e in d["edges"]},
            negatives=_pairs(d.get("adjacent_not_connected")),
            unknown=set(), path=path,
            scope={"negatives": "every adjacent pair the generator did not join",
                   "coverage": "complete: the generator knows every pair"},
        )

    if dialect == "derived":
        rooms = {r["rid"]: Room(rid=r["rid"],
                                label=r.get("long_name") or r.get("name"),
                                storey=str(r.get("storey")),
                                area=r.get("area"))
                 for r in d["rooms"]}
        return GroundTruth(
            building=building, provenance="derived",
            identity=IDENTITY_NODE_ID, rooms=rooms,
            positives={pair(e["a"], e["b"]): None for e in d["edges"]},
            negatives=_pairs(d.get("adjacent_not_connected")),
            unknown=_pairs(d.get("unknown")),
            usable=bool(d.get("derivable", True)),
            excluded_because=d.get("excluded_because"), path=path,
            scope={"negatives": "candidate-adjacent pairs the flood separated",
                   "coverage": f"pairs within {d.get('params', {}).get('adj_tol')} m only",
                   "caveat": "a second geometric reading, not a human judgement"},
        )

    # annotated
    rooms = {r["rid"]: Room(rid=r["rid"], label=r.get("label"),
                            storey=r.get("storey"), area=r.get("area"))
             for r in d["rooms"]}
    # Held-out items are uncertain or rest on a room that was never confirmed.
    # Either way they are not negatives, and folding them in would manufacture
    # true negatives out of an annotator saying "I don't know".
    held = {pair(h["a"], h["b"]) for h in (d.get("held_out") or [])
            if h.get("a") and h.get("b")}
    return GroundTruth(
        building=building, provenance="annotated",
        identity=IDENTITY_NODE_ID, rooms=rooms,
        positives={pair(e["a"], e["b"]): e.get("type") for e in d["edges"]},
        negatives=_pairs(d.get("adjacent_not_connected")),
        unknown=held | _pairs(d.get("unknown")),
        rooms_rejected=set(d.get("rooms_rejected") or []),
        usable=True, path=path,
        scope=d.get("review_scope") or {
            "negatives": "links the pipeline proposed and a human rejected",
            "coverage": "proposed links only; an unproposed pair was never judged",
        },
    )
