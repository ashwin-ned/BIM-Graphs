"""Score room labelling by holding out the labels IFC did state.

We cannot check predictions for the 344 recovered rooms -- nobody has said what
they are. But 681 rooms in the real corpus *are* labelled, and those labels came
from the building's designer rather than from us. Masking them and predicting
from graph context alone gives a genuine accuracy figure on real buildings,
today, with no annotation.

The held-out rooms are stated IfcSpaces, so this measures the labeller on
slightly easier geometry than the recovered rooms it will actually run on. That
makes the number an optimistic bound, and it is reported as one.

Labels are compared in the canonical taxonomy, not as raw strings: the corpus
spells one concept as "wc", "toilet", "rest room" and "bathroom", and scoring
strings would measure an exporter's vocabulary rather than the method.

    python main/eval_semantics.py --backend rule
    python main/eval_semantics.py --backend llm --limit 200
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import semantics as S


def _warn_missing_basis(g):
    """Say once, per graph, that its vertical links predate evidence tracking."""
    if g.graph.get("_warned_basis"):
        return
    g.graph["_warned_basis"] = True
    print(f"  note: {g.graph.get('model')} has vertical links with no `basis`; "
          f"it was built before label-derived links were marked. They are all "
          f"masked, which under-reports rather than leaks. Rebuild the graph "
          f"to distinguish them.")


@contextlib.contextmanager
def mask_room(g, node):
    """Hide a room's label, and everything downstream of it, then restore.

    Blanking the label was not enough. `topology.vertical_connections` links two
    rooms when *both* are named "Stair", so a room's own label had already been
    turned into a graph edge by the time the evaluator masked it -- and
    `n_vertical`, a feature the classifier reads, still counted that edge. The
    classifier was being told the answer for the one class the rule most relies
    on the name for.

    Masking therefore has to follow the dependency, not just the field. Edges
    marked `basis="stacked_label"` exist only because of the labels of their
    endpoints, so they go too. Links carved by an actual stair solid stay: they
    rest on geometry, which is evidence the method legitimately has.
    """
    d = g.nodes[node]
    kept_label = d.get("label")
    removed = []
    for u, v, k, ed in list(g.edges(keys=True, data=True)):
        if node not in (u, v):
            continue
        if ed.get("relation") != "vertically_connected":
            continue
        # Only a link demonstrably carved by geometry survives. A link marked
        # `stacked_label` rests on the label being masked. A link with no basis
        # at all comes from a graph built before this distinction existed, and
        # there is no way to tell which it is -- so it is removed too. That is
        # the conservative direction: over-masking can only make a measured
        # accuracy lower, while guessing "geometry" would leak the answer and
        # make it higher.
        if ed.get("basis") != "stair_solid":
            if ed.get("basis") is None:
                _warn_missing_basis(g)
            removed.append((u, v, k, dict(ed)))
            g.remove_edge(u, v, k)
    d["label"] = "room"
    try:
        yield
    finally:
        d["label"] = kept_label
        for u, v, k, ed in removed:
            g.add_edge(u, v, key=k, **ed)


def collect(pattern: str):
    """Rooms whose stated label maps onto the taxonomy, with their features.

    The label, and every feature derived from it, is removed from the graph
    before features are read, so a neighbour's label can inform a prediction but
    the room's own cannot -- directly or through the topology it induced.

    Each item also carries the source IFC path, the room's IFC name
    (`room_number`) and its GlobalId (`_room_gid`), which the VLM backend needs
    to render the crop of *that* room -- the serialized graph keeps only
    centroids, not footprint polygons, and room numbers repeat.
    """
    items = []
    for p in sorted(glob.glob(pattern)):
        g = G.load(p)
        ifc_path = g.graph.get("model")
        truth = {}
        for n, d in g.nodes(data=True):
            if d.get("layer") != "space" or d.get("source") != "ifc":
                continue
            c = S.canonical(d.get("label"))
            if c:
                truth[n] = c
        # Leave-one-out: mask only the room being predicted, so its neighbours
        # keep their labels. Masking every room at once destroys the
        # `connects_to` feature -- the most informative signal available -- and
        # measures the labeller on information it would never actually lack:
        # in deployment a recovered room sits among stated, labelled ones.
        for n, c in truth.items():
            with mask_room(g, n):
                feats = S.room_features(g, n)
            feats["_ifc_path"] = ifc_path
            feats["_room_number"] = g.nodes[n].get("room_number")
            feats["_room_gid"] = n
            feats["_storey_gid"] = next(
                (u for u, _, e in g.in_edges(n, data=True)
                 if e.get("relation") == "contains"
                 and g.nodes[u].get("layer") == "storey"), None)
            items.append((os.path.basename(p), n, feats, c))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphs", default="results/model_*.graph.json")
    ap.add_argument("--backend", default="rule",
                    choices=["rule", "llm", "vlm"])
    ap.add_argument("--model-id", default="zai-org/GLM-4-9B-0414")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=1536,
                    help="generation cap for the VLM backend. GLM-4.1V-9B-"
                         "Thinking emits an unconditional <think> trace; at "
                         "the old default of 300, 16.0%% of rooms never closed "
                         "it and produced no answer at all")
    ap.add_argument("--out", default="results/semantics_eval.json")
    args = ap.parse_args()

    items = collect(args.graphs)
    if args.limit:
        step = max(1, len(items) // args.limit)
        items = items[::step][:args.limit]
    print(f"held-out rooms: {len(items)}  "
          f"(from {len(set(i[0] for i in items))} models)")

    cache = None
    if args.backend == "rule":
        backend = S.RuleBackend()
    elif args.backend == "llm":
        backend = S.LLMBackend(args.model_id)
    else:
        from render_plan import PlanCache
        cache = PlanCache()
        vlm_id = (args.model_id if args.model_id != "zai-org/GLM-4-9B-0414"
                 else "zai-org/GLM-4.1V-9B-Thinking")
        backend = S.VLMBackend(vlm_id)

    # Majority class is the bar: predicting the single commonest label for
    # everything is what any real method has to beat.
    dist = Counter(c for _, _, _, c in items)
    majority = dist.most_common(1)[0]

    correct = 0
    skipped = 0
    # A non-answer is now counted, never substituted for. `status` comes from
    # the backend: "ok", "truncated" (hit the token cap mid-thought) or
    # "unparseable" (finished, but said nothing in the taxonomy). Both failure
    # modes yield pred=None and cannot be correct.
    status_n = Counter()
    per_class = defaultdict(lambda: [0, 0])       # class -> [hit, total]
    confusion = Counter()
    # Progress, because the VLM backend takes hours and printed nothing until
    # it finished -- there was no way to tell a slow run from a hung one, or to
    # estimate when it would end.
    import time as _time
    _t0 = _time.time()
    _every = 25 if len(items) > 100 else 10
    for _i, (_, _, feats, truth) in enumerate(items, 1):
        if cache is not None:
            # Identified by GlobalId, and checked against the storey the graph
            # puts it on. A crop of the wrong room is scored as if it were the
            # right one, and nothing in the results shows it happened.
            img = cache.crop(feats["_ifc_path"], feats["_room_number"],
                             gid=feats.get("_room_gid"),
                             storey_gid=feats.get("_storey_gid"))
            if img is None:
                skipped += 1
                continue
            pred, status = backend.predict_image(
                img, feats, max_new_tokens=args.max_new_tokens)
        else:
            pred, status = S.predict_label(backend, feats)
        status_n[status] += 1
        if _i % _every == 0 or _i == len(items):
            el = _time.time() - _t0
            rate = el / _i
            print(f"  [{_i}/{len(items)}] {el/60:.1f} min elapsed, "
                  f"{rate:.1f} s/room, ~{rate*(len(items)-_i)/60:.0f} min left"
                  f"  (ok {status_n['ok']}, truncated {status_n['truncated']}, "
                  f"unparseable {status_n['unparseable']})", flush=True)
        per_class[truth][1] += 1
        if pred is None:
            confusion[f"{truth}->NO ANSWER ({status})"] += 1
            continue
        if pred == truth:
            correct += 1
            per_class[truth][0] += 1
        else:
            confusion[f"{truth}->{pred}"] += 1

    # n excludes skipped items: a room whose crop could not be rendered was
    # never scored, so it must not be counted in the denominator either, or
    # skipped rooms would silently depress accuracy as if they were wrong.
    n = len(items) - skipped
    no_answer = status_n["truncated"] + status_n["unparseable"]
    answered = n - no_answer
    # Two honest numbers, both reported, because they answer different
    # questions and neither is the blended figure this used to print:
    #   acc        charges a non-answer as wrong  -- "how good is this backend"
    #   acc_answered  over rooms it did answer    -- "how good is it when it
    #                                                 commits to an answer"
    acc = correct / n if n else 0.0
    acc_answered = correct / answered if answered else None
    print(f"\nbackend: {getattr(backend,'name','?')}")
    print(f"  accuracy            {correct}/{n} = {acc:.3f}"
          f"{'  (non-answers charged as wrong)' if no_answer else ''}")
    if skipped:
        print(f"  skipped (no crop)   {skipped}")
    if no_answer:
        print(f"  NO ANSWER           {no_answer}/{n} = {no_answer/n:.1%}"
              f"   truncated {status_n['truncated']}, "
              f"unparseable {status_n['unparseable']}")
        print(f"  accuracy when it did answer: {correct}/{answered} = "
              f"{acc_answered:.3f}")
    print(f"  majority baseline   {majority[1]}/{n} = {majority[1]/n:.3f} "
          f"(always '{majority[0]}')")
    print(f"  lift over majority  {acc - majority[1]/n:+.3f}")

    print(f"\n  {'class':<14}{'recall':>9}{'n':>7}")
    for c, (h, t) in sorted(per_class.items(), key=lambda x: -x[1][1]):
        print(f"  {c:<14}{h/t:>9.3f}{t:>7}")

    print("\n  most common confusions:")
    for k, v in confusion.most_common(8):
        print(f"    {k:<34} {v}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    rec = {"backend": getattr(backend, "name", "?"), "n": n, "accuracy": acc,
           "majority_baseline": majority[1] / n if n else 0,
           "answered": answered,
           "no_answer": no_answer,
           "truncated_n": status_n["truncated"],
           "unparseable_n": status_n["unparseable"],
           "accuracy_answered_only": acc_answered,
           "max_new_tokens": args.max_new_tokens,
           "per_class": {c: {"recall": h / t, "n": t}
                         for c, (h, t) in per_class.items()},
           "confusions": dict(confusion.most_common(20))}
    hist = []
    if os.path.exists(args.out):
        try:
            hist = json.load(open(args.out))
        except Exception:
            hist = []
    hist.append(rec)
    json.dump(hist, open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")
    print("\nNOTE: held-out rooms are stated IfcSpaces, which are cleaner than\n"
          "the recovered rooms this will run on. Treat as an optimistic bound.")


if __name__ == "__main__":
    main()
