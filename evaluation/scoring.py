"""Score recovered connectivity in the matched room space.

Two rules the previous scorers broke.

**Nothing leaves the accounting for being unmatched.** An edge whose endpoint
is a room the reference does not have is not scoreless -- it is a claim the
method made about a building, and dropping it meant an invented room and its
invented edge cost nothing at all. Such edges are counted, and they enter the
strict precision denominator.

**Unknown is not negative.** A pair the reference examined and could not decide,
or a link a human marked unsure, is neither a hit nor a miss. It is reported in
its own bucket, never folded into precision. The headline precision is over
pairs the reference actually ruled on; the strict figure additionally charges
for predictions outside that scope, and both are reported because they answer
different questions.
"""

from __future__ import annotations

from collections import Counter

from .schema import pair

CONNECTIVE = ("connected_by_door", "open_passage", "vertically_connected")

# Reference edge type -> the relation the pipeline should emit for it.
TYPE_MAP = {"door": "connected_by_door",
            "open_passage": "open_passage",
            "vertical": "vertically_connected",
            # The annotator writes the pipeline's own relation names.
            "connected_by_door": "connected_by_door",
            "vertically_connected": "vertically_connected"}


def f1(p, r) -> float:
    """Harmonic mean of precision and recall, 0.0 when either is 0 or None.

    Exported so that nothing else in the project writes this expression again.
    Three scripts had their own copy, which is how a project ends up unsure
    whether a published figure was macro or micro averaged.
    """
    return (2 * p * r / (p + r)) if (p and r) else 0.0


_f1 = f1                              # internal alias, kept for readability


def predicted_pairs(g, matching, relations=CONNECTIVE) -> tuple:
    """Reference-space pairs the graph asserts, plus what could not be mapped.

    Returns `(typed, unmapped, internal)`:
      typed     -- pair -> set of relation names asserted for it
      unmapped  -- count of connective edges touching an unmatched room
      internal  -- count of edges whose endpoints matched the same room, which
                   is what a split prediction looks like after assignment
    """
    typed, unmapped, internal = {}, 0, 0
    seen = set()
    for u, v, k, d in g.edges(keys=True, data=True):
        rel = d.get("relation")
        if rel not in relations:
            continue
        # One physical transition is stored as two directed edges. Count the
        # transition once, not twice.
        #
        # A current graph gives both directions the same portal id, so the id
        # identifies the transition. A graph written before edges carried
        # identity has no id, and its two directions arrive under different
        # synthetic keys -- so there the undirected pair and relation are the
        # identity. That is exactly right for those files: the old builder could
        # not hold more than one edge per (pair, relation) anyway. Keying on the
        # synthetic key instead counted every legacy transition twice, which
        # doubled the tally of predictions hung on an unmatched room.
        ident = d.get("edge_id") or d.get("portal_id") or d.get("door")
        token = (frozenset((u, v)), rel, ident)
        if token in seen:
            continue
        seen.add(token)
        ra, rb = matching.assign.get(u), matching.assign.get(v)
        if ra is None or rb is None:
            unmapped += 1
            continue
        if ra == rb:
            internal += 1
            continue
        typed.setdefault(pair(ra, rb), set()).add(rel)
    return typed, unmapped, internal


def score_connectivity(g, gt, matching, relations=CONNECTIVE) -> dict:
    typed, unmapped, internal = predicted_pairs(g, matching, relations)
    pred = set(typed)

    truth = set(gt.positives)
    tp = pred & truth
    fn = truth - pred
    fp_negative = pred & gt.negatives
    fp_unknown = pred & gt.unknown
    fp_out = pred - gt.in_scope()

    # Precision over what the reference ruled on.
    denom_decided = len(tp) + len(fp_negative)
    precision = len(tp) / denom_decided if denom_decided else None
    # Precision that also charges for claims the reference never examined and
    # for edges hung on rooms that do not exist in it.
    denom_strict = denom_decided + len(fp_out) + unmapped
    precision_strict = len(tp) / denom_strict if denom_strict else None
    recall = len(tp) / len(truth) if truth else None

    # ---- edge type, on the pairs that matched -----------------------------
    type_ok = type_tot = 0
    confusion = Counter()
    recovered_by_type, total_by_type = Counter(), Counter()
    for p, declared in gt.positives.items():
        if declared is not None:
            total_by_type[declared] += 1
        if p not in pred:
            continue
        if declared is not None:
            recovered_by_type[declared] += 1
        want = TYPE_MAP.get(declared)
        if want is None:
            continue                     # reference states no type to check
        type_tot += 1
        got = typed[p]
        if got == {want}:
            type_ok += 1
        else:
            for other in sorted(got - {want}):
                confusion[f"{declared}->{other}"] += 1

    mc = matching.counts()
    # A predicted room that matched nothing is one of two very different
    # things, and reporting a single number for both flatters or punishes a
    # method at random.
    rejected = {n for n in matching.unmatched_pred if n in gt.rooms_rejected}
    return {
        "building": gt.building,
        "provenance": gt.provenance,
        "usable": gt.usable,
        "excluded_because": gt.excluded_because,
        # rooms
        "rooms_ref": len(gt.rooms),
        "rooms_pred": mc["matched"] + mc["unmatched_pred"],
        **{f"rooms_{k}": v for k, v in mc.items()},
        "rooms_confirmed_false": len(rejected),
        "rooms_unreviewed_pred": len(matching.unmatched_pred) - len(rejected),
        # pairs
        "gt_positives": len(truth),
        "gt_negatives": len(gt.negatives),
        "gt_unknown": len(gt.unknown),
        "pred_pairs": len(pred),
        "tp": len(tp),
        "fn": len(fn),
        "fp_on_reference_negative": len(fp_negative),
        "pred_on_unknown_pair": len(fp_unknown),
        "pred_out_of_scope": len(fp_out),
        "pred_unmatched_endpoint": unmapped,
        "pred_internal_to_one_room": internal,
        "precision": round(precision, 3) if precision is not None else None,
        "precision_strict": round(precision_strict, 3) if precision_strict is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(_f1(precision or 0, recall or 0), 3),
        "f1_strict": round(_f1(precision_strict or 0, recall or 0), 3),
        # type
        "type_exact": type_ok, "type_checked": type_tot,
        "type_accuracy": round(type_ok / type_tot, 3) if type_tot else None,
        "type_confusions": dict(confusion),
        "recall_by_type": {k: f"{recovered_by_type[k]}/{total_by_type[k]}"
                           for k in sorted(total_by_type)},
        # the bar a method has to clear: join every pair the reference examined
        "trivial_connect_all_precision": round(
            len(truth) / (len(truth) + len(gt.negatives)), 3)
        if (len(truth) + len(gt.negatives)) else None,
    }


def score_labels(g, gt, matching) -> dict:
    """Room-function accuracy over matched rooms, with the excluded rooms named.

    `Room.label` has been loaded by all three ground-truth dialects since M0 and
    read by nothing: label accuracy lived only in `main/eval_semantics.py`, with
    its own metric block and its own copy of the taxonomy. This is the scorer.

    The accounting follows the same rule as `score_connectivity`: a room is only
    scored where the reference actually states a function, and everything else is
    reported in its own bucket rather than quietly leaving the denominator.
    `evaluation.labels.classify` supplies the three reasons a room can be
    unscorable, and they mean different things:

      no_reference_label   the reference name carries no function ("Room",
                           "Raum", "Area"). There is no ground truth. Excluding
                           these is correct.
      reference_unmapped   a real function the twelve-class taxonomy cannot
                           express ("Gym", "Studio"). Excluding these overstates
                           coverage, so they are counted and must be quoted.
      reference_ambiguous  a combined-function room ("Living room / Kitchen").
                           Assigning one half would be inventing ground truth.

    A prediction is also charged when it is absent or outside the vocabulary:
    `pred_missing` and `pred_unmapped` keep a model that declines to answer from
    scoring the same as one that answers correctly.

    **Two accuracies, and the second is the comparable one.** Iterating the
    matching would make the denominator the model's own matching success: a
    model that places 2 of 27 rooms is scored on 2 labels, and those 2 are the
    ones it was confident enough to place. Measured on real runs, that inverts
    a comparison -- Qwen2.5-VL on a bare plan matched 12 of 27 rooms and
    scored 0.250, against InternVL3 on text+plan_som which matched 27 of 27 and
    scored 0.185, so the model scored on half as many rooms looked better at
    labelling. This iterates the *reference* instead, so:

      accuracy         correct / rooms that matched AND carry a class
                       -- "how good is it on rooms it found"
      accuracy_strict  correct / every reference room that carries a class
                       -- "how good is it on this building", comparable across
                          models because the denominator is the building's

    Exactly the distinction `score_connectivity` already draws between
    `precision` and `precision_strict`, applied to labels.
    """
    from . import labels as L

    rooms = gt.rooms                      # rid -> Room, per schema.GroundTruth
    per_class = Counter()
    correct_per_class = Counter()
    confusion = Counter()
    c = Counter()

    for rid, ref in rooms.items():
        node = matching.reverse.get(rid)
        outcome, truth, _ = L.classify(getattr(ref, "label", None))
        if outcome == L.UNNAMED:
            c["no_reference_label"] += 1
            continue
        if outcome == L.UNMAPPED:
            c["reference_unmapped"] += 1
            continue
        if outcome == L.AMBIGUOUS:
            c["reference_ambiguous"] += 1
            continue

        c["scored"] += 1
        per_class[truth] += 1
        if node is not None:
            c["scored_matched"] += 1

        attrs = g.nodes[node] if node in g.nodes else {}
        # ONLY `predicted_label`. Falling back to the node's own `label` would
        # score the IFC name against the IFC name and return near-perfect
        # accuracy for a method that predicted nothing -- the same shape as the
        # 380/380 edge-type tautology, where compose.py copied the pipeline's
        # relation and the scorer compared it to itself. No prediction is a
        # miss, not a free hit.
        raw = attrs.get("predicted_label")
        if raw in (None, ""):
            c["pred_missing"] += 1
            confusion[(truth, "<none>")] += 1
            continue
        p_outcome, pred, _ = L.classify(raw)
        if p_outcome != L.CLASS:
            # A model that emits an unusable string has not been correct. It is
            # charged, and separately from a wrong-but-valid class.
            c["pred_unmapped"] += 1
            confusion[(truth, f"<{p_outcome}>")] += 1
            continue
        if pred == truth:
            c["correct"] += 1
            correct_per_class[truth] += 1
        else:
            confusion[(truth, pred)] += 1

    scored = c["scored"]                  # every reference room with a class
    matched = c["scored_matched"]          # of those, the ones a prediction met
    majority = (max(per_class.values()) / scored) if scored else 0.0
    return {
        "vocab_version": L.VOCAB_VERSION,
        "scored": scored,
        "scored_matched": matched,
        "unmatched_reference_rooms": scored - matched,
        "correct": c["correct"],
        # Over rooms the prediction actually reached. NOT comparable between
        # models: the denominator is the model's own matching success.
        "accuracy": (c["correct"] / matched) if matched else 0.0,
        # Over every labelled room in the building, so the denominator belongs
        # to the building and not to the model. This is the comparable figure
        # and the one a cross-model table must use.
        "accuracy_strict": (c["correct"] / scored) if scored else 0.0,
        # The floor any real method must clear. Reported here so a caller cannot
        # quote an accuracy without it.
        "majority_baseline": majority,
        "pred_missing": c["pred_missing"],
        "pred_unmapped": c["pred_unmapped"],
        "no_reference_label": c["no_reference_label"],
        "reference_unmapped": c["reference_unmapped"],
        "reference_ambiguous": c["reference_ambiguous"],
        "per_class": {k: {"n": per_class[k], "correct": correct_per_class[k],
                          "recall": correct_per_class[k] / per_class[k]}
                      for k in sorted(per_class)},
        "confusions": [{"truth": t, "predicted": p, "n": n}
                       for (t, p), n in confusion.most_common(12)],
    }


def pool(rows: list) -> dict:
    """Micro-average over buildings, keeping every bucket separate."""
    keys = ("tp", "fn", "fp_on_reference_negative", "pred_out_of_scope",
            "pred_unmatched_endpoint", "pred_on_unknown_pair",
            "gt_positives", "gt_negatives", "gt_unknown",
            "rooms_unmatched_pred", "rooms_unmatched_gt", "rooms_matched",
            "rooms_confirmed_false", "rooms_unreviewed_pred",
            "rooms_merged_predictions", "rooms_split_predictions")
    agg = {k: sum(r.get(k) or 0 for r in rows) for k in keys}
    dd = agg["tp"] + agg["fp_on_reference_negative"]
    ds = dd + agg["pred_out_of_scope"] + agg["pred_unmatched_endpoint"]
    P = agg["tp"] / dd if dd else 0.0
    Ps = agg["tp"] / ds if ds else 0.0
    R = agg["tp"] / agg["gt_positives"] if agg["gt_positives"] else 0.0
    agg.update({
        "buildings": len(rows),
        "precision": round(P, 5), "precision_strict": round(Ps, 5),
        "recall": round(R, 5), "f1": round(_f1(P, R), 5),
        "f1_strict": round(_f1(Ps, R), 5),
        "macro_f1": round(sum(r["f1"] for r in rows) / len(rows), 5) if rows else 0.0,
    })
    return agg
