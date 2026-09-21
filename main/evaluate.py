"""Score the pipeline against human annotations, verdict by verdict.

This is what the annotation effort buys: the necessary-condition checks in
validate.py can falsify a graph but never confirm it, whereas these are genuine
precision/recall figures against human judgement.

It reads the vocabulary the annotator actually writes. The previous version
recognised `correct` / `spurious`, words the tool stopped writing before any of
the current 15 buildings were reviewed, so every verdict failed to match and the
score came out blank rather than wrong -- the worst failure mode available,
because a blank table looks like a small corpus rather than a broken join.

Three distinctions are kept throughout, because collapsing any of them turns a
partial review into a finished one:

* a **rejection** is evidence, and the only thing that makes precision
  measurable;
* an **unsure** verdict is not a rejection -- it is routed to a second reviewer;
* an **unjudged** item is not evidence at all, and never enters a denominator.

Rooms are also split by provenance -- stated by IFC versus recovered
geometrically -- which isolates the contribution of the recovery stage from what
the file already provided.

    python main/evaluate.py                                   # annotator/annotations
    python main/evaluate.py --annotations ifc-graphx/annotations
    python main/evaluate.py --annotator AN
    python main/evaluate.py --agreement
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from evaluation.verdicts import (NEGATIVE, PARTIAL, POSITIVE, UNKNOWN,
                                 RetiredVerdict, link_outcome, room_outcome)
from evaluation.manifest import RunManifest

ROOT = os.path.dirname(HERE)
# Resolved against the repository, not the caller's working directory. A
# relative default meant that running this from anywhere else reported every
# building as missing -- which is at least visible now, but is still the wrong
# answer.
ANNO_DIR = os.path.join(ROOT, "annotator", "annotations")
PLAN_DIR = os.path.join(ROOT, "annotator", "data")


def load_plan(model):
    p = os.path.join(PLAN_DIR, f"{model}.plan.json")
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return json.load(fh)


def room_provenance(plan):
    out = {}
    for st in plan["storeys"]:
        for r in st["rooms"]:
            out[r["id"]] = r["source"]
    return out


def read_annotations(path: str) -> list:
    """Every annotation under a directory, a bundle, or a single file.

    Annotators on the browser-only build return one bundle holding every
    building they reviewed. Reading only loose per-model files meant the
    scorer saw none of the current corpus.
    """
    paths = ([path] if os.path.isfile(path)
             else sorted(glob.glob(os.path.join(path, "*.json"))))
    out = []
    for p in paths:
        with open(p) as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            continue
        bundle = d.get("annotations")
        if isinstance(bundle, dict):
            bundle = [{**a, "model": a.get("model", m)}
                      for m, a in sorted(bundle.items())]
        if isinstance(bundle, list):
            # A bundle carries the annotator once, at the top; the entries
            # inside may or may not repeat it.
            who = d.get("annotator")
            for anno in bundle:
                anno = dict(anno)
                anno.setdefault("annotator", who)
                out.append((f"{p}#{anno.get('model')}", anno))
        elif d.get("model"):
            out.append((p, d))
    return out


def _tally(entries, outcome) -> dict:
    c = {POSITIVE: 0, NEGATIVE: 0, PARTIAL: 0, UNKNOWN: 0}
    for e in entries:
        o = outcome(e)
        if o in c:
            c[o] += 1
    return c


def score_one(anno: dict, plan=None) -> dict | None:
    model, who = anno.get("model"), anno.get("annotator")
    plan = plan if plan is not None else load_plan(model)
    if plan is None:
        return None
    prov = room_provenance(plan)

    r = {"model": model, "annotator": who}

    # ---- rooms, split by provenance ----
    for bucket in ("all", "ifc", "inferred"):
        entries = [v for rid, v in (anno.get("rooms") or {}).items()
                   if bucket == "all" or prov.get(rid) == bucket]
        c = _tally(entries, room_outcome)
        # A rejection is a judgement about a region that is there; precision is
        # over what the reviewer ruled on, and never over what they skipped.
        judged = c[POSITIVE] + c[NEGATIVE] + c[PARTIAL]
        prec = c[POSITIVE] / judged if judged else None
        missing = len(anno.get("missing_rooms") or []) if bucket == "all" else 0
        denom = c[POSITIVE] + c[PARTIAL] + missing
        rec = c[POSITIVE] / denom if (bucket == "all" and denom) else None
        r[f"rooms_{bucket}"] = {
            "judged": judged, "tp": c[POSITIVE], "fp": c[NEGATIVE],
            "partial": c[PARTIAL], "unsure": c[UNKNOWN],
            "precision": round(prec, 3) if prec is not None else None,
            "recall": round(rec, 3) if rec is not None else None,
        }
    r["missing_rooms"] = len(anno.get("missing_rooms") or [])

    # ---- connectivity: horizontal and vertical, scored separately ----
    #
    # The old scorer read `edges` alone. The floor-to-floor verdicts live in
    # `vertical`, and they are the links that chain a building into one
    # routable object -- omitting them scored the easy half.
    for kind, key, added_key in (("edges", "edges", "added_edges"),
                                 ("vertical", "vertical", "added_vertical")):
        c = _tally(list((anno.get(key) or {}).values()), link_outcome)
        added = len(anno.get(added_key) or [])
        judged = c[POSITIVE] + c[NEGATIVE]
        r[kind] = {
            "judged": judged, "tp": c[POSITIVE], "fp": c[NEGATIVE],
            "unsure": c[UNKNOWN], "added": added,
            "precision": round(c[POSITIVE] / judged, 3) if judged else None,
            # A link the reviewer had to add is one the pipeline missed.
            "recall": round(c[POSITIVE] / (c[POSITIVE] + added), 3)
            if (c[POSITIVE] + added) else None,
        }

    # ---- label accuracy ----
    orig = {rm["id"]: rm["label"] for st in plan["storeys"] for rm in st["rooms"]}
    lab_tot = lab_ok = 0
    for rid, v in (anno.get("rooms") or {}).items():
        if "label" not in v:
            continue
        lab_tot += 1
        if v["label"].strip().lower() == (orig.get(rid, "") or "").strip().lower():
            lab_ok += 1
    r["labels"] = {"checked": lab_tot,
                   "accuracy": round(lab_ok / lab_tot, 3) if lab_tot else None}

    # ---- how much of this building was actually reviewed ----
    n_rooms = sum(len(st["rooms"]) for st in plan["storeys"])
    n_links = sum(len(st["edges"]) for st in plan["storeys"])
    n_vert = len(plan.get("vertical") or [])
    r["coverage"] = {
        "rooms": f"{r['rooms_all']['judged'] + r['rooms_all']['unsure']}/{n_rooms}",
        "links": f"{r['edges']['judged'] + r['edges']['unsure']}/{n_links}",
        "vertical": f"{r['vertical']['judged'] + r['vertical']['unsure']}/{n_vert}",
        "complete": (r["rooms_all"]["judged"] + r["rooms_all"]["unsure"] == n_rooms
                     and r["edges"]["judged"] + r["edges"]["unsure"] == n_links
                     and r["vertical"]["judged"] + r["vertical"]["unsure"] == n_vert),
    }
    # Bulk verdicts are ground truth, but they were swept in rather than looked
    # at one by one, and a reader should be able to weigh that.
    r["bulk"] = sum(1 for v in (anno.get("rooms") or {}).values() if v.get("bulk")) \
        + sum(1 for v in (anno.get("edges") or {}).values() if v.get("bulk"))
    return r


def agreement(annos: list):
    """Inter-annotator agreement on items judged by two or more people.

    This characterises how uncertain the labels are. It is **not** a ceiling on
    model accuracy: two reviewers disagreeing about a corridor does not make the
    building ambiguous, and a method can be right where one of them was wrong.
    """
    by_model = defaultdict(list)
    for _, a in annos:
        by_model[a.get("model")].append(a)

    rows = []
    for model, group in by_model.items():
        if len(group) < 2:
            continue
        for x, y in itertools.combinations(group, 2):
            for kind in ("rooms", "edges", "vertical"):
                shared = set(x.get(kind) or {}) & set(y.get(kind) or {})
                if not shared:
                    continue
                same = sum(1 for k in shared
                           if x[kind][k].get("verdict") == y[kind][k].get("verdict"))
                rows.append({
                    "model": model, "kind": kind,
                    "pair": f"{x.get('annotator')}/{y.get('annotator')}",
                    "overlap": len(shared),
                    "agreement": round(same / len(shared), 3),
                })
    return rows


def main():
    global PLAN_DIR
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--annotations", default=ANNO_DIR,
                    help="directory, bundle file, or single annotation")
    ap.add_argument("--plans", default=PLAN_DIR,
                    help="exported plans the annotations refer to")
    ap.add_argument("--annotator")
    ap.add_argument("--agreement", action="store_true")
    ap.add_argument("--out", default="results/evaluation.json")
    ap.add_argument("--allow-missing", action="store_true")
    args = ap.parse_args()
    PLAN_DIR = args.plans

    if not os.path.exists(args.annotations):
        print(f"no annotations at {args.annotations}")
        print("Run the annotator first:  python annotator/app.py")
        return 2
    annos = read_annotations(args.annotations)
    if args.annotator:
        annos = [(p, a) for p, a in annos if a.get("annotator") == args.annotator]
    if not annos:
        print(f"no annotations found in {args.annotations}")
        return 2

    if args.agreement:
        rows = agreement(annos)
        if not rows:
            print("No model has been annotated by two or more people yet.")
            print("Agreement needs at least one model double-annotated.")
            return 0
        print(f"{'model':<12}{'kind':<8}{'pair':<22}{'overlap':>8}{'agree':>8}")
        for r in rows:
            print(f"{r['model']:<12}{r['kind']:<8}{r['pair']:<22}"
                  f"{r['overlap']:>8}{r['agreement']:>8.3f}")
        print("\nAgreement characterises label uncertainty. It is not a ceiling "
              "on model accuracy.")
        return 0

    man = RunManifest("evaluate", expected=[f"{a.get('model')}__{a.get('annotator')}"
                                            for _, a in annos],
                      config={"annotations": args.annotations})
    rows = []
    for src, a in annos:
        unit = f"{a.get('model')}__{a.get('annotator')}"
        try:
            r = score_one(a)
        except RetiredVerdict as exc:
            man.failed(unit, str(exc))
            continue
        if r is None:
            man.missing(unit, f"no exported plan for {a.get('model')} in {PLAN_DIR}")
            continue
        rows.append(r)
        man.ok(unit, complete=r["coverage"]["complete"])

    print(man.report())
    if not rows:
        print("No annotation matched an exported plan.")
        return 1

    print()
    print(f"{'model':<11}{'by':<6}{'rm judged':>10}{'prec':>7}{'rec':>7}"
          f"{'  |':>3}{'ifc prec':>9}{'inf prec':>9}{'  |':>3}"
          f"{'links':>7}{'prec':>7}{'rec':>7}{'  |':>3}{'vert':>6}{'prec':>7}"
          f"{'  |':>3}{'reviewed':>9}")
    print("-" * 116)
    f = lambda v: f"{v:.3f}" if isinstance(v, float) else "  -  "
    for r in rows:
        ra, ri, rf = r["rooms_all"], r["rooms_ifc"], r["rooms_inferred"]
        e, vt = r["edges"], r["vertical"]
        print(f"{r['model']:<11}{str(r['annotator'])[:5]:<6}{ra['judged']:>10}"
              f"{f(ra['precision']):>7}{f(ra['recall']):>7}{'  |':>3}"
              f"{f(ri['precision']):>9}{f(rf['precision']):>9}{'  |':>3}"
              f"{e['judged']:>7}{f(e['precision']):>7}{f(e['recall']):>7}{'  |':>3}"
              f"{vt['judged']:>6}{f(vt['precision']):>7}{'  |':>3}"
              f"{('yes' if r['coverage']['complete'] else 'partial'):>9}")

    def agg(key, sub):
        return sum(r[key][sub] for r in rows)

    rtp, rfp, rpart = (agg("rooms_all", "tp"), agg("rooms_all", "fp"),
                       agg("rooms_all", "partial"))
    runs = agg("rooms_all", "unsure")
    etp, efp, euns = agg("edges", "tp"), agg("edges", "fp"), agg("edges", "unsure")
    vtp, vfp = agg("vertical", "tp"), agg("vertical", "fp")
    miss = sum(r["missing_rooms"] for r in rows)
    eadd, vadd = agg("edges", "added"), agg("vertical", "added")

    print("\n--- CORPUS TOTALS ---")
    print(f"  rooms judged      {rtp + rfp + rpart}  "
          f"(real {rtp}, rejected {rfp}, merge/split {rpart}; "
          f"{runs} unsure, {miss} missed)")
    if rtp + rfp + rpart:
        print(f"  room precision    {rtp / (rtp + rfp + rpart):.3f}")
    if rtp + rpart + miss:
        print(f"  room recall       {rtp / (rtp + rpart + miss):.3f}")
    print(f"  links judged      {etp + efp}  (passable {etp}, rejected {efp}; "
          f"{euns} unsure, {eadd} added by hand)")
    if etp + efp:
        print(f"  link precision    {etp / (etp + efp):.3f}")
    if etp + eadd:
        print(f"  link recall       {etp / (etp + eadd):.3f}")
    print(f"  floor links       {vtp + vfp} judged (passable {vtp}, rejected "
          f"{vfp}, {vadd} added by hand)")
    if vtp + vfp:
        print(f"  vertical prec     {vtp / (vtp + vfp):.3f}")
    bulk = sum(r["bulk"] for r in rows)
    judged_items = rtp + rfp + rpart + etp + efp
    if judged_items:
        print(f"  swept in bulk     {bulk}/{judged_items} "
              f"({bulk / judged_items:.0%} of judged items were accepted with "
              f"'rest of floor is correct')")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"schema": 2, "per_annotation": rows,
                   "manifest": man.to_dict()}, fh, indent=2)
    mp = args.out.replace(".json", "") + ".manifest.json"
    man.write(mp)
    print(f"\nwrote {args.out}\nwrote {mp}")
    print("\nNOTE: these are only as complete as the annotation. Check the "
          "'reviewed' column\n      before quoting any figure, and report the "
          "bulk fraction alongside it.")
    return 0 if (man.complete or args.allow_missing) else 1


if __name__ == "__main__":
    sys.exit(main())
