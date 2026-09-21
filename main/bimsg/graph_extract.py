"""Ask a model for a whole building storey's graph, and parse the answer safely.

Everything the model backends did before this module was per-room single-word
classification: `semantics.predict_label` hands over one room's features and
takes back one word. That measures a classifier, not an extractor. The paper's
central claim is about recovering *structure* -- rooms and the transitions
between them -- so the task a model is set has to be the structure task.

Three things here, and the third is the reason the module exists.

**A prompt per modality.** Text, plan image, occupancy image, and text+image
all ask for the same output, so the only thing that varies across a row of the
Phase C grid is what the model was allowed to look at. The text serialisation
is deliberately built from the same `.plan.json` storey the renderer draws, not
from raw IFC: raw IFC would hand the text condition an entity soup no model
fits in context, and would confound "can it read IFC syntax" with "can it
recover topology".

**A decision about size, stated rather than discovered.** Extraction is
**per storey**, and vertical links are out of scope for the task. A plan image
is already one storey, so per-storey text keeps the two modalities comparable
instead of asking the text condition to do strictly more work. Vertical links
are excluded because the deterministic path already recovers them at
precision 1.000 (measured, `report/results_2026_09_18.tex` §5) -- there is
nothing for a model to win there, and including them would let a model lose
points on the one relation the method never gets wrong. That exclusion is
applied to the *reference* as well, by `GroundTruth.restrict`, so the pairs
leave both sides of the comparison together and the manifest records it. They
are not dropped from the prediction and left in the truth.

**Parsing that charges a non-answer.** This is the general version of the
defect Phase A fixed for the per-room task. `_coerce(text, fallback=hyp)`
returned the rule baseline's answer whenever a model's output did not parse, so
a model that said nothing scored as the rule baseline; on the 18 September 2026
VLM run that fired for 95 of 592 rooms (16.0%), almost exactly the model's own
margin over majority-class guessing. The whole-graph task has strictly more
ways to fail -- no JSON at all, JSON that will not parse, valid JSON of the
wrong shape, an edge naming a room the model never declared -- and every one of
them here produces a counted, named failure. `Extraction.status` is never
absent, nothing is silently repaired, and `to_graph` on a failed extraction
returns an empty graph rather than a partial one dressed up as a prediction.

    from bimsg import graph_extract as X
    ex = X.parse_response(raw_text, hit_cap=False)
    g  = X.to_graph(ex, transform=storey["transform"])
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

try:
    from evaluation import labels as L
except ImportError:                       # pragma: no cover - path bootstrap
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    from evaluation import labels as L

# The relations a model is asked for. `vertically_connected` is absent by
# design -- see the module docstring.
EXTRACT_RELATIONS = ("connected_by_door", "open_passage")

# What the model may say for an edge, mapped onto the pipeline's relation
# names. Kept small and explicit: a model that invents a relation name is
# charged for it rather than having it guessed at.
EDGE_TYPES = {
    "door": "connected_by_door",
    "connected_by_door": "connected_by_door",
    "doorway": "connected_by_door",
    "open": "open_passage",
    "open_passage": "open_passage",
    "opening": "open_passage",
    "passage": "open_passage",
}

# Extraction-level outcomes. Exactly one is always set.
OK = "ok"
NO_JSON = "no_json"                  # nothing that looked like a JSON object
BAD_JSON = "invalid_json"            # found a block, json.loads refused it
BAD_SHAPE = "schema_invalid"         # parsed, but not the requested shape
TRUNCATED = "truncated"              # hit the generation cap; no complete answer
FAILED = (NO_JSON, BAD_JSON, BAD_SHAPE, TRUNCATED)


@dataclass
class Extraction:
    """One model response, parsed. Failures are counted, never repaired.

    `rooms` and `edges` hold only items that survived validation, and the
    `dropped_*` counters say how many did not and why. A caller that reports
    room or edge counts without those counters is reporting a number this
    class deliberately refuses to blend.
    """

    status: str = OK
    detail: str | None = None
    rooms: list = field(default_factory=list)      # {id, label, centroid_px?}
    edges: list = field(default_factory=list)      # {a, b, relation}
    dropped_rooms: dict = field(default_factory=dict)   # reason -> n
    dropped_edges: dict = field(default_factory=dict)   # reason -> n
    raw: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def _drop_room(self, reason: str) -> None:
        self.dropped_rooms[reason] = self.dropped_rooms.get(reason, 0) + 1

    def _drop_edge(self, reason: str) -> None:
        self.dropped_edges[reason] = self.dropped_edges.get(reason, 0) + 1

    def summary(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "rooms": len(self.rooms),
            "edges": len(self.edges),
            "dropped_rooms": dict(self.dropped_rooms),
            "dropped_edges": dict(self.dropped_edges),
            "dropped_rooms_total": sum(self.dropped_rooms.values()),
            "dropped_edges_total": sum(self.dropped_edges.values()),
        }


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

OUTPUT_CONTRACT = """Answer with a single JSON object and nothing else:

{{"rooms": [{{"id": "<id>", "label": "<function>"{extra_room}}}],
 "edges": [{{"a": "<room id>", "b": "<room id>", "type": "door|open"}}]}}

Rules:
- List every room on this storey exactly once.
- "label" must be one of: {taxonomy}
{anchor_rule}
- An edge means a person can walk directly between the two rooms without
  passing through a third. Use "door" when a door separates them and "open"
  when the opening has no door.
- Only use room ids you listed in "rooms".
- Do not include edges between rooms on different storeys.
- Output the JSON object only. No explanation before or after it."""

# A room has to carry something that locates it, or it cannot be compared to
# the reference at all. The two conditions are told to use the coordinate
# system they can actually see: a model reading an image knows pixels and
# nothing else, and a model reading the text is given metres.
_ROOM_EXTRA_PX = ', "centroid_px": [<x>, <y>]'
_ROOM_EXTRA_M = ', "centroid_m": [<x>, <y>]'
_ANCHOR_PX = ('- "centroid_px" is the pixel coordinate of the room\'s '
              'approximate centre in the image you were shown.')
_ANCHOR_M = ('- "centroid_m" is the room\'s approximate centre in metres, in '
             'the same coordinate system as the coordinates you were given.')

SYSTEM_TEXT = """You are a building-information assistant. You recover the \
navigable structure of one storey of a building: which rooms exist, what each \
is for, and which pairs of rooms open directly onto each other."""

SYSTEM_VISION = """You are a building-information assistant. You are shown a \
top-down architectural floor plan of one storey. You recover the navigable \
structure it depicts: which rooms exist, what each is for, and which pairs of \
rooms open directly onto each other.

Walls are drawn as thick lines. A gap in a wall between two rooms is an \
opening you can walk through. Give each room you find an id of your own \
choosing and report the pixel coordinates of its approximate centre, so that \
your rooms can be located on the image you were shown."""

SYSTEM_OCC = """You are a building-information assistant. You are shown a \
binary occupancy map of one storey of a building: white pixels are free floor \
space a person can stand on, black pixels are walls and other obstructions.

Recover the navigable structure. Each enclosed white region is a room. A \
narrow white channel joining two regions is an opening you can walk through. \
Give each room you find an id of your own choosing and report the pixel \
coordinates of its approximate centre. You cannot see furniture or labels, so \
infer each room's function from its size, shape and how it connects to the \
others."""


def storey_text(storey: dict, mask_labels: bool = False,
                max_elements: int = 400) -> str:
    """Serialise one `.plan.json` storey as the text condition's input.

    Built from the same sidecar the renderer draws from, so the text and vision
    conditions describe the same storey and the comparison is about modality
    rather than about two different extractions of the building.

    `mask_labels` removes the architect's room names. That is the condition the
    experiment needs: with names given, a model can copy `IfcSpace.Name` and the
    labelling half of the task is trivially solved from text while the vision
    condition has to infer it, which would make the modality gap an artefact of
    the input rather than a finding.
    """
    lines = []
    name = storey.get("name") or storey.get("gid") or "storey"
    lines.append(f"Storey: {name}")
    el = storey.get("elevation", storey.get("elevation_m"))
    if el is not None:
        lines.append(f"Elevation: {el} m")

    spaces = storey.get("spaces") or storey.get("rooms") or []
    lines.append(f"\nSpaces declared in the file ({len(spaces)}):")
    if not spaces:
        lines.append("  (none -- the file declares no spaces on this storey)")
    for s in spaces:
        sid = s.get("global_id") or s.get("id")
        c = s.get("centroid")
        area = s.get("area_m2", s.get("area"))
        bits = [f"  id={sid}"]
        if not mask_labels:
            nm = s.get("name") or s.get("label")
            if nm:
                bits.append(f'name="{nm}"')
        if area is not None:
            bits.append(f"area={area} m2")
        if c:
            bits.append(f"centre=({c[0]:.2f}, {c[1]:.2f})")
        poly = s.get("polygon")
        if poly:
            bits.append(f"outline={_fmt_poly(poly)}")
        lines.append(" ".join(bits))

    # Doors and walls are what the topology has to be read off. Doors carry an
    # orientation in the sidecar; it is included because the oriented door probe
    # is the deterministic method's strongest single signal and withholding it
    # would make the text condition weaker than the method it is compared to.
    els = storey.get("elements") or []
    doors = [e for e in els if (e.get("ifc_class") or "") == "IfcDoor"]
    walls = [e for e in els if (e.get("ifc_class") or "") == "IfcWall"]
    raw_doors = storey.get("doors") or []
    lines.append(f"\nDoors ({len(doors) or len(raw_doors)}):")
    if raw_doors:
        for d in raw_doors:
            lines.append(f"  id={d.get('id')} at ({d.get('x')}, {d.get('y')}) "
                         f"facing ({d.get('nx')}, {d.get('ny')})")
    else:
        for d in doors[:max_elements]:
            lines.append(f"  id={d.get('global_id')} "
                         f"outline={_fmt_poly(_first_poly(d))}")

    lines.append(f"\nWalls ({len(walls)}):")
    for w in walls[:max_elements]:
        lines.append(f"  {_fmt_poly(_first_poly(w))}")
    if len(walls) > max_elements:
        # Said out loud, because a silently clipped input is an unrecorded
        # change to the condition a model was run under.
        lines.append(f"  ... {len(walls) - max_elements} further walls omitted "
                     f"to fit the context window")
    return "\n".join(lines)


def _first_poly(el: dict):
    pls = el.get("polylines") or []
    return pls[0] if pls else []


def _fmt_poly(poly, limit: int = 12) -> str:
    if not poly:
        return "[]"
    pts = [f"({p[0]:.2f},{p[1]:.2f})" for p in poly[:limit]]
    more = "" if len(poly) <= limit else f" +{len(poly) - limit}"
    return " ".join(pts) + more


def build_prompt(modality: str, storey: dict | None = None,
                 mask_labels: bool = False) -> tuple:
    """(system_prompt, user_prompt, wants_image) for one modality."""
    pixel = modality in ("plan", "occ", "text+plan")
    contract = OUTPUT_CONTRACT.format(
        taxonomy=", ".join(L.CLASSES),
        extra_room=_ROOM_EXTRA_PX if pixel else _ROOM_EXTRA_M,
        anchor_rule=_ANCHOR_PX if pixel else _ANCHOR_M)

    if modality == "text":
        return (SYSTEM_TEXT,
                storey_text(storey, mask_labels) + "\n\n" + contract, False)
    if modality == "plan":
        return (SYSTEM_VISION, contract, True)
    if modality == "occ":
        return (SYSTEM_OCC, contract, True)
    if modality == "text+plan":
        return (SYSTEM_VISION,
                "Facts the building file states about this storey:\n\n"
                + storey_text(storey, mask_labels)
                + "\n\nThe image shows the same storey.\n\n" + contract, True)
    raise ValueError(f"unknown modality {modality!r}; expected one of "
                     f"text, plan, occ, text+plan")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_THINK_END = "</think>"


def _json_block(text: str):
    """The outermost balanced {...} in `text`, or None.

    A regex cannot do this: the response contains nested objects, and a greedy
    or lazy match either swallows trailing prose or stops at the first inner
    brace. Scanning for balance is the only correct reading, and it also tells
    truncation apart from malformation -- an unbalanced block means the
    generation stopped mid-object.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None                            # opened but never closed


def parse_response(text: str, hit_cap: bool = False) -> Extraction:
    """Parse one model response into an `Extraction`.

    `hit_cap` is whether generation stopped because it ran out of tokens. It is
    a caller's fact, not something recoverable from the text, and it is what
    separates "the model had nothing to say" from "the model was cut off" --
    a distinction the per-room task learned to make the expensive way.
    """
    ex = Extraction(raw=text)
    if text is None:
        text = ""
    ex.raw = text

    # A thinking model reasons first. Only what follows the closed trace is an
    # answer; mining the trace for a JSON-looking fragment would score the
    # model's deliberation as its output, the same mistake as reading a class
    # word out of an unfinished thought.
    body = text
    if _THINK_END in text:
        body = text.split(_THINK_END, 1)[1]
    elif "<think>" in text:
        ex.status = TRUNCATED if hit_cap else BAD_SHAPE
        ex.detail = ("generation stopped inside a <think> trace"
                     if hit_cap else
                     "opened a <think> trace and never closed it")
        return ex

    block = _json_block(body)
    if block is None:
        ex.status = TRUNCATED if hit_cap else NO_JSON
        ex.detail = ("no complete JSON object before the token cap" if hit_cap
                     else "response contains no balanced JSON object")
        return ex
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        ex.status = BAD_JSON
        ex.detail = f"{type(e).__name__}: {e}"
        return ex
    if not isinstance(doc, dict):
        ex.status = BAD_SHAPE
        ex.detail = f"top level is {type(doc).__name__}, expected an object"
        return ex

    raw_rooms = doc.get("rooms")
    raw_edges = doc.get("edges")
    if not isinstance(raw_rooms, list):
        ex.status = BAD_SHAPE
        ex.detail = ("'rooms' is missing or not a list"
                     if raw_rooms is None else
                     f"'rooms' is {type(raw_rooms).__name__}, expected a list")
        return ex
    if raw_edges is not None and not isinstance(raw_edges, list):
        ex.status = BAD_SHAPE
        ex.detail = f"'edges' is {type(raw_edges).__name__}, expected a list"
        return ex

    seen = {}
    for r in raw_rooms:
        if not isinstance(r, dict):
            ex._drop_room("not_an_object")
            continue
        rid = r.get("id")
        if rid in (None, ""):
            ex._drop_room("no_id")
            continue
        rid = str(rid)
        if rid in seen:
            # Two rooms under one id: the second is unplaceable, because every
            # edge naming that id is now ambiguous.
            ex._drop_room("duplicate_id")
            continue
        room = {"id": rid, "label": r.get("label")}
        # Pixels and metres are kept apart. Pooling them would let a pixel
        # coordinate be written to a node as though it were a world coordinate,
        # putting every room hundreds of metres from the building and reading
        # as a total recall failure rather than as a unit error.
        for key, dest in (("centroid_px", "centroid_px"),
                          ("centroid_m", "centroid_m"),
                          ("centroid", "centroid_bare")):
            v = r.get(key)
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                try:
                    room[dest] = (float(v[0]), float(v[1]))
                    break
                except (TypeError, ValueError):
                    ex._drop_room("bad_centroid_kept_room")
                    break
        seen[rid] = room
        ex.rooms.append(room)

    for e in (raw_edges or []):
        if not isinstance(e, dict):
            ex._drop_edge("not_an_object")
            continue
        a, b = e.get("a"), e.get("b")
        if a in (None, "") or b in (None, ""):
            ex._drop_edge("missing_endpoint")
            continue
        a, b = str(a), str(b)
        if a == b:
            ex._drop_edge("self_loop")
            continue
        # An edge naming a room the model never declared is a claim about a
        # room that does not exist in its own answer. It is charged here rather
        # than having the room invented for it.
        if a not in seen or b not in seen:
            ex._drop_edge("undeclared_endpoint")
            continue
        rel = EDGE_TYPES.get(str(e.get("type") or "").strip().lower())
        if rel is None:
            ex._drop_edge("unknown_type")
            continue
        ex.edges.append({"a": a, "b": b, "relation": rel})

    if not ex.rooms:
        ex.status = BAD_SHAPE
        ex.detail = "no usable room survived validation"
    return ex


# --------------------------------------------------------------------------
# Lifting a prediction into the space the scorer works in
# --------------------------------------------------------------------------

def px_to_m(px, py, tr: dict) -> tuple:
    """Invert `render_floorplan.view_transform`: px = s*x + tx, py = -s*y + ty."""
    s = tr["px_per_m"]
    return ((px - tr["tx"]) / s, (tr["ty"] - py) / s)


def to_graph(ex: Extraction, transform: dict | None = None,
             elevation: float | None = None, storey_gid: str | None = None):
    """Build a graph in the pipeline's own shape, so the existing scorers apply.

    This is the whole reason the evaluation contract was worth building. The
    scorer takes a graph and a reference and never asks how the graph was made,
    so a model's answer, once it is a graph, is scored by exactly the code that
    scores the deterministic method -- no second metric, no second definition of
    precision, nothing to drift apart.

    A failed extraction yields an **empty** graph. That is the honest
    representation: the model produced no usable prediction, and an empty graph
    scores recall 0 with no precision denominator, which is what a non-answer
    deserves. Returning the rooms that happened to survive a schema violation
    would be the same species of error as substituting the rule prior.
    """
    import networkx as nx

    g = nx.MultiDiGraph()
    g.graph["prediction"] = True
    g.graph["extraction_status"] = ex.status
    g.graph["extraction_detail"] = ex.detail
    if not ex.ok:
        return g

    if storey_gid:
        g.add_node(storey_gid, layer="storey", source="predicted",
                   elevation=elevation)
    for r in ex.rooms:
        attrs = {"layer": "space", "source": "predicted",
                 "room_number": r["id"],
                 # `predicted_label` and not `label`: score_labels reads only
                 # the former precisely so that a prediction can never be
                 # scored against itself.
                 "predicted_label": r.get("label")}
        if elevation is not None:
            attrs["elevation"] = elevation
        px = r.get("centroid_px")
        m = r.get("centroid_m")
        # `centroid` on a node means world metres -- that is what
        # `matching._bbox_score` compares against a reference bbox -- so only
        # a value actually in metres may be written there.
        if m is not None:
            attrs["centroid"] = [round(m[0], 3), round(m[1], 3)]
        elif px is not None and transform:
            x, y = px_to_m(px[0], px[1], transform)
            attrs["centroid"] = [round(x, 3), round(y, 3)]
        elif px is not None:
            # Pixels with no transform to lift them cannot be compared to a
            # reference in metres. Kept on the node under a different name so
            # nothing downstream mistakes them for world coordinates.
            attrs["centroid_px"] = [px[0], px[1]]
        elif r.get("centroid_bare") is not None:
            # The model used the bare key the contract did not ask for, so
            # which system it is in is unknown. Recorded, never guessed.
            attrs["centroid_unknown_units"] = list(r["centroid_bare"])
        g.add_node(r["id"], **attrs)
        if storey_gid:
            g.add_edge(storey_gid, r["id"], key=f"contains:{r['id']}",
                       relation="contains")

    for i, e in enumerate(ex.edges):
        pid = f"pred-{i:04d}"
        for u, v in ((e["a"], e["b"]), (e["b"], e["a"])):
            g.add_edge(u, v, key=pid, edge_id=pid, portal_id=pid,
                       relation=e["relation"], provenance="model")
    return g
