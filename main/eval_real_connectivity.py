"""Score recovered connectivity against the derived real-corpus reference.

Companion to `eval_connectivity.py`, which scores against generator ground truth
on synthetic buildings. Here the reference comes from `derive_real_gt.py`, so
two things differ and both constrain what may be claimed.

First, scope. The reference decides only *candidate-adjacent* room pairs -- ones
whose footprints lie within `ADJ_TOL` of each other. It says nothing about a
pair at opposite ends of a floor, so a prediction there is neither right nor
wrong by this reference. Those are reported separately as `fp_out_of_scope`
rather than folded into precision, which would punish a method for an edge the
reference never examined. Reporting them separately keeps the headline number
honest without letting a method predict everything for free.

Second, provenance. The reference is derived, not authored: it inherits the
flood-fill's own error, which measured at precision 1.000 / recall 1.000 on the
synthetic corpus where truth is known (`--calibrate`). That is a bound
established on clean generated geometry and it will not hold exactly on real
files. A score here is agreement between two independent geometric readings of
the same building, which is weaker evidence than agreement with an annotator,
and should be reported as such.

    python main/eval_real_connectivity.py --gt results/real_gt \\
        --graphs results/bl_real/ours --out results/real_connectivity_eval.json

Third: this scorer predates `evaluation/`. It is kept because its room
contraction answers a question the unified scorer does not -- "can you get from
A to B without passing through another *stated* room" -- but it is not
interchangeable with `main/eval_connectivity.py`, which now reads this same
`.realgt.json` dialect through `evaluation.schema`. Quote which one produced a
number.

Note also that any reference under `results/real_gt/` built before 14 September
2026 is stale: `derive_real_gt` was scaling tessellated geometry by the declared
unit a second time, which discarded every doorway void in the seven
millimetre-declaring models. Rebuild before using.
"""

from __future__ import annotations

import argparse
import glob
import json
import itertools
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import networkx as nx

from bimsg import graph as G

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evaluation.manifest import RunManifest
from evaluation.scoring import f1

CONNECTIVE = {"connected_by_door", "open_passage", "vertically_connected",
              "adjacent_to"}
# `adjacent_to` states that two rooms share a wall, not that you can walk
# between them, so it is not a connectivity claim and is scored as one only if
# a method offers nothing else. Kept out of the default set deliberately.
TRAVERSABLE = {"connected_by_door", "open_passage"}


def pred_pairs(g, rids: set, contract: bool = True) -> set:
    """Traversable room pairs the graph asserts, in the reference's room set.

    The two room sets are not the same and cannot be made the same. The
    reference knows only the rooms the IFC states; the pipeline additionally
    recovers rooms the file omits, and then routes connectivity through them --
    a doorway from a flat into a stairwell the file never declared becomes
    `flat -> recovered corridor -> stair` rather than a single edge. Scoring
    only literal room-to-room edges marks all of that wrong: on `model_0`, 14
    of 16 apparently missing reference edges are present as a path through a
    recovered room.

    So the comparable question is not "does an edge join A and B" but "does the
    graph assert you can get from A to B without passing through another
    *stated* room". Recovered rooms are contracted away and the reference rooms
    they join are linked. `contract=False` recovers the literal-edge reading,
    which is reported alongside so the effect of this choice stays visible.

    Contraction is not free: one sprawling recovered room touching everything
    would connect everything through it. That failure shows up as precision
    against the reference's separated pairs, which is why both are reported.
    """
    out = set()
    inferred = []
    for n, d in g.nodes(data=True):
        if d.get("layer") != "space":
            continue
        if n not in rids:
            inferred.append(n)

    T = nx.Graph()
    T.add_nodes_from(rids)
    for u, v, d in g.edges(data=True):
        if d.get("relation") in TRAVERSABLE and u != v:
            T.add_edge(u, v)

    for u, v in T.edges():
        if u in rids and v in rids:
            out.add(frozenset((u, v)))
    if not contract:
        return out

    # Every reference room touching one recovered-room component is mutually
    # reachable without crossing a stated room.
    inf_present = [n for n in inferred if n in T]
    for comp in nx.connected_components(T.subgraph(inf_present)):
        touching = {r for n in comp for r in T.neighbors(n) if r in rids}
        for a, b in itertools.combinations(sorted(touching), 2):
            out.add(frozenset((a, b)))
    return out


def score_one(gt_path: str, graph_path: str) -> dict | None:
    ref = json.load(open(gt_path))
    if not ref.get("derivable"):
        return None
    g = G.load(graph_path)

    rids = {r["rid"] for r in ref["rooms"]}
    truth = {frozenset((e["a"], e["b"])) for e in ref["edges"]}
    negatives = {frozenset((e["a"], e["b"])) for e in ref["adjacent_not_connected"]}
    in_scope = truth | negatives

    pred = pred_pairs(g, rids, contract=True)
    lit = pred_pairs(g, rids, contract=False)
    tp = pred & truth
    fn = truth - pred
    fp_in = (pred & negatives)            # crossed a wall the raster found solid
    fp_out = pred - in_scope - truth      # pair the reference never examined

    prec = len(tp) / (len(tp) + len(fp_in)) if tp or fp_in else 0.0
    rec = len(tp) / len(truth) if truth else 0.0
    score = f1(prec, rec)

    # How much of the reference's room set the graph even represents. A method
    # scoring well on a third of the rooms is not scoring well on the building.
    covered = len(rids & set(g.nodes))
    # Rooms the pipeline recovered that the file never stated. These are what
    # contraction routes through, and when one is a large circulation blob it
    # can link many rooms at once -- so the count is reported per building and
    # the subset with none of them is summarised separately.
    extra = sum(1 for n, d in g.nodes(data=True)
                if d.get("layer") == "space" and n not in rids)
    return {
        "building": ref["building"],
        "rooms_ref": len(rids), "rooms_matched": covered, "rooms_extra": extra,
        "truth": len(truth), "negatives": len(negatives),
        "tp": len(tp), "fp": len(fp_in), "fn": len(fn),
        "fp_out_of_scope": len(fp_out),
        "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(score, 3),
        # The same score without contracting recovered rooms, so the effect of
        # that choice is visible rather than buried in the headline number.
        "literal_tp": len(lit & truth), "literal_fp": len(lit & negatives),
        "literal_recall": round(len(lit & truth) / len(truth), 3) if truth else 0.0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt", default="results/real_gt")
    ap.add_argument("--graphs", default="results/bl_real/ours")
    ap.add_argument("--out", default="results/real_connectivity_eval.json")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--allow-missing", action="store_true")
    args = ap.parse_args()

    gt_paths = sorted(glob.glob(os.path.join(args.gt, "*.realgt.json")))
    # The denominator is fixed before anything is scored: a building whose graph
    # was never built is a failed run, not a smaller corpus.
    man = RunManifest("eval_real_connectivity",
                      expected=[os.path.basename(p).replace(".realgt.json", "")
                                for p in gt_paths],
                      config={"gt": args.gt, "graphs": args.graphs})
    rows, skipped, missing = [], 0, 0
    for gt_path in gt_paths:
        name = os.path.basename(gt_path).replace(".realgt.json", "")
        gp = os.path.join(args.graphs, f"{name}.graph.json")
        if not os.path.exists(gp):
            missing += 1
            man.missing(name, f"no graph at {gp}")
            continue
        r = score_one(gt_path, gp)
        if r is None:
            skipped += 1
            man.excluded(name, "the reference's own trust gate rejected it")
            continue
        rows.append(r)
        man.ok(name, f1=r["f1"])
        if not args.quiet:
            print(f"  {r['building']:<16} P {r['precision']:.3f}  R {r['recall']:.3f}"
                  f"  F1 {r['f1']:.3f}   tp{r['tp']} fp{r['fp']} fn{r['fn']}"
                  f"  oos{r['fp_out_of_scope']}  rooms {r['rooms_matched']}/{r['rooms_ref']}")

    agg = Counter()
    for r in rows:
        for k in ("tp", "fp", "fn", "fp_out_of_scope", "truth", "negatives"):
            agg[k] += r[k]
    P = agg["tp"] / (agg["tp"] + agg["fp"]) if agg["tp"] + agg["fp"] else 0.0
    R = agg["tp"] / agg["truth"] if agg["truth"] else 0.0
    F = f1(P, R)
    macro = (sum(r["f1"] for r in rows) / len(rows)) if rows else 0.0

    print(f"\nREAL-CORPUS CONNECTIVITY  ({len(rows)} scorable models)")
    print(f"  pooled   precision {P:.3f}  recall {R:.3f}  F1 {F:.3f}")
    print(f"  macro    mean per-building F1 {macro:.3f}")
    print(f"  tp {agg['tp']}  fp {agg['fp']}  fn {agg['fn']}"
          f"  out-of-scope predictions {agg['fp_out_of_scope']}")
    print(f"  reference: {agg['truth']} connected, {agg['negatives']} separated pairs")
    lit_tp = sum(r["literal_tp"] for r in rows)
    lit_fp = sum(r["literal_fp"] for r in rows)
    lit_p = lit_tp / (lit_tp + lit_fp) if lit_tp + lit_fp else 0.0
    lit_r = lit_tp / agg["truth"] if agg["truth"] else 0.0
    print(f"  without contracting recovered rooms: precision {lit_p:.3f}"
          f"  recall {lit_r:.3f}  (tp {lit_tp} fp {lit_fp})")
    clean = [r for r in rows if r["rooms_extra"] == 0]
    if clean:
        ctp = sum(r["tp"] for r in clean); cfp = sum(r["fp"] for r in clean)
        ctruth = sum(r["truth"] for r in clean)
        cP = ctp / (ctp + cfp) if ctp + cfp else 0.0
        cR = ctp / ctruth if ctruth else 0.0
        cF = f1(cP, cR)
        print(f"  matched room sets only ({len(clean)} models, no recovered rooms):"
              f" precision {cP:.3f}  recall {cR:.3f}  F1 {cF:.3f}")
    if skipped:
        print(f"  {skipped} models excluded by the reference's own trust gate")
    if missing:
        print(f"  {missing} models had no graph built -- they stay in the "
              f"denominator ({man.denominator()} expected)")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump({"per_building": rows,
               "pooled": {"precision": round(P, 3), "recall": round(R, 3),
                          "f1": round(F, 3), "macro_f1": round(macro, 3), **dict(agg)},
               "skipped_not_derivable": skipped, "missing_graphs": missing,
               "manifest": man.to_dict()},
              open(args.out, "w"), indent=2)
    mp = args.out.replace(".json", "") + ".manifest.json"
    man.write(mp)
    print(f"  -> {args.out}\n  -> {mp}")
    if not man.complete and not args.allow_missing:
        print("\nFAILED: buildings are missing from this corpus. Build them, or "
              "pass --allow-missing and say so wherever the score is quoted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
