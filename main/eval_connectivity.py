"""Score recovered connectivity against a reference, whatever wrote it.

This is the primary metric: the claim is connectivity recovery from IFC that
states none, so the number that matters is how many room-to-room transitions
were recovered and how many were invented.

It reads generator ground truth, the derived flood-fill reference and human
annotation through one contract (`evaluation.schema`), because it previously
read only the first and raised `KeyError` on the third -- the one produced by
the annotation effort it was supposed to measure.

Three properties make the score meaningful, and the first two were absent:

* **Rooms are matched one to one.** Several recovered rooms could previously
  stand for one reference room, and a recovered room matching nothing left the
  scoring set entirely, taking its edges with it. An invented room and its
  invented edge then cost a method nothing. Every predicted room is now matched
  to at most one reference room, and what does not match is still counted.
* **Unknown is not negative.** Pairs a reference abstained on, and links a
  human marked unsure, are reported apart and never enter precision.
* **Types are retained, not collapsed.** A doorway is a gap in a wall, so a door
  can also be reported as an open passage. The pair is right either way, while
  the edge type -- which tells a planner whether the opening can be shut -- is
  wrong.

    python main/eval_connectivity.py --gt dataset/synthetic_data/synthetic --graphs results/synthetic
    python main/eval_connectivity.py --gt ifc-graphx/annotated_gt --graphs results --pattern '*.gt.json'
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from bimsg import graph as G
from evaluation import load_gt, match_rooms, score_connectivity, RunManifest
from evaluation.scoring import pool

# Edge relations that assert traversability between two rooms.
CONNECTIVE = ("connected_by_door", "open_passage", "vertically_connected")


def score_one(gt_path: str, graph_path: str) -> dict:
    gt = load_gt(gt_path)
    g = G.load(graph_path)
    matching = match_rooms(g, gt)
    row = score_connectivity(g, gt, matching, CONNECTIVE)
    row["graph"] = os.path.basename(graph_path)
    row["gt"] = os.path.basename(gt_path)
    return row


def _unit(gt_path: str) -> str:
    """The building this ground-truth file is about."""
    name = os.path.basename(gt_path)
    for suffix in (".gt.json", ".realgt.json", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt", default="dataset/synthetic_data/synthetic")
    ap.add_argument("--graphs", default="results/synthetic")
    ap.add_argument("--pattern", default="*.gt.json",
                    help="ground-truth filenames inside --gt")
    ap.add_argument("--out", default="results/connectivity_eval.json")
    ap.add_argument("--manifest", default=None,
                    help="where to write the run manifest "
                         "(default: alongside --out)")
    ap.add_argument("--allow-missing", action="store_true",
                    help="exit 0 even when an expected graph was never built. "
                         "Off by default: a missing run is a failed run, not a "
                         "smaller corpus.")
    args = ap.parse_args()

    gt_paths = sorted(glob.glob(os.path.join(args.gt, args.pattern)))
    if not gt_paths:
        print(f"no ground truth matching {args.pattern} in {args.gt}")
        return 2

    # The denominator is fixed here, before anything is scored.
    man = RunManifest(
        "eval_connectivity",
        expected=[_unit(p) for p in gt_paths],
        config={"gt": args.gt, "graphs": args.graphs, "pattern": args.pattern},
    )

    rows = []
    for gt_path in gt_paths:
        name = _unit(gt_path)
        # The annotator suffixes its exports with the annotator's name; the
        # graph it judged carries the plain model name.
        stem = name.split("__")[0]
        candidates = [os.path.join(args.graphs, f"{name}.graph.json"),
                      os.path.join(args.graphs, f"{stem}.graph.json")]
        gp = next((c for c in candidates if os.path.exists(c)), None)
        if gp is None:
            man.missing(name, f"no graph at {candidates[0]}")
            continue
        try:
            row = score_one(gt_path, gp)
        except Exception as exc:                     # noqa: BLE001
            man.failed(name, f"{type(exc).__name__}: {exc}")
            continue
        if not row["usable"]:
            man.excluded(name, row["excluded_because"] or
                         "the reference declared itself unusable")
            continue
        rows.append(row)
        man.ok(name, f1=row["f1"], tp=row["tp"], fn=row["fn"])

    print(man.report())
    if not rows:
        print("nothing scored")
        _write(args, rows, man)
        return 1

    hdr = (f"{'building':<16}{'GT':>4}{'neg':>5}{'unk':>5}{'pred':>6}{'TP':>4}"
           f"{'FP':>4}{'FN':>4}{'prec':>7}{'rec':>7}{'F1':>7}{'strict':>8}"
           f"{'rooms':>12}")
    print()
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['building'][:15]:<16}{r['gt_positives']:>4}{r['gt_negatives']:>5}"
              f"{r['gt_unknown']:>5}{r['pred_pairs']:>6}{r['tp']:>4}"
              f"{r['fp_on_reference_negative']:>4}{r['fn']:>4}"
              f"{(r['precision'] or 0):>7.3f}{(r['recall'] or 0):>7.3f}"
              f"{r['f1']:>7.3f}{r['f1_strict']:>8.3f}"
              f"{r['rooms_matched']:>6}/{r['rooms_ref']:<5}")

    agg = pool(rows)
    print(f"\n--- MICRO-AVERAGED over {agg['buildings']} buildings ---")
    print(f"  precision {agg['precision']:.3f}   recall {agg['recall']:.3f}"
          f"   F1 {agg['f1']:.3f}")
    print(f"  strict (charging out-of-scope and unmatched-endpoint claims): "
          f"precision {agg['precision_strict']:.3f}  F1 {agg['f1_strict']:.3f}")
    print(f"  macro mean per-building F1 {agg['macro_f1']:.3f}")
    print(f"  tp {agg['tp']}  fn {agg['fn']}  "
          f"fp on a reference negative {agg['fp_on_reference_negative']}")
    print(f"  predictions the reference never examined: {agg['pred_out_of_scope']}"
          f"   on an unknown pair: {agg['pred_on_unknown_pair']}"
          f"   hung on an unmatched room: {agg['pred_unmatched_endpoint']}")
    print(f"  rooms: {agg['rooms_matched']} matched, "
          f"{agg['rooms_unmatched_gt']} reference rooms unmatched "
          f"({agg['rooms_merged_predictions']} merged, "
          f"{agg['rooms_split_predictions']} split)")
    print(f"  predicted rooms with no reference room: "
          f"{agg['rooms_confirmed_false']} a reviewer rejected, "
          f"{agg['rooms_unreviewed_pred']} nobody judged")

    triv = [r["trivial_connect_all_precision"] for r in rows
            if r["trivial_connect_all_precision"] is not None]
    if triv:
        print(f"  connect-every-examined-pair baseline would score precision "
              f"{sum(triv)/len(triv):.3f} at recall 1.000")

    tok = sum(r["type_exact"] for r in rows)
    ttot = sum(r["type_checked"] for r in rows)
    if ttot:
        print(f"  edge-type accuracy (matched edges): {tok}/{ttot} = "
              f"{tok/ttot:.3f}")
    conf = Counter()
    for r in rows:
        conf.update(r["type_confusions"])
    if conf:
        print("  type confusions: "
              + ", ".join(f"{k} x{v}" for k, v in conf.most_common(5)))

    _write(args, rows, man)
    if not man.complete and not args.allow_missing:
        print("\nFAILED: the corpus is incomplete. Re-run the missing "
              "buildings, or pass --allow-missing to accept a partial score "
              "and say so wherever it is quoted.")
        return 1
    return 0


def _write(args, rows, man):
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    payload = {"schema": 2, "per_building": rows,
               "pooled": pool(rows) if rows else {},
               "manifest": man.to_dict()}
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2)
    mp = args.manifest or os.path.join(
        os.path.dirname(args.out) or ".",
        os.path.basename(args.out).replace(".json", "") + ".manifest.json")
    man.write(mp)
    print(f"\nwrote {args.out}\nwrote {mp}")


if __name__ == "__main__":
    sys.exit(main())
