"""Check a whole-graph extraction that failed is charged, not quietly repaired.

The per-room task learned this the expensive way: `_coerce(text, fallback=hyp)`
handed back the rule baseline's answer whenever a model's output did not parse,
so a model that said nothing scored as the rule baseline, and 95 of 592 rooms
(16.0%) went through that path on the 18 September 2026 VLM run before anybody
counted them.

The whole-graph task has strictly more ways to fail. Each of them is given a
deliberately broken input here and checked to produce a named, counted failure
rather than a partial prediction wearing a full prediction's clothes.

Every assertion in this file was run against a deliberately broken parser
before being trusted -- the standing rule in this project. Run with:

    python tests/test_graph_extraction.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "main"))

from bimsg import graph_extract as X          # noqa: E402
from evaluation import matching as M          # noqa: E402
from evaluation import schema as S            # noqa: E402
from evaluation import scoring as SC          # noqa: E402

failures = 0


def ok(msg: str, cond: bool) -> None:
    global failures
    if not cond:
        failures += 1
    print(("  PASS " if cond else "  FAIL ") + msg)


GOOD = json.dumps({
    "rooms": [{"id": "R1", "label": "kitchen", "centroid_px": [100, 200]},
              {"id": "R2", "label": "living", "centroid_px": [300, 200]}],
    "edges": [{"a": "R1", "b": "R2", "type": "door"}],
})


def test_parse_failures() -> None:
    print("\n-- a response that is not an answer is never an answer --")

    ex = X.parse_response("", hit_cap=False)
    ok(f"an empty response fails ({ex.status})",
       not ex.ok and ex.status == X.NO_JSON)

    ex = X.parse_response("I am not able to read this floor plan.", False)
    ok(f"prose with no JSON fails ({ex.status})",
       not ex.ok and ex.status == X.NO_JSON)
    ok("...and yields no rooms at all", ex.rooms == [])

    # An object that opened and never closed. The balance scan must not hand
    # back a fragment for json.loads to choke on and be reported as malformed
    # when the real cause is the token cap.
    ex = X.parse_response('{"rooms": [{"id": "R1", "label": "kit', True)
    ok(f"an unterminated object at the cap reads as truncated ({ex.status})",
       ex.status == X.TRUNCATED)
    ex = X.parse_response('{"rooms": [{"id": "R1", "label": "kit', False)
    ok(f"...and as no_json when the cap was not the cause ({ex.status})",
       ex.status == X.NO_JSON)

    ex = X.parse_response('{"rooms": [1, 2,,]}', False)
    ok(f"malformed JSON is reported as malformed ({ex.status})",
       ex.status == X.BAD_JSON and ex.detail)

    ex = X.parse_response('{"buildings": []}', False)
    ok(f"valid JSON of the wrong shape fails ({ex.status})",
       ex.status == X.BAD_SHAPE)
    ex = X.parse_response('{"rooms": {"R1": "kitchen"}}', False)
    ok(f"'rooms' as an object, not a list, fails ({ex.status})",
       ex.status == X.BAD_SHAPE)
    ex = X.parse_response('{"rooms": [{"id": "R1"}], "edges": "none"}', False)
    ok(f"'edges' as a string fails ({ex.status})",
       ex.status == X.BAD_SHAPE)

    ex = X.parse_response('[{"id": "R1"}]', False)
    ok(f"a top-level list fails ({ex.status})", ex.status == X.BAD_SHAPE)

    ex = X.parse_response('{"rooms": []}', False)
    ok(f"zero rooms is a failure, not an empty success ({ex.status})",
       not ex.ok and ex.status == X.BAD_SHAPE)


def test_think_traces() -> None:
    print("\n-- a thinking model's deliberation is not its answer --")

    # The trace contains a complete, valid JSON object that the model then
    # revised. Reading it would score the model's first draft.
    trace = ('<think>Maybe {"rooms": [{"id": "X", "label": "bathroom"}]} '
             'but no, that is wrong.</think>' + GOOD)
    ex = X.parse_response(trace, False)
    ok(f"the answer after </think> is used ({[r['id'] for r in ex.rooms]})",
       ex.ok and {r["id"] for r in ex.rooms} == {"R1", "R2"})
    ok("...and the JSON inside the trace is not mined",
       all(r["id"] != "X" for r in ex.rooms))

    ex = X.parse_response('<think>let me look at the walls', True)
    ok(f"an unclosed trace at the cap is truncated ({ex.status})",
       ex.status == X.TRUNCATED)
    ex = X.parse_response('<think>let me look at the walls', False)
    ok(f"an unclosed trace under the cap is a shape failure ({ex.status})",
       ex.status == X.BAD_SHAPE)
    ok("...and neither yields rooms", ex.rooms == [])


def test_item_accounting() -> None:
    print("\n-- a dropped item is counted, never silently dropped --")

    resp = json.dumps({
        "rooms": [{"id": "R1", "label": "kitchen"},
                  {"id": "R2", "label": "living"},
                  {"id": "R1", "label": "office"},        # duplicate
                  {"label": "office"},                    # no id
                  "not an object"],
        "edges": [{"a": "R1", "b": "R2", "type": "door"},
                  {"a": "R1", "b": "R9", "type": "door"},   # undeclared
                  {"a": "R1", "b": "R1", "type": "door"},   # self loop
                  {"a": "R1", "b": "R2", "type": "teleport"},  # bad type
                  {"a": "R1", "type": "door"},              # missing endpoint
                  "not an object"],
    })
    ex = X.parse_response(resp, False)
    ok(f"the good rooms survive ({len(ex.rooms)})", len(ex.rooms) == 2)
    ok(f"a duplicate id is counted ({ex.dropped_rooms})",
       ex.dropped_rooms.get("duplicate_id") == 1)
    ok("a room with no id is counted", ex.dropped_rooms.get("no_id") == 1)
    ok("a non-object room is counted",
       ex.dropped_rooms.get("not_an_object") == 1)

    ok(f"only the valid edge survives ({len(ex.edges)})", len(ex.edges) == 1)
    ok("an edge naming an undeclared room is counted",
       ex.dropped_edges.get("undeclared_endpoint") == 1)
    ok("a self loop is counted", ex.dropped_edges.get("self_loop") == 1)
    ok("an unknown edge type is counted",
       ex.dropped_edges.get("unknown_type") == 1)
    ok("an edge missing an endpoint is counted",
       ex.dropped_edges.get("missing_endpoint") == 1)
    ok("a non-object edge is counted",
       ex.dropped_edges.get("not_an_object") == 1)

    s = ex.summary()
    ok(f"the summary totals the drops ({s['dropped_rooms_total']} rooms, "
       f"{s['dropped_edges_total']} edges)",
       s["dropped_rooms_total"] == 3 and s["dropped_edges_total"] == 5)


def test_to_graph() -> None:
    print("\n-- the graph a failure produces is empty, not partial --")

    for bad in ("", "no json here", '{"rooms": []}',
                '{"rooms": [{"id": "R1"', '{"buildings": []}'):
        ex = X.parse_response(bad, False)
        g = X.to_graph(ex)
        n_spaces = len([n for n, d in g.nodes(data=True)
                        if d.get("layer") == "space"])
        ok(f"a {ex.status!r} extraction yields an empty graph "
           f"({n_spaces} rooms, {g.number_of_edges()} edges)",
           n_spaces == 0 and g.number_of_edges() == 0)
        ok("...and the graph records why",
           g.graph.get("extraction_status") == ex.status)

    ex = X.parse_response(GOOD, False)
    tr = {"px_per_m": 50.0, "tx": 100.0, "ty": 900.0,
          "width": 800, "height": 1000}
    g = X.to_graph(ex, transform=tr, elevation=0.0, storey_gid="ST0")
    spaces = [n for n, d in g.nodes(data=True) if d.get("layer") == "space"]
    ok(f"a good extraction yields its rooms ({len(spaces)})", len(spaces) == 2)
    ok("both directions of the edge are emitted, as the pipeline does",
       g.number_of_edges() >= 2)

    # score_labels reads ONLY predicted_label, precisely so that a prediction
    # can never be compared against itself. If to_graph wrote `label`, a model
    # emitting nothing would inherit the reference name and score perfectly --
    # the same shape as the 380/380 edge-type tautology.
    d = g.nodes["R1"]
    ok(f"the model's label lands on predicted_label ({d.get('predicted_label')!r})",
       d.get("predicted_label") == "kitchen")
    ok("...and NOT on label, which the scorer would read as truth",
       d.get("label") is None)

    x, y = d["centroid"]
    ok(f"the pixel centroid is lifted into metres ({x}, {y})",
       abs(x - 0.0) < 1e-6 and abs(y - 14.0) < 1e-6)

    # Round-trip, because an inverted sign on ty is invisible in a single
    # number but puts every room on the wrong side of the building.
    for pxy in ((0, 0), (123.5, 456.5), (800, 1000)):
        mx, my = X.px_to_m(pxy[0], pxy[1], tr)
        bx = tr["px_per_m"] * mx + tr["tx"]
        by = -tr["px_per_m"] * my + tr["ty"]
        ok(f"px->m->px round-trips at {pxy} ({bx:.6f}, {by:.6f})",
           abs(bx - pxy[0]) < 1e-6 and abs(by - pxy[1]) < 1e-6)

    ex2 = X.parse_response(GOOD, False)
    g2 = X.to_graph(ex2, transform=None)
    ok("without a transform, pixels are NOT written as world coordinates",
       g2.nodes["R1"].get("centroid") is None
       and g2.nodes["R1"].get("centroid_px") is not None)

    print("\n-- metres and pixels are never pooled --")
    # The text condition works in metres and must NOT be lifted as though its
    # numbers were pixels: passing a metre value through px_to_m moves the room
    # hundreds of metres and reads as a total recall failure, not a unit error.
    m_resp = json.dumps({"rooms": [{"id": "R1", "label": "office",
                                    "centroid_m": [3.5, 7.25]}],
                         "edges": []})
    gm = X.to_graph(X.parse_response(m_resp, False), transform=tr)
    ok(f"a metre centroid is used as-is, not lifted ({gm.nodes['R1']['centroid']})",
       gm.nodes["R1"]["centroid"] == [3.5, 7.25])

    # The bare key the contract never asked for: units unknown, so guessing
    # either reading would be inventing a coordinate system.
    bare = json.dumps({"rooms": [{"id": "R1", "label": "office",
                                  "centroid": [3.5, 7.25]}], "edges": []})
    gb = X.to_graph(X.parse_response(bare, False), transform=tr)
    ok("an ambiguous bare centroid is not silently treated as metres",
       gb.nodes["R1"].get("centroid") is None)
    ok("...and is not silently treated as pixels either",
       gb.nodes["R1"].get("centroid_unknown_units") == [3.5, 7.25])


def test_restrict() -> None:
    print("\n-- restricting scope moves both sides, and says what it lost --")

    gt = S.GroundTruth(
        building="b", provenance="generator", identity=S.IDENTITY_ROOM_NUMBER,
        rooms={"a": S.Room("a", storey=0, storey_index=0),
               "b": S.Room("b", storey=0, storey_index=0),
               "c": S.Room("c", storey=1, storey_index=1)},
        positives={S.pair("a", "b"): "door", S.pair("b", "c"): "vertical"},
        negatives={S.pair("a", "c")}, unknown=set())

    r = gt.restrict({"a", "b"}, reason="storey 0")
    ok(f"rooms outside the scope are gone ({sorted(r.rooms)})",
       set(r.rooms) == {"a", "b"})
    ok(f"a pair inside the scope is kept ({len(r.positives)})",
       S.pair("a", "b") in r.positives and len(r.positives) == 1)
    ok("a pair straddling the boundary is not kept as a positive",
       S.pair("b", "c") not in r.positives)
    ok("...nor smuggled in as a negative",
       S.pair("a", "c") not in r.negatives)
    ok(f"both straddling pairs are counted, not forgotten "
       f"({r.scope['dropped_straddling']})",
       r.scope["dropped_straddling"] == 2)
    ok(f"the scope names the restriction ({r.scope['restricted_to']!r})",
       r.scope["restricted_to"] == "storey 0")
    ok("the original is untouched",
       len(gt.rooms) == 3 and len(gt.positives) == 2)

    ok(f"rooms_on_storey reads an int index ({sorted(gt.rooms_on_storey(0))})",
       gt.rooms_on_storey(0) == {"a", "b"})
    ok("rooms_on_storey reads a string name",
       gt.rooms_on_storey("1") == {"c"})


def test_unscorable_diagnosis() -> None:
    print("\n-- zero matches for the reference's reasons is not a 0.000 score --")

    ex = X.parse_response(GOOD, False)
    g = X.to_graph(ex, transform={"px_per_m": 50.0, "tx": 0.0, "ty": 0.0,
                                  "width": 10, "height": 10})

    # The annotated/derived dialects state no bbox, by design. A model that
    # invents room ids therefore cannot match at all -- and the resulting
    # precision 0.000 would describe the reference, not the model.
    gt = S.GroundTruth(
        building="b", provenance="annotated", identity=S.IDENTITY_NODE_ID,
        rooms={"gid-1": S.Room("gid-1", label="Kitchen"),
               "gid-2": S.Room("gid-2", label="Living Room")},
        positives={S.pair("gid-1", "gid-2"): "door"})
    m = M.match_rooms(g, gt)
    ok(f"the impossibility is named ({(m.unscorable or '')[:48]}...)",
       m.unscorable is not None and "geometric matching is unavailable"
       in m.unscorable)
    ok("...and it is not silently scored", len(m.assign) == 0)

    # With a bbox available, a miss is a real miss and must NOT be excused.
    gt_geo = S.GroundTruth(
        building="b", provenance="generator", identity=S.IDENTITY_ROOM_NUMBER,
        rooms={"g1": S.Room("g1", label="Kitchen", bbox=(900, 900, 910, 910))},
        positives={})
    m2 = M.match_rooms(g, gt_geo)
    ok(f"a genuine geometric miss is NOT excused ({m2.unscorable!r})",
       m2.unscorable is None)

    # The mirror case, which is what a harness bug looks like: the reference
    # could have matched geometrically, but the prediction was never lifted
    # into metres, so nothing could match and precision would read 0.000 for
    # a model that may have been right.
    g_unlifted = X.to_graph(X.parse_response(GOOD, False), transform=None)
    m4 = M.match_rooms(g_unlifted, gt_geo)
    ok(f"an unlifted prediction is named as a harness fault "
       f"({(m4.unscorable or '')[:44]}...)",
       m4.unscorable is not None and "harness or prompt fault" in m4.unscorable)

    # And identity matching still works when the model is given real ids.
    ex3 = X.parse_response(json.dumps({
        "rooms": [{"id": "gid-1", "label": "kitchen"},
                  {"id": "gid-2", "label": "living"}],
        "edges": [{"a": "gid-1", "b": "gid-2", "type": "door"}]}), False)
    g3 = X.to_graph(ex3)
    m3 = M.match_rooms(g3, gt)
    ok(f"real ids match by identity ({m3.by_identity})", m3.by_identity == 2)
    ok("...and nothing is flagged unscorable", m3.unscorable is None)

    sc = SC.score_connectivity(g3, gt, m3)
    ok(f"the existing scorer runs on a model graph unchanged "
       f"(tp={sc['tp']}, P={sc['precision']}, R={sc['recall']})",
       sc["tp"] == 1 and sc["recall"] == 1.0)
    sl = SC.score_labels(g3, gt, m3)
    ok(f"and so does the label scorer ({sl['correct']}/{sl['scored']})",
       sl["scored"] == 2 and sl["correct"] == 2)


def test_empty_prediction_scores_zero_not_perfect() -> None:
    print("\n-- a model that says nothing scores zero, not the baseline --")

    gt = S.GroundTruth(
        building="b", provenance="annotated", identity=S.IDENTITY_NODE_ID,
        rooms={"gid-1": S.Room("gid-1", label="Kitchen"),
               "gid-2": S.Room("gid-2", label="Living Room")},
        positives={S.pair("gid-1", "gid-2"): "door"})

    g = X.to_graph(X.parse_response("", False))
    m = M.match_rooms(g, gt)
    sc = SC.score_connectivity(g, gt, m)
    sl = SC.score_labels(g, gt, m)
    ok(f"no prediction means no true positives (tp={sc['tp']})",
       sc["tp"] == 0)
    ok(f"...recall is 0, not None ({sc['recall']})", sc["recall"] == 0.0)
    # The building's 2 labelled rooms stay in the denominator. Scoring an
    # empty prediction as "nothing scorable" would be indistinguishable from
    # "this building has no ground truth", and a model that answers nothing
    # would leave the comparison rather than losing it.
    ok(f"the building's labelled rooms stay in the denominator "
       f"({sl['correct']}/{sl['scored']})",
       sl["scored"] == 2 and sl["correct"] == 0)
    ok(f"...so the comparable accuracy is 0.000, not undefined "
       f"({sl['accuracy_strict']})", sl["accuracy_strict"] == 0.0)
    ok("...and both rooms are charged as a missing prediction",
       sl["pred_missing"] == 2 and sl["scored_matched"] == 0)
    ok("an empty prediction is not flagged unscorable -- it IS a failure",
       m.unscorable is None or "reference" not in (m.unscorable or ""))


def test_prompts() -> None:
    print("\n-- the four modalities ask for the same thing --")

    storey = {
        "name": "Ground Floor", "elevation_m": 0.0,
        "spaces": [{"global_id": "gid-1", "name": "LIVING ROOM",
                    "area_m2": 10.8, "centroid": [1.0, 2.0],
                    "polygon": [[0, 0], [4, 0], [4, 3], [0, 3], [0, 0]]}],
        "elements": [{"global_id": "w1", "ifc_class": "IfcWall",
                      "polylines": [[[0, 0], [4, 0]]]},
                     {"global_id": "d1", "ifc_class": "IfcDoor",
                      "polylines": [[[1, 0], [2, 0]]]}],
        "transform": {"px_per_m": 50.0, "tx": 0, "ty": 0,
                      "width": 200, "height": 200},
    }

    for mod in ("text", "plan", "occ", "text+plan"):
        sysp, userp, wants_img = X.build_prompt(mod, storey)
        ok(f"{mod}: asks for the taxonomy and the JSON contract",
           all(c in userp for c in X.__dict__["L"].CLASSES[:3])
           and '"rooms"' in userp and '"edges"' in userp)
        ok(f"{mod}: image flag is {wants_img}",
           wants_img == (mod in ("plan", "occ", "text+plan")))
        ok(f"{mod}: forbids cross-storey edges (the stated task scope)",
           "different storeys" in userp)
        pixel_asked = "centroid_px" in userp
        ok(f"{mod}: asks for pixel centroids only when it sees an image "
           f"({pixel_asked})",
           pixel_asked == (mod in ("plan", "occ", "text+plan")))

    # The masked condition must actually remove the architect's names, or the
    # text row solves labelling by copying IfcSpace.Name while the vision row
    # has to infer it, and the modality gap becomes an artefact of the input.
    given = X.storey_text(storey, mask_labels=False)
    masked = X.storey_text(storey, mask_labels=True)
    ok("names given: the room name is present", "LIVING ROOM" in given)
    ok("names masked: the room name is gone", "LIVING ROOM" not in masked)
    ok("names masked: the geometry survives",
       "area=10.8" in masked and "gid-1" in masked)

    ok("the vertical relation is not asked for",
       "vertically_connected" not in X.EXTRACT_RELATIONS
       and set(X.EXTRACT_RELATIONS) == {"connected_by_door", "open_passage"})

    print("\n-- the text condition is not handed the rooms it must recover --")
    # The sidecars mix the pipeline's own recovered rooms into `spaces`, tagged
    # source="inferred". Leaking those solves the room-recovery half of the
    # task and flattens the damage axis: measured on synth150_L2, an
    # unfiltered reading saw 500 spaces where the IFC declares 261.
    leaky = {
        "name": "Ground Floor", "elevation_m": 0.0,
        "spaces": [
            {"global_id": "declared-1", "name": "OFFICE", "source": "ifc",
             "area_m2": 12.0, "centroid": [1.0, 1.0]},
            {"global_id": "recovered-1", "name": "Corridor",
             "source": "inferred", "area_m2": 8.0, "centroid": [5.0, 1.0]},
        ],
        "elements": [], "transform": None,
    }
    txt = X.storey_text(leaky)
    ok("an IFC-declared space is shown", "declared-1" in txt)
    ok("a pipeline-recovered room is NOT shown",
       "recovered-1" not in txt)
    ok("...and its name does not leak either", "Corridor" not in txt)
    ok(f"the stated count is the declared count, not the recovered total "
       f"({'(1)' in txt})", "Spaces declared in the file (1)" in txt)
    # A sidecar with no `source` field at all must not silently lose every
    # room -- absent means "as declared", not "inferred".
    bare = {"name": "S", "spaces": [{"global_id": "x1", "name": "OFFICE",
                                     "area_m2": 9.0, "centroid": [0, 0]}],
            "elements": []}
    ok("a space with no source field is still shown",
       "x1" in X.storey_text(bare))

    try:
        X.build_prompt("interpretive-dance", storey)
        ok("an unknown modality raises", False)
    except ValueError:
        ok("an unknown modality raises rather than silently picking one", True)


def test_set_of_mark() -> None:
    print("\n-- a mark number resolves to a room, or is charged --")

    marks = [{"index": 1, "gid": "GID-A", "centroid": [2.0, 3.0]},
             {"index": 2, "gid": "GID-B", "centroid": [8.0, 3.0]}]
    resp = json.dumps({
        "rooms": [{"id": "1", "label": "kitchen"},
                  {"id": "Room 2", "label": "living"},
                  {"id": "7", "label": "office"}],          # no such mark
        "edges": [{"a": "1", "b": "Room 2", "type": "door"},
                  {"a": "1", "b": "7", "type": "open"}]})
    ex = X.parse_response(resp, False)
    g = X.to_graph(ex, marks=marks)

    ok(f"a mark that was never drawn is charged ({ex.dropped_rooms})",
       ex.dropped_rooms.get("unresolved_mark") == 1)
    ok(f"an edge onto it is charged too ({ex.dropped_edges})",
       ex.dropped_edges.get("endpoint_unresolved_mark") == 1)

    # The node takes the marked room's GlobalId, which is what makes identity
    # matching work on the real corpus, and the marked room's true centroid,
    # which is what makes geometric matching work on the synthetic one. The
    # model supplied neither -- that is the point of the condition.
    spaces = {n for n, d in g.nodes(data=True) if d.get("layer") == "space"}
    ok(f"resolved rooms carry the reference GlobalId ({sorted(spaces)})",
       spaces == {"GID-A", "GID-B"})
    ok("...and the mark's true centroid, not the model's guess",
       g.nodes["GID-A"]["centroid"] == [2.0, 3.0])
    ok("...and record which mark they came from",
       g.nodes["GID-A"]["mark_index"] == 1)
    ok("the surviving edge is rewritten onto the resolved ids",
       any(u == "GID-A" and v == "GID-B"
           for u, v, _ in g.edges(keys=True)))

    # Accepted id forms are a fixed set. "13" must NOT resolve to mark 1 --
    # a prefix or substring match would silently attach an edge to the wrong
    # room and there would be nothing in the output to show it happened.
    by_index = {"1": marks[0], "2": marks[1]}
    for form in ("1", "room 1", "Room_1", "#1", "R1", " 1 "):
        ok(f"id form {form!r} resolves",
           X._match_mark(form, by_index) is not None)
    for form in ("13", "one", "", "1a", "room"):
        ok(f"id form {form!r} does NOT resolve",
           X._match_mark(form, by_index) is None)

    # The set-of-mark prompt must not ask for coordinates: asking for them on a
    # bare plan is what produced a round-50-px grid in one corner of the image.
    for mod in X.SOM_MODALITIES:
        _, user, wants_img = X.build_prompt(mod, {
            "name": "S", "spaces": [], "elements": []})
        ok(f"{mod}: asks for no coordinate", "centroid" not in user)
        ok(f"{mod}: wants an image", wants_img is True)
        ok(f"{mod}: tells the model to use the printed numbers",
           "number printed" in user)


def test_label_denominator_is_the_buildings_not_the_models() -> None:
    print("\n-- label accuracy must not be scored on a denominator the model chose --")
    # Iterating the matching makes the denominator the model's own matching
    # success: a model that places 2 of 27 rooms is scored on 2 labels, and
    # those 2 are the ones it was confident enough to place. Measured on real
    # runs this INVERTED a comparison -- Qwen2.5-VL on a bare plan scored
    # 0.250 over 12 matched rooms against InternVL3 on text+plan_som at 0.185
    # over 27, so the model scored on half as many rooms looked better.
    gt = S.GroundTruth(
        building="b", provenance="annotated", identity=S.IDENTITY_NODE_ID,
        rooms={f"gid-{i}": S.Room(f"gid-{i}", label="Kitchen")
               for i in range(10)},
        positives={})

    # A prediction that finds ONE room and labels it correctly.
    ex = X.parse_response(json.dumps({
        "rooms": [{"id": "gid-0", "label": "kitchen"}], "edges": []}), False)
    g = X.to_graph(ex)
    m = M.match_rooms(g, gt)
    sl = SC.score_labels(g, gt, m)

    ok(f"it matched only 1 of 10 reference rooms ({sl['scored_matched']}/"
       f"{sl['scored']})",
       sl["scored_matched"] == 1 and sl["scored"] == 10)
    ok(f"the comparable accuracy is over the BUILDING's 10 rooms "
       f"({sl['accuracy_strict']:.2f})",
       abs(sl["accuracy_strict"] - 0.1) < 1e-9)
    ok(f"the matched-only accuracy is 1.000 and must never be quoted alone "
       f"({sl['accuracy']:.2f})", sl["accuracy"] == 1.0)
    ok(f"the 9 unreached reference rooms are counted "
       f"({sl['unmatched_reference_rooms']})",
       sl["unmatched_reference_rooms"] == 9)
    ok("...and each is charged as a missing prediction",
       sl["pred_missing"] == 9)

    # The inversion, reproduced in miniature: model A finds 1 room and gets it
    # right; model B finds all 10 and gets 5 right. B is plainly better.
    exB = X.parse_response(json.dumps({
        "rooms": [{"id": f"gid-{i}", "label": "kitchen" if i < 5 else "office"}
                  for i in range(10)], "edges": []}), False)
    gB = X.to_graph(exB)
    slB = SC.score_labels(gB, gt, M.match_rooms(gB, gt))
    ok(f"matched-only ranks the worse model HIGHER "
       f"(A {sl['accuracy']:.2f} vs B {slB['accuracy']:.2f})",
       sl["accuracy"] > slB["accuracy"])
    ok(f"the comparable figure ranks them correctly "
       f"(A {sl['accuracy_strict']:.2f} vs B {slB['accuracy_strict']:.2f})",
       sl["accuracy_strict"] < slB["accuracy_strict"])


def test_output_tag_agreement() -> None:
    print("\n-- the writer and the resume check name files identically --")
    # These were briefly two separate f-strings that disagreed, so every text
    # model overwrote the previous one's results (8 files where 24 belonged)
    # and the grid's resume check looked for names that never existed.
    sys.path.insert(0, os.path.join(ROOT, "main"))
    import extract_graph_llm as E
    import run_grid as R

    for cell in R.cells():
        kind = "llm" if E.MODELS[cell["model"]]["kind"] == "text" else "vlm"
        want = R.cell_tag(cell)
        got = E.output_tag(
            os.path.join("dataset/synthetic_data", cell["corpus"]),
            kind, cell["model"], cell["modality"], cell["mask"])
        if want != got:
            ok(f"tag disagreement: run_grid={want!r} harness={got!r}", False)
            return
    ok(f"all {len(R.cells())} grid cells agree between writer and resume check",
       True)

    # And every cell must have a DISTINCT stem, or two cells share a file and
    # one silently overwrites the other.
    tags = [R.cell_tag(c) for c in R.cells()]
    dupes = {t for t in tags if tags.count(t) > 1}
    ok(f"every cell has a distinct output stem ({len(set(tags))}/{len(tags)})",
       not dupes)
    if dupes:
        for t in sorted(dupes)[:5]:
            print(f"         duplicated stem: {t}")


def main() -> int:
    test_parse_failures()
    test_think_traces()
    test_item_accounting()
    test_to_graph()
    test_restrict()
    test_unscorable_diagnosis()
    test_empty_prediction_scores_zero_not_perfect()
    test_prompts()
    test_set_of_mark()
    test_output_tag_agreement()
    test_label_denominator_is_the_buildings_not_the_models()
    print()
    return failures


if __name__ == "__main__":
    n = main()
    print(f"FAIL {n} problem(s) in whole-graph extraction" if n else
          "PASS an unparseable extraction is charged, never repaired")
    sys.exit(1 if n else 0)
