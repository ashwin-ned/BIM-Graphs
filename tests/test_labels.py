"""Check the label vocabulary, and that it stays honest about what it misses.

The defect this replaces was not a wrong answer, it was a missing distinction:
`semantics.canonical()` returned `None` both for a string with no functional
content ("Room") and for a real room function outside the taxonomy ("Gym"), so
1298 of 6323 IFC-named rooms left every semantic score with no trace. The first
exclusion is correct and the second overstates coverage, and nothing could tell
them apart.

The sharpest case that hid behind it: `main/generate_building.py:112` emits
"Lab" as one of twelve synthetic room types, and no keyword matched it, so
**166 of 2049 rooms in the primary corpus (8.1%) were silently unscorable**.
That is the regression guarded below -- if the generator gains a room type the
vocabulary does not know, this test fails rather than the corpus quietly
shrinking.

    python tests/test_labels.py
"""

from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "main"))

from evaluation import labels as L        # noqa: E402

failures = 0


def ok(msg: str, cond: bool) -> None:
    global failures
    if not cond:
        failures += 1
    print(("  PASS " if cond else "  FAIL ") + msg)


def cls(s):
    return L.classify(s)[1]


def outcome(s):
    return L.classify(s)[0]


def main() -> int:
    # ---- the distinction that did not exist before -----------------------
    ok("'Room' is UNNAMED, not a taxonomy gap", outcome("Room") == L.UNNAMED)
    ok("'Room Name' is UNNAMED", outcome("Room Name") == L.UNNAMED)
    ok("'Raum' (German for room) is UNNAMED", outcome("Raum") == L.UNNAMED)
    ok("'room_12' is UNNAMED (a bare identifier)",
       outcome("room_12") == L.UNNAMED)
    ok("'Gym' is UNMAPPED, not unnamed", outcome("Gym") == L.UNMAPPED)
    ok("'Studio' is UNMAPPED", outcome("Studio") == L.UNMAPPED)
    ok("'Office' is a CLASS", outcome("Office") == L.CLASS)
    ok("the four outcomes are distinct",
       len({L.CLASS, L.UNNAMED, L.UNMAPPED, L.AMBIGUOUS}) == 4)

    # ---- the two measured bugs -------------------------------------------
    ok("'Store Front' is retail, not storage (dict-order bug)",
       cls("Store Front") == "retail")
    ok("'W.C. (Women)' is bathroom (punctuation was never stripped)",
       cls("W.C. (Women)") == "bathroom")
    ok("'W/C' is bathroom", cls("W/C") == "bathroom")
    # Longest-match was tried as the fix for the ordering bug and rejected,
    # because "shaft" (5) would beat "lift" (4) and a lift is vertical
    # circulation, not plant.
    ok("'Lift Shaft' is stair, not service (longest-match was rejected)",
       cls("Lift Shaft") == "stair")

    # ---- false-positive keyword hits, resolved explicitly ----------------
    ok("'Conference Hall' is meeting, not circulation",
       cls("Conference Hall") == "meeting")
    ok("'toilet cabin' is bathroom, not office",
       cls("toilet cabin") == "bathroom")
    ok("'Office Lobby' is circulation", cls("Office Lobby") == "circulation")

    # ---- genuinely combined rooms are not guessed at ---------------------
    ok("'Living room / Kitchen' is AMBIGUOUS, not silently one of them",
       outcome("Living room / Kitchen") == L.AMBIGUOUS)
    ok("an ambiguous label yields no class",
       cls("Living room / Kitchen") is None)

    # ---- abbreviations and instance numbers ------------------------------
    ok("'Corr. 1' is circulation", cls("Corr. 1") == "circulation")
    ok("'Conf. 2' is meeting", cls("Conf. 2") == "meeting")
    ok("'Work Room 2' is office (trailing index stripped)",
       cls("Work Room 2") == "office")

    # ---- table integrity --------------------------------------------------
    bad = {k: v for k, v in L.ALIASES.items() if v not in L.CLASSES}
    ok(f"every alias maps to a real class ({len(L.ALIASES)} aliases)", not bad)
    for k, v in list(bad.items())[:5]:
        print(f"         {k!r} -> {v!r} is not in CLASSES")
    ok("no string is both an alias and a no-function term",
       not (set(L.ALIASES) & L.NO_FUNCTION))
    ok("every keyword table key is a real class",
       set(L.KEYWORDS) <= set(L.CLASSES))
    ok("the vocabulary is versioned", bool(L.VOCAB_VERSION))

    # The backends are told to predict into semantics.CANON, so a class list
    # that drifts from it asks a model for a label the scorer cannot score.
    try:
        from bimsg.semantics import CANON
        ok(f"CLASSES matches semantics.CANON ({len(L.CLASSES)} classes)",
           tuple(L.CLASSES) == tuple(CANON))
    except Exception as e:
        print(f"  SKIP could not import semantics.CANON ({type(e).__name__})")

    # ---- determinism ------------------------------------------------------
    probes = ["Store Front", "Conference Hall", "Lift Shaft", "Lab",
              "Living room / Kitchen", "W.C. (Men)", "Gym", "Room"]
    once = [L.classify(p) for p in probes]
    twice = [L.classify(p) for p in probes]
    ok("classification is deterministic", once == twice)

    # ---- the primary corpus must be fully scorable -----------------------
    gt = sorted(glob.glob(os.path.join(
        ROOT, "dataset", "synthetic_data", "synth150", "*.gt.json")))
    if not gt:
        print("  SKIP synth150 not present")
    else:
        unscorable = {}
        total = 0
        for p in gt:
            for r in json.load(open(p))["rooms"]:
                total += 1
                name = r.get("name")
                if L.classify(name)[0] != L.CLASS:
                    unscorable[name] = unscorable.get(name, 0) + 1
        ok(f"every room type in the primary corpus is scorable "
           f"({total} rooms, {sum(unscorable.values())} unscorable)",
           not unscorable)
        for name, n in sorted(unscorable.items(), key=lambda kv: -kv[1])[:8]:
            print(f"         {name!r} x{n} has no class -- add it to "
                  f"evaluation/labels.py")

    # ---- coverage on the real corpus, reported not asserted --------------
    plans = sorted(glob.glob(os.path.join(
        ROOT, "dataset", "2025_BIMfundamentals", "plans", "*.plan.json")))
    if not plans:
        print("  SKIP the real corpus plans are not present")
    else:
        names = []
        for p in plans:
            for st in json.load(open(p)).get("storeys") or []:
                for r in st.get("rooms") or []:
                    if r.get("source") == "ifc":
                        names.append(r.get("label"))
        t = L.tally(names)
        named = len(names) - t[L.UNNAMED]
        pct = 100 * t[L.CLASS] / named if named else 0
        # A floor, not a target: this guards against a regression in the
        # vocabulary, and the honest denominator is rooms that carry a name.
        ok(f"real-corpus coverage of named rooms is >=85% ({pct:.1f}%)",
           pct >= 85.0)
        print(f"         {len(names)} IFC-stated rooms: {t[L.CLASS]} class, "
              f"{t[L.UNNAMED]} unnamed, {t[L.UNMAPPED]} unmapped, "
              f"{t[L.AMBIGUOUS]} ambiguous")

    # ---- score_labels: the tautology it must refuse ----------------------
    # The first version of score_labels fell back to the node's own `label`
    # when no prediction was present. That scores the IFC name against the IFC
    # name, so a pipeline predicting nothing would have reported near-perfect
    # accuracy -- the same shape as the 380/380 edge-type figure, where
    # compose.py copied the pipeline's relation and the scorer compared it to
    # itself.
    import networkx as nx
    from evaluation.scoring import score_labels
    from evaluation.schema import GroundTruth, Room
    from evaluation.matching import Matching

    gt = GroundTruth(building="t", provenance="annotated", identity="node_id",
                     rooms={"r1": Room(rid="r1", label="Office"),
                            "r2": Room(rid="r2", label="Kitchen"),
                            "r3": Room(rid="r3", label="Room"),      # unnamed
                            "r4": Room(rid="r4", label="Gym")})      # unmapped
    g = nx.MultiDiGraph()
    for n, lab in (("n1", "Office"), ("n2", "Kitchen"),
                   ("n3", "Room"), ("n4", "Gym")):
        g.add_node(n, layer="space", label=lab)     # label only, no prediction
    mt = Matching(assign={"n1": "r1", "n2": "r2", "n3": "r3", "n4": "r4"})

    r = score_labels(g, gt, mt)
    ok("a graph with no predicted_label scores 0.000, not 1.000",
       r["accuracy"] == 0.0 and r["correct"] == 0)
    ok(f"...and every scorable room is charged as missing ({r['pred_missing']})",
       r["pred_missing"] == r["scored"] == 2)
    ok("the unnamed room is excluded as having no reference label",
       r["no_reference_label"] == 1)
    ok("the unmapped room is counted separately, not as unnamed",
       r["reference_unmapped"] == 1)

    # Now give it real predictions: one right, one wrong.
    g.nodes["n1"]["predicted_label"] = "Office"
    g.nodes["n2"]["predicted_label"] = "Bathroom"
    r = score_labels(g, gt, mt)
    ok(f"a correct prediction scores ({r['correct']}/{r['scored']})",
       r["correct"] == 1 and r["scored"] == 2 and r["accuracy"] == 0.5)
    ok("the majority baseline is reported alongside accuracy",
       r["majority_baseline"] == 0.5)
    ok("the confusion is recorded",
       any(c["truth"] == "kitchen" and c["predicted"] == "bathroom"
           for c in r["confusions"]))

    # An unusable prediction is charged, not treated as correct or absent.
    g.nodes["n2"]["predicted_label"] = "Gym"
    r = score_labels(g, gt, mt)
    ok("a prediction outside the vocabulary is charged separately",
       r["pred_unmapped"] == 1 and r["correct"] == 1)
    ok("the vocabulary version travels with the score",
       r["vocab_version"] == L.VOCAB_VERSION)

    print()
    return failures


if __name__ == "__main__":
    n = main()
    print(f"FAIL the label vocabulary has {n} problem(s)" if n else
          "PASS the label vocabulary separates 'no name' from 'no class', and "
          "the primary corpus is fully scorable")
    sys.exit(1 if n else 0)
