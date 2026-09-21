"""End-to-end goal resolution: does "go to the kitchen" find the kitchen?

Everything measured so far scores one stage in isolation -- connectivity
recall, or label accuracy on a held-out room. Nothing checks the thing a
robot planner actually needs: given a functional query, does the pipeline
point it at the right room, and can it then actually get there?

Reuses eval_semantics.py's leave-one-out discipline: for each real room whose
IFC-stated label is known, that label is hidden, the RuleBackend predicts
one from graph context alone (as it would for a room the pipeline recovered
and nobody ever named), and the query is the room's true canonical class.
Success is the true room appearing in `qa.BuildingIndex.find_rooms`'s
candidate set for that query -- resolved using the masked room's PREDICTED
label, exactly as a deployed system would see it.

A second pass plans a route to every resolved room under each robot profile
(wheeled/legged/human), closing the loop from language query to a concrete,
embodiment-aware plan.

    python main/eval_goal_resolution.py
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import navigation as N
from bimsg import qa as QA
from bimsg import semantics as S
from eval_semantics import collect


def _entry_room(g) -> str | None:
    """A plausible route start: the largest circulation room, else any room."""
    idx = QA.BuildingIndex(g)
    circ = [(d.get("area") or 0, n) for n, d in idx.spaces.items()
            if S.canonical(d.get("label")) == "circulation"
            or d.get("predicted_label") == "circulation"]
    if circ:
        return max(circ)[1]
    any_room = next(iter(idx.spaces), None)
    return any_room


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphs", default="results/model_*.graph.json")
    ap.add_argument("--out", default="results/goal_resolution_eval.json")
    args = ap.parse_args()

    items = collect(args.graphs)
    by_model = defaultdict(list)
    for model, node, feats, truth in items:
        by_model[model].append((node, feats, truth))
    print(f"held-out rooms: {len(items)}  ({len(by_model)} models)")

    backend = S.RuleBackend()
    path_by_model = {os.path.basename(p): p
                      for p in sorted(glob.glob(args.graphs))}
    graphs = {model: G.load(path_by_model[model]) for model in by_model}

    resolved = 0
    total = 0
    n_candidates_hist = Counter()
    per_class = defaultdict(lambda: [0, 0])   # class -> [hit, total]
    reach = defaultdict(lambda: [0, 0])       # profile -> [reachable, planned]
    examples = []

    for model, rooms in by_model.items():
        g = graphs[model]
        entry = _entry_room(g)
        for node, feats, truth in rooms:
            kept_label = g.nodes[node].get("label")
            kept_pred = g.nodes[node].get("predicted_label")
            g.nodes[node]["label"] = "room"
            g.nodes[node]["predicted_label"] = backend.predict(feats)

            idx = QA.BuildingIndex(g)
            cand = idx.find_rooms(truth)
            total += 1
            per_class[truth][1] += 1
            n_candidates_hist[min(len(cand), 5)] += 1
            hit = node in cand
            if hit:
                resolved += 1
                per_class[truth][0] += 1
                if entry and entry != node:
                    for prof in N.PROFILES:
                        p = N.plan(g, entry, node, profile=prof)
                        reach[prof][1] += 1
                        if p is not None:
                            reach[prof][0] += 1
                if len(examples) < 12:
                    examples.append({
                        "model": model, "query": truth,
                        "n_candidates": len(cand), "resolved": True})
            elif len(examples) < 20:
                examples.append({
                    "model": model, "query": truth,
                    "n_candidates": len(cand), "resolved": False})

            g.nodes[node]["label"] = kept_label
            g.nodes[node]["predicted_label"] = kept_pred

    acc = resolved / total if total else 0.0
    print(f"\ngoal resolution: {resolved}/{total} = {acc:.3f}")
    print(f"  candidate-set size (query hits, capped at 5+): {dict(n_candidates_hist)}")
    print(f"\n  {'class':<14}{'recall':>9}{'n':>7}")
    for c, (h, t) in sorted(per_class.items(), key=lambda x: -x[1][1]):
        print(f"  {c:<14}{h/t:>9.3f}{t:>7}")

    print(f"\n  downstream: route from building entry to resolved goal")
    for prof, (h, t) in reach.items():
        print(f"    {prof:<10} {h}/{t} = {h/t if t else 0:.3f} reachable")

    rec = {
        "n": total, "resolved": resolved, "accuracy": acc,
        "candidate_hist": dict(n_candidates_hist),
        "per_class": {c: {"recall": h / t, "n": t}
                      for c, (h, t) in per_class.items()},
        "reachability_from_entry": {
            prof: {"reachable": h, "planned": t, "fraction": h / t if t else 0}
            for prof, (h, t) in reach.items()},
        "examples": examples,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(rec, open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")
    print("\nNOTE: other rooms in the same building keep their real/predicted\n"
          "label while one room is held out -- this measures whether a query\n"
          "finds an unlabelled room among otherwise-labelled ones, the actual\n"
          "deployment condition. It does not measure disambiguating between\n"
          "several rooms of the identical class in the same building (e.g.\n"
          "'the kitchen on the 2nd floor') -- any candidate in the set counts\n"
          "as resolved, which is the easier and, for now, honest bar.")


if __name__ == "__main__":
    main()
