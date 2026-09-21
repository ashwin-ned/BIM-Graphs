"""Every audit finding, as a test that runs.

The 13 September audit demonstrated its findings with throwaway fixtures in
`docs/audit_2026_09_13/probes.py`, which printed observations and asserted
nothing. This file is those reproductions turned into checks, which is the M0
acceptance criterion: a defect that has been fixed must stay fixed, and a defect
that has not been fixed yet must say so out loud rather than being forgotten.

Two kinds of case:

    fixed(...)     the repair holds. Each one is paired with a `known_bad`
                   block that reproduces the *old* behaviour inline and asserts
                   the same check rejects it -- otherwise a check that has
                   quietly stopped discriminating still passes, which is how a
                   green suite comes to mean nothing.

    open_defect(...)  the finding is real and not repaired yet. The test asserts
                   the defect is *still present*, and fails when someone fixes
                   it, so the fix arrives with a demand to flip the case rather
                   than leaving a stale expectation behind. These are the
                   geometry findings (A01, A03, A04, A05), which belong to M1.

    python tests/test_m0_regressions.py
    python tests/test_m0_regressions.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "main"), str(ROOT / "annotator")]

import networkx as nx
import numpy as np
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize, unary_union

from bimsg import graph as G, navigation as N, parser as P, semantics as S, topology as T
import compose as C
import derive_real_gt as DR
import eval_connectivity as EC
import eval_semantics as ES
import evaluate as EA
from evaluation import RunManifest, load_gt, match_rooms, score_connectivity
from evaluation import verdicts as V

VERBOSE = "-v" in sys.argv
RESULTS = []


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def _run(kind, ident, title, fn):
    try:
        detail = fn()
        RESULTS.append((kind, ident, title, True, detail or ""))
    except Exception as exc:                              # noqa: BLE001
        tb = traceback.format_exc() if VERBOSE else f"{type(exc).__name__}: {exc}"
        RESULTS.append((kind, ident, title, False, tb))


def fixed(ident, title):
    """A repaired finding. Must hold."""
    def deco(fn):
        _run("fixed", ident, title, fn)
        return fn
    return deco


def open_defect(ident, title):
    """A finding not repaired yet. Asserts the defect is still there."""
    def deco(fn):
        _run("open", ident, title, fn)
        return fn
    return deco


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")
    return f"{what} = {got!r}"


def truthy(cond, what):
    if not cond:
        raise AssertionError(f"{what}: expected true")
    return what


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def space(gid, poly, storey="s0", label="room", z=0):
    return P.Space(gid, gid, label, z, z + 3, poly, storey)


def product(gid, poly, kind="IfcDoor", z0=0, z1=2.1):
    c = poly.centroid
    return P.Product(gid, kind, gid, "", z0, z1, (c.x, c.y, (z0 + z1) / 2),
                     poly, np.eye(4), "s0")


def model(spaces, products, storeys=None):
    return P.ParsedModel("probe.ifc", "IFC4", "probe", "probe", 1,
                         storeys or [P.Storey("s0", "Ground", 0)], spaces, products)


def two_rooms_two_doors():
    """One wide door and one narrow door between the same two rooms."""
    a = space("a", box(-3, -3, 3, -.1))
    b = space("b", box(-3, .1, 3, 3))
    wide = product("wide", box(-2, -.1, -.8, .1))
    narrow = product("narrow", box(1, -.1, 1.4, .1))
    return a, b, wide, narrow


PLAN = {"model": "probe", "storeys": [{
    "gid": "s0", "name": "Ground", "elevation": 0,
    "rooms": [{"id": r, "label": "room", "source": "ifc", "area": 9,
               "centroid": [i * 3, 0]} for i, r in enumerate(["a", "b", "c"])],
    "edges": [{"a": "a", "b": "b", "type": "connected_by_door"},
              {"a": "b", "b": "c", "type": "connected_by_door"}]}],
    "vertical": []}


# ==========================================================================
# A02 -- parallel portals survive recovery, routing and a save/load round trip
# ==========================================================================

@fixed("A02", "two doors between one pair of rooms both survive")
def _a02_stored():
    a, b, wide, narrow = two_rooms_two_doors()
    g = G.build(model([a, b], [wide, narrow]), infer_missing_rooms=False)
    stored = g.get_edge_data("a", "b")
    doors = {e.get("door") for e in stored.values()
             if e.get("relation") == "connected_by_door"}
    eq(doors, {"wide", "narrow"}, "doors stored between a and b")
    ids = {k for k in stored}
    truthy(len(ids) >= 2, "each portal has its own edge key")

    # known-bad: the old builder keyed on the relation name, so the second door
    # replaced the first and this same check would have caught it.
    old = nx.MultiDiGraph()
    old.add_node("a", layer="space"); old.add_node("b", layer="space")
    for d in ("wide", "narrow"):
        old.add_edge("a", "b", key="connected_by_door",
                     relation="connected_by_door", door=d)
    old_doors = {e.get("door") for e in old.get_edge_data("a", "b").values()}
    truthy(old_doors != {"wide", "narrow"},
           "known-bad: relation-keyed graph keeps only one of the two doors")
    return f"kept {sorted(doors)}; relation-keyed control kept {sorted(old_doors)}"


@fixed("A02", "a wheeled route exists whatever order the products arrive in")
def _a02_order():
    a, b, wide, narrow = two_rooms_two_doors()
    fwd = G.build(model([a, b], [wide, narrow]), infer_missing_rooms=False)
    rev = G.build(model([a, b], [narrow, wide]), infer_missing_rooms=False)
    f = N.plan(fwd, "a", "b", profile="wheeled") is not None
    r = N.plan(rev, "a", "b", profile="wheeled") is not None
    eq((f, r), (True, True), "wheeled route in both product orders")
    def door_keys(g):
        return sorted(k for k, e in g.get_edge_data("a", "b").items()
                      if e.get("relation") == "connected_by_door")
    kf, kr = door_keys(fwd), door_keys(rev)
    eq(kf, kr, "portal ids do not depend on input order")
    # Both portals must still be there. Sorting the candidates alone would make
    # the route reproducible while quietly keeping only the widest door, which
    # passes an order test for the wrong reason. Counting every key between the
    # two rooms would not catch it either -- the adjacency edge makes up the
    # number -- so only door portals are counted.
    eq(len(kf), 2, "both door portals are present, not just the passable one")
    return f"route found in both orders; identical portal ids {kf}"


@fixed("A02", "parallel edges survive save and reload")
def _a02_roundtrip():
    g = nx.MultiDiGraph()
    g.add_node("a", layer="space"); g.add_node("b", layer="space")
    for d in ("d1", "d2"):
        g.add_edge("a", "b", key=f"door:{d}", edge_id=f"door:{d}",
                   relation="connected_by_door", door=d)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "g.json")
        G.save(g, p)
        back = G.load(p)
        eq(back.number_of_edges(), g.number_of_edges(), "edges after round trip")
        eq({e["door"] for _, _, e in back.edges(data=True)}, {"d1", "d2"},
           "door identities after round trip")

        # known-bad: a file written without keys must not lose the edges it
        # does contain -- the loader gives each one a distinct key instead of
        # merging them back onto the relation name.
        raw = json.load(open(p))
        for e in raw["edges"]:
            e.pop("key", None)
        legacy = os.path.join(td, "legacy.json")
        json.dump(raw, open(legacy, "w"))
        old = G.load(legacy)
        eq(old.number_of_edges(), 2, "keyless legacy file still loads both edges")
    return "2 parallel portals survive save/load, and legacy keyless files too"


# ==========================================================================
# A06/A07/A08 -- the annotation contract
# ==========================================================================

@fixed("A06", "an unsure room stays out of ground truth and is held out")
def _a06():
    anno = {"model": "probe", "annotator": "t",
            "rooms": {"a": {"verdict": "unsure"}, "b": {"verdict": "real"},
                      "c": {"verdict": "real"}},
            "edges": {"a|b": {"verdict": "passable"},
                      "b|c": {"verdict": "passable"}}}
    g = C.compose(PLAN, anno)
    ids = {n["id"] for n in g["nodes"]}
    truthy("a" not in ids, "unsure room is not a node")
    truthy(any(h.get("room") == "a" for h in g["held_out"]),
           "unsure room is recorded in held_out")
    truthy(all("a" not in (e["a"], e["b"]) for e in g["edges"]
               if e["relation"] != "contains"),
           "no link rests on the unsure room")
    truthy(not g["complete"] or True, "coverage flag computed")
    return f"held_out={len(g['held_out'])}, nodes={len(ids)}"


@fixed("A08", "a rejection survives composition as a negative")
def _a08_negatives():
    anno = {"model": "probe", "annotator": "t",
            "rooms": {"a": {"verdict": "real"}, "b": {"verdict": "real"},
                      "c": {"verdict": "not_a_room"}},
            "edges": {"a|b": {"verdict": "passable"},
                      "b|c": {"verdict": "not_passable"}}}
    g = C.compose(PLAN, anno)
    gt = C.connectivity_gt(g)
    eq(gt["rooms_rejected"], ["c"], "rooms the reviewer rejected")
    # b|c cannot be a scorable negative: c is not a confirmed room, so the pair
    # is held out rather than counted against a method.
    truthy(any(h.get("a") == "b" and h.get("b") == "c" for h in gt["held_out"]),
           "a negative on an unconfirmed room is held out, not counted")

    anno["rooms"]["c"]["verdict"] = "real"
    gt2 = C.connectivity_gt(C.compose(PLAN, anno))
    eq(gt2["adjacent_not_connected"], [{"a": "b", "b": "c"}],
       "rejected link between two confirmed rooms")
    truthy("review_scope" in gt2, "the export states its review scope")
    truthy(gt2["review_scope"]["unreviewed_is_not_negative"] is True,
           "the export says silence is not a negative")
    return "negatives, rejected rooms and scope all present"


@fixed("A08", "human ground truth loads in the evaluator without a KeyError")
def _a08_roundtrip():
    anno = {"model": "probe", "annotator": "t",
            "rooms": {r: {"verdict": "real"} for r in "abc"},
            "edges": {"a|b": {"verdict": "passable"},
                      "b|c": {"verdict": "not_passable"}}}
    gt_dict = C.connectivity_gt(C.compose(PLAN, anno))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "probe__t.gt.json")
        json.dump(gt_dict, open(p, "w"))
        gt = load_gt(p)                    # raised KeyError before the repair
        eq(gt.provenance, "annotated", "dialect detected")
        eq(len(gt.positives), 1, "positive pairs")
        eq(len(gt.negatives), 1, "negative pairs")
        eq(gt.identity, "node_id", "rooms joined by GlobalId, not by name")

        # known-bad: a positives-only export (what the composer used to write)
        # must load as having *no* negatives rather than inventing them.
        stripped = dict(gt_dict)
        stripped.pop("adjacent_not_connected")
        q = os.path.join(td, "old__t.gt.json")
        json.dump(stripped, open(q, "w"))
        eq(len(load_gt(q).negatives), 0,
           "known-bad: a positives-only export yields zero negatives")
    return "annotated export reads as 1 positive, 1 negative"


@fixed("A07", "the current verdict vocabulary actually scores")
def _a07():
    anno = {"model": "probe", "annotator": "t",
            "rooms": {"a": {"verdict": "real"}, "b": {"verdict": "real"},
                      "c": {"verdict": "not_a_room"}},
            "edges": {"a|b": {"verdict": "passable"},
                      "b|c": {"verdict": "not_passable"}},
            "vertical": {}, "added_edges": [], "missing_rooms": []}
    r = EA.score_one(anno, plan=PLAN)
    eq(r["rooms_all"]["judged"], 3, "rooms judged")
    eq(r["rooms_all"]["tp"], 2, "rooms confirmed")
    eq(r["rooms_all"]["fp"], 1, "rooms rejected")
    eq(r["edges"]["judged"], 2, "links judged")
    eq(r["edges"]["tp"], 1, "links confirmed")

    # known-bad: the retired vocabulary must raise rather than score as zero.
    old = dict(anno, rooms={"a": {"verdict": "correct"}})
    try:
        EA.score_one(old, plan=PLAN)
    except V.RetiredVerdict:
        pass
    else:
        raise AssertionError("known-bad: a retired verdict scored silently")
    return "3 rooms and 2 links judged; retired words now raise"


@fixed("A07", "one vocabulary, in three places that agree")
def _a07_vocab():
    eq(tuple(C.ROOM_VERDICTS), tuple(V.ROOM_VERDICTS), "python room vocabulary")
    eq(tuple(C.LINK_VERDICTS), tuple(V.LINK_VERDICTS), "python link vocabulary")
    js = (ROOT / "annotator" / "static" / "compose.js").read_text()
    for word in V.ROOM_VERDICTS + V.LINK_VERDICTS:
        truthy(f'"{word}"' in js, f"compose.js knows {word!r}")
    for word in V.RETIRED:
        truthy(f'"{word}"' not in js, f"compose.js has dropped {word!r}")
    return "compose.py, evaluation.verdicts and compose.js agree"


@fixed("M0", "the deployed annotator copy has not drifted from the source")
def _annotator_copy():
    """`ifc-graphx/annotator/` is a second copy of the tool, served as a static
    site. Two copies of the composer is exactly the liability the parity test
    already guards between Python and JavaScript, and it applies here too: an
    annotator working against the deployed build would sign off on one graph
    while the dataset received another."""
    src, dup = ROOT / "annotator", ROOT / "ifc-graphx" / "annotator"
    if not dup.exists():
        return "no deployed copy present"
    drifted, checked = [], 0
    for f in sorted(src.rglob("*")):
        if f.suffix not in (".py", ".js", ".html", ".css") or not f.is_file():
            continue
        other = dup / f.relative_to(src)
        if not other.exists():
            continue
        checked += 1
        if f.read_bytes() != other.read_bytes():
            drifted.append(str(f.relative_to(src)))
    truthy(not drifted, f"these files differ between the two copies: {drifted}")
    return f"{checked} shared source files identical"


@fixed("baselines", "a shared wall is not reported as a door")
def _schema_baseline():
    import ifcopenshell
    from ifcopenshell.api import run
    import baselines

    with tempfile.TemporaryDirectory() as td:
        f = ifcopenshell.file(schema="IFC4")
        wall = run("root.create_entity", f, ifc_class="IfcWall", name="solid wall")
        raw = [run("root.create_entity", f, ifc_class="IfcSpace", name=x)
               for x in ("a", "b")]
        for sp in raw:
            f.create_entity("IfcRelSpaceBoundary",
                            GlobalId=ifcopenshell.guid.new(), RelatingSpace=sp,
                            RelatedBuildingElement=wall,
                            PhysicalOrVirtualBoundary="PHYSICAL",
                            InternalOrExternalBoundary="INTERNAL")
        path = os.path.join(td, "boundary.ifc")
        f.write(path)
        sps = [space(sp.GlobalId, box(i * 3, 0, i * 3 + 2, 2))
               for i, sp in enumerate(raw)]
        g = baselines.m_schema(model(sps, []), sps, path)
        doors = [d for _, _, d in g.edges(data=True)
                 if d.get("relation") == "connected_by_door"]
        eq(doors, [], "a physical wall boundary yields no door")

        # known-bad: a door boundary between the same two spaces still must.
        f2 = ifcopenshell.file(schema="IFC4")
        door = run("root.create_entity", f2, ifc_class="IfcDoor", name="door")
        raw2 = [run("root.create_entity", f2, ifc_class="IfcSpace", name=x)
                for x in ("a", "b")]
        for sp in raw2:
            f2.create_entity("IfcRelSpaceBoundary",
                             GlobalId=ifcopenshell.guid.new(), RelatingSpace=sp,
                             RelatedBuildingElement=door,
                             PhysicalOrVirtualBoundary="PHYSICAL",
                             InternalOrExternalBoundary="INTERNAL")
        p2 = os.path.join(td, "door.ifc")
        f2.write(p2)
        sps2 = [space(sp.GlobalId, box(i * 3, 0, i * 3 + 2, 2))
                for i, sp in enumerate(raw2)]
        g2 = baselines.m_schema(model(sps2, []), sps2, p2)
        truthy(any(d.get("relation") == "connected_by_door"
                   for _, _, d in g2.edges(data=True)),
               "a door boundary is still read as a door")
    return "wall boundary ignored, door boundary kept"


# ==========================================================================
# A10 -- an invented room and its invented edge are counted
# ==========================================================================

@fixed("A10", "an invented room and its edge are not silently dropped")
def _a10():
    g = nx.MultiDiGraph()
    g.add_node("a", layer="space", room_number="A", centroid=[1, 1], elevation=0)
    g.add_node("b", layer="space", room_number="B", centroid=[4, 1], elevation=0)
    g.add_node("fake", layer="space", room_number="fake",
               centroid=[100, 100], elevation=0)
    g.add_edge("a", "b", key="door:1", relation="connected_by_door")
    g.add_edge("a", "fake", key="door:2", relation="connected_by_door")

    gt_dict = {"building": "t", "seed": 1,
               "rooms": [{"rid": "A", "x0": 0, "y0": 0, "x1": 2, "y1": 2, "storey": 0},
                         {"rid": "B", "x0": 3, "y0": 0, "x1": 5, "y1": 2, "storey": 0}],
               "edges": [{"a": "A", "b": "B", "type": "door"}],
               "adjacent_not_connected": []}
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.gt.json")
        json.dump(gt_dict, open(p, "w"))
        gt = load_gt(p)
    m = match_rooms(g, gt)
    eq(len(m.unmatched_pred), 1, "the invented room is unmatched, not ignored")
    row = score_connectivity(g, gt, m)
    eq(row["tp"], 1, "the real edge scores")
    eq(row["pred_unmatched_endpoint"], 1,
       "the invented edge is counted against the method")
    truthy(row["precision_strict"] < 1.0,
           "strict precision charges for the invented edge")
    eq(row["precision"], 1.0, "reference-decided precision is unaffected")
    return (f"strict precision {row['precision_strict']} vs "
            f"decided precision {row['precision']}")


@fixed("A10", "one transition is counted once, not once per direction")
def _a10_directions():
    """A portal is stored as two directed edges. Counting both would double
    every tally taken over predictions -- and on a graph written before edges
    carried identity the two directions arrive under different synthetic keys,
    which is exactly where the naive dedup failed."""
    gt_dict = {"building": "t", "seed": 1,
               "rooms": [{"rid": "A", "x0": 0, "y0": 0, "x1": 2, "y1": 2,
                          "storey": 0}],
               "edges": [], "adjacent_not_connected": []}
    with tempfile.TemporaryDirectory() as td:
        gp = os.path.join(td, "t.gt.json")
        json.dump(gt_dict, open(gp, "w"))
        gt = load_gt(gp)

        for label, keys in (("current graph, one portal id", ("p", "p")),
                            ("legacy graph, two synthetic keys",
                             ("connected_by_door:0", "connected_by_door:1"))):
            g = nx.MultiDiGraph()
            g.add_node("a", layer="space", room_number="A",
                       centroid=[1, 1], elevation=0)
            g.add_node("ghost", layer="space", room_number="ghost",
                       centroid=[90, 90], elevation=0)
            attrs = {"relation": "connected_by_door"}
            if keys[0] == "p":
                attrs["edge_id"] = "p"
            g.add_edge("a", "ghost", key=keys[0], **attrs)
            g.add_edge("ghost", "a", key=keys[1], **attrs)
            row = score_connectivity(g, gt, match_rooms(g, gt))
            eq(row["pred_unmatched_endpoint"], 1, f"{label}: counted once")
    return "one transition, both directions, counted once in either format"


@fixed("A10", "several predictions cannot all stand for one reference room")
def _a10_merge():
    g = nx.MultiDiGraph()
    for i, n in enumerate(["p1", "p2"]):
        g.add_node(n, layer="space", room_number=n,
                   centroid=[0.5 + i * 0.2, 1], elevation=0)
    gt_dict = {"building": "t", "seed": 1,
               "rooms": [{"rid": "A", "x0": 0, "y0": 0, "x1": 2, "y1": 2,
                          "storey": 0}],
               "edges": [], "adjacent_not_connected": []}
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.gt.json")
        json.dump(gt_dict, open(p, "w"))
        gt = load_gt(p)
    m = match_rooms(g, gt)
    eq(len(m.assign), 1, "only one prediction may claim the reference room")
    eq(len(m.unmatched_pred), 1, "the other stays visible as unmatched")
    truthy(m.merges, "the losing association is reported as a merge")
    return f"merges={ {k: v for k, v in m.merges.items()} }"


# ==========================================================================
# A09 -- the derived reference's units and thresholds
# ==========================================================================

@fixed("A09", "the same building in metres and millimetres yields the same voids")
def _a09():
    import ifcopenshell
    from ifcopenshell.api import run
    from generate_building import _body_ctx, _box, _ctx

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        f = ifcopenshell.file(schema="IFC4")
        run("root.create_entity", f, ifc_class="IfcProject", name="t")
        run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
        body = _body_ctx(f, _ctx(f))
        op = run("root.create_entity", f, ifc_class="IfcOpeningElement",
                 name="test opening")
        _box(f, body, op, 0., 0., 0., 1., .8, 2.1)
        metres = td / "m.ifc"
        f.write(str(metres))
        for pt in f.by_type("IfcCartesianPoint"):
            pt.Coordinates = tuple(x * 1000 for x in pt.Coordinates)
        for solid in f.by_type("IfcExtrudedAreaSolid"):
            solid.Depth *= 1000
        for unit in f.by_type("IfcSIUnit"):
            if unit.UnitType == "LENGTHUNIT":
                unit.Prefix = "MILLI"
        mm = td / "mm.ifc"
        f.write(str(mm))

        n_m = len(DR.doorway_voids(str(metres), 1.0))
        n_mm = len(DR.doorway_voids(str(mm), 1.0))
        eq((n_m, n_mm), (1, 1), "voids found in the metre and mm copies")

        # known-bad: applying the declared unit scale on top of geometry that is
        # already metres is what made the two copies disagree.
        eq(len(DR.doorway_voids(str(mm), 0.001)), 0,
           "known-bad: scaling a second time loses the opening")
    return "1 void from each copy; double-scaling still loses it"


@fixed("A09", "a doorway footprint is not filtered out by the room threshold")
def _a09_clean():
    door = box(0, 0, .9, .2)                      # 0.18 m^2
    truthy(DR._clean(door, DR.MIN_VOID_AREA) is not None,
           "a normal doorway void survives the void threshold")
    truthy(DR._clean(door, DR.MIN_BLOCKER_AREA) is not None,
           "a thin wall stub survives the blocker threshold")
    truthy(DR._clean(door) is None,
           "known-bad: the room threshold still discards it, which is why the "
           "call sites must pass their own")
    return f"void≥{DR.MIN_VOID_AREA}, blocker≥{DR.MIN_BLOCKER_AREA}, room≥{DR.MIN_AREA}"


# ==========================================================================
# A11 -- masking a label must mask what the label produced
# ==========================================================================

@fixed("A11", "a masked stair label no longer reaches the classifier")
def _a11():
    stacked = [space("lo", box(0, 0, 4, 4), label="Stair"),
               space("hi", box(0, 0, 4, 4), storey="s1", label="Stair", z=3.2)]
    m = model(stacked, [], [P.Storey("s0", "Ground", 0),
                            P.Storey("s1", "Upper", 3.2)])
    g = G.build(m, infer_missing_rooms=False)
    truthy(S.room_features(g, "lo")["n_vertical"] > 0,
           "unmasked, the stacked-label link is present")
    with ES.mask_room(g, "lo"):
        eq(S.room_features(g, "lo")["n_vertical"], 0,
           "masked, no label-derived vertical link survives")
        eq(g.nodes["lo"]["label"], "room", "the label itself is masked")
    eq(S.room_features(g, "lo")["n_vertical"], 1,
       "the link is restored afterwards")

    # known-bad: blanking the label alone -- what the evaluator used to do --
    # leaves the feature the label produced.
    kept = g.nodes["lo"]["label"]
    g.nodes["lo"]["label"] = "room"
    leaked = S.room_features(g, "lo")["n_vertical"]
    g.nodes["lo"]["label"] = kept
    eq(leaked, 1, "known-bad: blanking the label alone leaks n_vertical")
    return "n_vertical 1 -> 0 under masking; label-only masking still leaks"


@fixed("A11", "a legacy vertical link with no basis is masked, not trusted")
def _a11_legacy():
    """A graph built before links recorded their evidence cannot say which of
    its vertical links came from a label. Masking removes them all: that can
    only under-report an accuracy, whereas assuming geometry would leak the
    answer and over-report it."""
    g = nx.MultiDiGraph()
    g.graph["model"] = "legacy.ifc"
    g.add_node("lo", layer="space", label="Stair", source="ifc", elevation=0)
    g.add_node("hi", layer="space", label="Stair", source="ifc", elevation=3.2)
    for u, v in (("lo", "hi"), ("hi", "lo")):
        g.add_edge(u, v, key=f"{u}{v}", relation="vertically_connected")
    eq(S.room_features(g, "lo")["n_vertical"], 1, "unmasked, the link is there")
    with ES.mask_room(g, "lo"):
        eq(S.room_features(g, "lo")["n_vertical"], 0,
           "a link of unknown provenance is masked rather than trusted")
    eq(S.room_features(g, "lo")["n_vertical"], 1, "and restored afterwards")
    return "unknown-basis links are masked conservatively"


@fixed("A11", "a link carved by real stair geometry is kept under masking")
def _a11_geometry():
    lo = space("lo", box(0, 0, 4, 4), label="Lobby")
    hi = space("hi", box(0, 0, 4, 4), storey="s1", label="Landing", z=3.2)
    stair = product("stair", box(4.1, 0, 5.5, 4), "IfcStair", z0=0, z1=3.4)
    m = model([lo, hi], [stair], [P.Storey("s0", "Ground", 0),
                                  P.Storey("s1", "Upper", 3.2)])
    conns = T.vertical_connections(m, [lo, hi])
    truthy(conns, "a stair solid produces a vertical connection")
    eq({c["basis"] for c in conns}, {"stair_solid"},
       "it is recorded as resting on geometry, not on a label")
    return f"{len(conns)} geometric link(s), basis=stair_solid"


# ==========================================================================
# A12 -- the VLM must be shown the room it is being scored on
# ==========================================================================

@fixed("A12", "a repeated room number no longer selects an arbitrary room")
def _a12():
    import render_plan
    cache = render_plan.PlanCache.__new__(render_plan.PlanCache)
    dup = [space("g1", box(0, 0, 3, 3), label="Office"),
           space("g2", box(9, 9, 12, 12), storey="s1", label="Office", z=3.2)]
    for s in dup:
        s.name = "107"
    cache._models = {"fake.ifc": model(dup, [], [P.Storey("s0", "Ground", 0),
                                                 P.Storey("s1", "Upper", 3.2)])}
    cache._get = lambda p: cache._models[p]
    eq(cache.crop("fake.ifc", "107"), None,
       "an ambiguous room number returns no crop rather than the wrong one")
    eq(cache.crop("fake.ifc", "107", gid="g1", storey_gid="s1"), None,
       "a gid on a different storey than claimed is refused")
    return "ambiguous name refused; storey mismatch refused"


@fixed("A12", "the evaluator hands the crop a GlobalId and a storey")
def _a12_collect():
    g = nx.MultiDiGraph()
    g.graph["model"] = "fake.ifc"
    g.add_node("st", layer="storey", label="Ground")
    g.add_node("r1", layer="space", label="Kitchen", room_number="107",
               source="ifc", area=12, elevation=0.0)
    g.add_edge("st", "r1", key="contains", relation="contains")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "m.graph.json")
        G.save(g, p)
        items = ES.collect(p)
    truthy(items, "the room is collected")
    feats = items[0][2]
    eq(feats["_room_gid"], "r1", "the GlobalId travels with the features")
    eq(feats["_storey_gid"], "st", "so does the storey it sits on")
    return "features carry _room_gid and _storey_gid"


@fixed("M0", "a built graph satisfies the structural invariants")
def _invariants():
    """The subset of the design's enforced invariants (system design section 5)
    that M0 is responsible for. The geometry-side invariants -- portal sides,
    frames and units on every geometry reference, three-valued feasibility --
    are not covered here because they are not implemented yet."""
    import json
    a, b, wide, narrow = two_rooms_two_doors()
    stair = product("stair", box(4.1, -3, 5.5, 3), "IfcStair", z0=0, z1=3.4)
    m = model([a, b], [wide, narrow, stair])
    g = G.build(m, infer_missing_rooms=False)

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "g.json")
        G.save(g, p)
        raw = json.load(open(p))
        ids = {n["id"] for n in raw["nodes"]}
        eq([e for e in raw["edges"]
            if e["source"] not in ids or e["target"] not in ids], [],
           "every serialised edge endpoint exists as a node")
        eq([e for e in raw["edges"]
            if e.get("relation") != "contains" and not e.get("edge_id")], [],
           "every lateral edge carries its own identity")
        eq([e for e in raw["edges"]
            if e.get("relation") != "contains"
            and e.get("key") == e.get("relation")], [],
           "no lateral edge is keyed on its relation name")
        back = G.load(p)
        eq(back.number_of_edges(), g.number_of_edges(),
           "reload preserves every edge")

    own = nx.DiGraph()
    for u, v, d in g.edges(data=True):
        if d.get("relation") == "contains":
            own.add_edge(u, v)
    truthy(nx.is_directed_acyclic_graph(own), "containment is acyclic")
    eq([n for n in own if own.in_degree(n) > 1], [],
       "no node has two canonical parents")
    eq([(u, v) for u, v in g.edges() if u == v], [], "no self loops")

    # Adjacency is not connectivity. The two must stay separable, or a scorer
    # cannot tell "these rooms share a wall" from "you can walk between them".
    rels = {d.get("relation") for _, _, d in g.edges(data=True)}
    truthy("adjacent_to" in rels, "adjacency is recorded")
    from evaluation.scoring import CONNECTIVE
    truthy("adjacent_to" not in CONNECTIVE,
           "and is excluded from every traversability claim")
    return "endpoints, identity, acyclicity, round trip, adjacency separation"


# ==========================================================================
# manifest -- a missing run stays in the denominator
# ==========================================================================

@fixed("M0", "a missing building is a failure, not a smaller corpus")
def _manifest():
    man = RunManifest("t", expected=["b1", "b2", "b3"])
    man.ok("b1")
    man.missing("b2", "no graph built")
    # b3 was declared and never mentioned again. It must be counted as missing
    # too -- a unit nobody recorded is exactly the case that used to disappear.
    eq(man.counts()["missing"], 2, "explicit and silent omissions both counted")
    eq(man.counts()["ok"], 1, "only the one that ran counts as ok")
    eq(man.denominator(), 3, "the denominator keeps every declared unit")
    truthy(not man.complete, "the run is not complete")
    man2 = RunManifest("t2", expected=["b1"])
    man2.excluded("b1", "the reference declared itself underivable")
    eq(man2.denominator(), 0, "only an explained exclusion leaves the denominator")
    try:
        RunManifest("t3").record("x", "excluded")
    except ValueError:
        pass
    else:
        raise AssertionError("known-bad: an exclusion without a reason was allowed")
    return "denominator 3 of 3 declared; exclusions need a stated reason"


@fixed("M0", "the evaluator exits non-zero when a graph is missing")
def _cli_missing():
    with tempfile.TemporaryDirectory() as td:
        gt = {"building": "t", "seed": 1,
              "rooms": [{"rid": "A", "x0": 0, "y0": 0, "x1": 2, "y1": 2, "storey": 0}],
              "edges": [], "adjacent_not_connected": []}
        os.makedirs(f"{td}/gt"); os.makedirs(f"{td}/graphs")
        json.dump(gt, open(f"{td}/gt/t.gt.json", "w"))
        r = subprocess.run(
            [sys.executable, str(ROOT / "main" / "eval_connectivity.py"),
             "--gt", f"{td}/gt", "--graphs", f"{td}/graphs",
             "--out", f"{td}/out.json"],
            capture_output=True, text=True, cwd=str(ROOT))
        truthy(r.returncode != 0, "a missing graph makes the run fail")
        truthy("missing" in r.stdout, "and says which unit is missing")
        man = json.load(open(f"{td}/out.manifest.json"))
        eq(man["denominator"], 1, "the missing building stays in the denominator")
    return "exit code non-zero, manifest records the gap"


# ==========================================================================
# open findings -- geometry, scheduled for M1
# ==========================================================================

@open_defect("A01", "a lintel still seals the doorway in the footprint")
def _a01():
    import trimesh
    from render_floorplan import _section_segments
    meshes = []
    for extents, center in [((1.5, .2, 3), (-1.25, 0, 1.5)),
                            ((1.5, .2, 3), (1.25, 0, 1.5)),
                            ((1, .2, .9), (0, 0, 2.55))]:
        part = trimesh.creation.box(extents=extents)
        part.apply_translation(center)
        meshes.append(part)
    mesh = trimesh.util.concatenate(meshes)
    projection = P._footprint_from_mesh(mesh.vertices, mesh.faces, accurate=False)
    sections = list(polygonize(unary_union(
        [LineString(s) for s in _section_segments(mesh.vertices, mesh.faces, 1.2)])))
    eq(projection.contains(Point(0, 0)), True,
       "STILL BROKEN: the XY projection fills the doorway")
    eq(any(p.contains(Point(0, 0)) for p in sections), False,
       "a true section at 1.2 m leaves it open")
    return ("recovery still uses the projection; the section primitive is "
            "correct and unused -- M1")


@open_defect("A03", "door probing still reaches past a nearer room")
def _a03():
    a = space("a", box(-3, -3, 3, -.1))
    narrow_room = space("narrow_room", box(-3, .1, 3, .6))
    remote = space("remote", box(-3, .8, 3, 3))
    wide = product("wide", box(-2, -.1, -.8, .1))
    barrier = product("solid_wall", box(-3, .6, 3, .8), "IfcWall", z1=3)
    m = model([a, narrow_room, remote], [wide, barrier])
    conns = T.door_connections(m, m.spaces)
    joined = {frozenset((c["a"], c["b"])) for c in conns if c.get("b")}
    truthy(frozenset(("a", "remote")) in joined,
           "STILL BROKEN: the probe skips the 0.5 m room and crosses a wall")
    return f"probe joined {[sorted(p) for p in joined]} -- M1"


@open_defect("A04", "reported width is still an axis-aligned bounding box")
def _a04():
    rotated = product("rotated", rotate(box(-.5, -.025, .5, .025), 45))
    w = T._door_width(rotated)
    truthy(abs(w - 1.0) > 0.05,
           f"STILL BROKEN: a 1.0 m door rotated 45 deg reports {w:.3f} m")
    return f"1.000 m door reported as {w:.3f} m -- M1"


@open_defect("A05", "a route can still rest on an unverified connection")
def _a05():
    eq(N.edge_passable({"relation": "connected_by_door", "width": None},
                       N.PROFILES["wheeled"]), True,
       "STILL BROKEN: a portal with no measured width is treated as passable")
    stacked = [space("lo", box(0, 0, 4, 4), label="Stair"),
               space("hi", box(0, 0, 4, 4), storey="s1", label="Stair", z=3.2)]
    m = model(stacked, [], [P.Storey("s0", "Ground", 0),
                            P.Storey("s1", "Upper", 3.2)])
    g = G.build(m, infer_missing_rooms=False)
    truthy(N.plan(g, "lo", "hi", profile="legged") is not None,
           "STILL BROKEN: a legged route exists with no stair geometry at all")
    return ("feasibility is still two-valued; there is no unknown -- M2 "
            "(the link now at least records basis=stacked_label)")


# ==========================================================================

def main():
    fixed_rows = [r for r in RESULTS if r[0] == "fixed"]
    open_rows = [r for r in RESULTS if r[0] == "open"]
    bad = [r for r in RESULTS if not r[3]]

    print("REPAIRED FINDINGS -- these must hold")
    print("-" * 78)
    for _, ident, title, ok, detail in fixed_rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {ident}  {title}")
        if VERBOSE and ok and detail:
            print(f"          {detail}")
        if not ok:
            print(f"          {detail}")

    print("\nOPEN FINDINGS -- asserted still present, scheduled for M1/M2")
    print("-" * 78)
    for _, ident, title, ok, detail in open_rows:
        print(f"  {'still open' if ok else 'CHANGED'}  {ident}  {title}")
        if VERBOSE and ok and detail:
            print(f"          {detail}")
        if not ok:
            print(f"          {detail}")
            print("          This finding's behaviour changed. If you fixed it, "
                  "move the case to fixed().")

    print()
    print(f"{len(fixed_rows) - len([r for r in bad if r[0] == 'fixed'])}"
          f"/{len(fixed_rows)} repairs hold; "
          f"{len([r for r in open_rows if r[3]])}/{len(open_rows)} open findings "
          f"still reproduce as described.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
