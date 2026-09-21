"""Downstream task 2 -- spatial question answering over the hybrid graph.

The design here follows the two findings that matter from the literature:
SayPlan's collapse/expand interface (never show the model the whole building,
show it one storey and expand on demand) and the Graph-RAG pattern (answer from
retrieved facts, never from parametric memory).

The retrieval layer is deterministic and fully testable on its own. An LLM is
optional and pluggable: `answer()` resolves questions through graph queries and
returns grounded facts; `build_prompt()` packages those facts for any
open-weights chat model without this module depending on one.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Optional, Protocol

import networkx as nx


class LLMBackend(Protocol):
    """Any open-weights chat model can be dropped in behind this."""

    def generate(self, prompt: str) -> str: ...


# --------------------------------------------------------------------------
# Retrieval primitives  (each returns grounded facts, never prose)
# --------------------------------------------------------------------------

class BuildingIndex:
    def __init__(self, G: nx.MultiDiGraph):
        self.G = G
        self.spaces = {n: d for n, d in G.nodes(data=True) if d.get("layer") == "space"}
        self.storeys = {n: d for n, d in G.nodes(data=True) if d.get("layer") == "storey"}
        self.elements = {n: d for n, d in G.nodes(data=True) if d.get("layer") == "element"}

        self.storey_of = {}
        self.rooms_in_storey = defaultdict(list)
        self.elements_in_room = defaultdict(list)
        for u, v, d in G.edges(data=True):
            if d.get("relation") != "contains":
                continue
            if u in self.storeys and v in self.spaces:
                self.storey_of[v] = u
                self.rooms_in_storey[u].append(v)
            elif u in self.spaces and v in self.elements:
                self.elements_in_room[u].append(v)

    # -- lookups ----------------------------------------------------------
    def find_rooms(self, text: str) -> list:
        """Resolve a room reference to node ids.

        Tried in order: (1) the taxonomy class, matched against either the
        room's stated label or -- crucially -- its `predicted_label` from the
        semantic-injection stage, so a functional query like "kitchen"
        resolves rooms this pipeline recovered geometrically and that were
        never given a name by anyone; (2) literal substring match, for named
        rooms ("Conference Room A") that a taxonomy class can't capture; (3)
        token overlap, as a last resort.
        """
        t = text.lower().strip()
        from . import semantics as S
        cls = S.canonical(t)
        if cls:
            hits = [n for n, d in self.spaces.items()
                    if S.canonical(d.get("label")) == cls
                    or d.get("predicted_label") == cls]
            if hits:
                return hits
        hits = [n for n, d in self.spaces.items()
                if t in (d.get("label") or "").lower()]
        if not hits:                      # try token overlap
            toks = set(t.split())
            hits = [n for n, d in self.spaces.items()
                    if toks & set((d.get("label") or "").lower().split())]
        return hits

    def find_elements(self, text: str) -> list:
        """Match by IFC class or name, tolerating plurals ('doors' -> IfcDoor)."""
        t = text.lower().strip()
        forms = {t, t.rstrip("s"), t + "s"}
        out = []
        for n, d in self.elements.items():
            cls = d.get("ifc_type", "").replace("Ifc", "").lower()
            label = (d.get("label") or "").lower()
            if cls in forms or any(f and f in label for f in (t, t.rstrip("s"))):
                out.append(n)
        return out

    def room_label(self, n: str) -> str:
        return self.spaces.get(n, {}).get("label") or n

    def storey_label(self, n: str) -> str:
        return self.storeys.get(n, {}).get("label") or n

    def contents(self, room: str) -> Counter:
        return Counter(self.elements[e]["ifc_type"].replace("Ifc", "")
                       for e in self.elements_in_room.get(room, []))

    def neighbours(self, room: str, relation: str = "connected_by_door") -> list:
        out = []
        for _, v, d in self.G.out_edges(room, data=True):
            if d.get("relation") == relation and v in self.spaces:
                out.append(v)
        return sorted(set(out))

    def room_of_element(self, el: str) -> Optional[str]:
        for u, v, d in self.G.in_edges(el, data=True):
            if d.get("relation") == "contains" and u in self.spaces:
                return u
        return None

    # -- summaries --------------------------------------------------------
    def building_summary(self) -> dict:
        return {
            "storeys": len(self.storeys),
            "rooms": len(self.spaces),
            "elements": len(self.elements),
            "room_types": Counter(
                (d.get("label") or "?") for d in self.spaces.values()).most_common(12),
            "total_floor_area_m2": round(
                sum(d.get("area") or 0 for d in self.spaces.values()), 1),
        }

    def storey_view(self, storey: str) -> dict:
        """One storey, collapsed -- the unit SayPlan expands at a time."""
        rooms = self.rooms_in_storey.get(storey, [])
        return {
            "storey": self.storey_label(storey),
            "elevation_m": self.storeys.get(storey, {}).get("elevation"),
            "rooms": [
                {
                    "id": r,
                    "name": self.room_label(r),
                    "area_m2": self.spaces[r].get("area"),
                    "source": self.spaces[r].get("source"),
                    "connects_to": [self.room_label(x) for x in self.neighbours(r)],
                    "contains": dict(self.contents(r).most_common(6)),
                }
                for r in rooms
            ],
        }


# --------------------------------------------------------------------------
# Question answering
# --------------------------------------------------------------------------

_PATTERNS = [
    ("count_rooms",   r"how many (rooms|spaces)"),
    ("count_type",    r"how many (\w+)s?\b"),
    ("count_storeys", r"how many (storeys|floors|levels)"),
    ("what_in",       r"what(?:'s| is)? (?:in|inside) (?:the )?(.+?)\??$"),
    ("where_is",      r"where(?:'s| is)? (?:the )?(.+?)\??$"),
    ("adjacent",      r"(?:what|which) rooms? (?:are )?(?:next to|adjacent to|near) (?:the )?(.+?)\??$"),
    ("path",          r"(?:how do i get|path|route|navigate) from (?:the )?(.+?) to (?:the )?(.+?)\??$"),
    ("largest",       r"(?:what|which) is the (largest|biggest|smallest) room"),
    ("list_rooms",    r"(?:list|what are) the rooms"),
]


def answer(G: nx.MultiDiGraph, question: str,
           llm: Optional[LLMBackend] = None) -> dict:
    """Answer a spatial question from the graph.

    Returns both a natural-language `answer` and the `facts` it was derived
    from, so every response is auditable against the model.
    """
    idx = BuildingIndex(G)
    q = question.lower().strip()

    for name, pat in _PATTERNS:
        m = re.search(pat, q)
        if not m:
            continue
        res = _dispatch(name, m, idx, G)
        if res is not None:
            if llm is not None:
                res["answer"] = llm.generate(
                    build_prompt(question, res["facts"])).strip()
            return res

    # Unmatched: hand the LLM a storey-level view rather than guessing.
    facts = idx.building_summary()
    out = {"question": question, "intent": "general", "facts": facts,
           "answer": _fmt_summary(facts)}
    if llm is not None:
        out["answer"] = llm.generate(build_prompt(question, facts)).strip()
    return out


def _dispatch(name, m, idx, G):
    if name == "count_storeys":
        n = len(idx.storeys)
        return _mk("count_storeys", {"storeys": n,
                                     "names": [idx.storey_label(s) for s in idx.storeys]},
                   f"The building has {n} storeys.")

    if name == "count_rooms":
        n = len(idx.spaces)
        by = Counter(d.get("source") for d in idx.spaces.values())
        return _mk("count_rooms", {"rooms": n, "by_source": dict(by)},
                   f"There are {n} rooms "
                   f"({by.get('ifc', 0)} from the IFC spatial tree, "
                   f"{by.get('inferred', 0)} recovered geometrically).")

    if name == "count_type":
        word = m.group(1)
        els = idx.find_elements(word)
        if not els:
            return None
        per = Counter()
        for e in els:
            r = idx.room_of_element(e)
            per[idx.room_label(r) if r else "unassigned"] += 1
        plural = word if word.endswith("s") else word + "s"
        return _mk("count_type", {"type": word.rstrip("s"), "count": len(els),
                                  "per_room": dict(per.most_common(10))},
                   f"There are {len(els)} {plural} in the building.")

    if name == "what_in":
        rooms = idx.find_rooms(m.group(1))
        if not rooms:
            return None
        r = rooms[0]
        c = idx.contents(r)
        return _mk("what_in",
                   {"room": idx.room_label(r), "contents": dict(c),
                    "area_m2": idx.spaces[r].get("area")},
                   f"The {idx.room_label(r)} contains: "
                   f"{', '.join(f'{v} {k.lower()}' for k, v in c.most_common()) or 'nothing recorded'}.")

    if name == "where_is":
        target = m.group(1)
        rooms = idx.find_rooms(target)
        if rooms:
            r = rooms[0]
            st = idx.storey_of.get(r)
            return _mk("where_is",
                       {"room": idx.room_label(r),
                        "storey": idx.storey_label(st) if st else None,
                        "centroid": idx.spaces[r].get("centroid")},
                       f"The {idx.room_label(r)} is on "
                       f"{idx.storey_label(st) if st else 'an unknown storey'}.")
        els = idx.find_elements(target)
        if els:
            locs = Counter()
            for e in els[:200]:
                r = idx.room_of_element(e)
                locs[idx.room_label(r) if r else "unassigned"] += 1
            return _mk("where_is", {"element": target, "locations": dict(locs.most_common(8))},
                       f"{target} appears in: "
                       f"{', '.join(f'{k} ({v})' for k, v in locs.most_common(5))}.")
        return None

    if name == "adjacent":
        rooms = idx.find_rooms(m.group(1))
        if not rooms:
            return None
        r = rooms[0]
        doors = [idx.room_label(x) for x in idx.neighbours(r, "connected_by_door")]
        adj = [idx.room_label(x) for x in idx.neighbours(r, "adjacent_to")]
        return _mk("adjacent",
                   {"room": idx.room_label(r), "connected_by_door": doors,
                    "shares_wall_with": adj},
                   f"The {idx.room_label(r)} opens onto "
                   f"{', '.join(doors) or 'nothing'}; it shares a wall with "
                   f"{', '.join(adj) or 'nothing'}.")

    if name == "path":
        from . import navigation
        a, b = idx.find_rooms(m.group(1)), idx.find_rooms(m.group(2))
        if not a or not b:
            return None
        p = navigation.plan(G, a[0], b[0])
        if p is None:
            return _mk("path", {"from": idx.room_label(a[0]), "to": idx.room_label(b[0]),
                                "reachable": False},
                       "No route exists between those rooms for this robot.")
        return _mk("path", p,
                   f"Route: {' -> '.join(p['labels'])} "
                   f"({p['length_m']} m"
                   f"{', via stairs' if p['uses_stairs'] else ''}).")

    if name == "largest":
        want_max = m.group(1) in ("largest", "biggest")
        # Exterior circulation is a recovered outdoor deck, not a room someone
        # would mean by "the largest room".
        # Circulation, decks and open floor plates are spaces but not what
        # anyone means by "the largest room".
        skip = {"circulation", "exterior circulation", "open floor plate",
                "open area"}
        cand = [(d.get("area") or 0, n) for n, d in idx.spaces.items()
                if d.get("area") and (d.get("label") or "").lower() not in skip]
        if not cand:
            return None
        area, r = (max if want_max else min)(cand)
        return _mk("largest", {"room": idx.room_label(r), "area_m2": area},
                   f"The {'largest' if want_max else 'smallest'} room is "
                   f"{idx.room_label(r)} at {area} m².")

    if name == "list_rooms":
        c = Counter(idx.room_label(n) for n in idx.spaces)
        return _mk("list_rooms", {"rooms": dict(c.most_common())},
                   "Rooms: " + ", ".join(f"{k} x{v}" if v > 1 else k
                                         for k, v in c.most_common(20)) + ".")
    return None


def _mk(intent, facts, text):
    return {"intent": intent, "facts": facts, "answer": text}


def _fmt_summary(f) -> str:
    return (f"The building has {f['storeys']} storeys, {f['rooms']} rooms and "
            f"{f['elements']} catalogued elements, totalling "
            f"{f['total_floor_area_m2']} m² of floor area.")


# --------------------------------------------------------------------------
# LLM packaging
# --------------------------------------------------------------------------

PROMPT = """You are answering questions about a building using ONLY the facts below.
These facts were extracted deterministically from an IFC building model.
If the facts do not answer the question, say so. Never invent rooms or objects.

FACTS:
{facts}

QUESTION: {question}
ANSWER:"""


def build_prompt(question: str, facts: dict) -> str:
    return PROMPT.format(facts=json.dumps(facts, indent=2, default=str),
                         question=question)


def context_for_llm(G: nx.MultiDiGraph, max_storeys: int = 3) -> str:
    """A collapsed, token-bounded view of the whole building.

    Following SayPlan: expose storeys and room connectivity, not geometry, so a
    building-scale graph stays inside a context window.
    """
    idx = BuildingIndex(G)
    lines = [f"BUILDING: {len(idx.storeys)} storeys, {len(idx.spaces)} rooms"]
    for s in sorted(idx.storeys, key=lambda x: idx.storeys[x].get("elevation") or 0)[:max_storeys]:
        v = idx.storey_view(s)
        lines.append(f"\nSTOREY {v['storey']} (elev {v['elevation_m']} m):")
        for r in v["rooms"]:
            conn = ", ".join(r["connects_to"][:5]) or "-"
            lines.append(f"  - {r['name']} ({r['area_m2']} m2) -> {conn}")
    return "\n".join(lines)
