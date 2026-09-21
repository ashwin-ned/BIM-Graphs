"""Label the real corpus's recovered rooms and re-save the graphs.

344 of 1,025 rooms across the 25 real models have no functional label -- every
one of them is a room our pipeline recovered geometrically, since IFC cannot
name what it never modelled. This runs the semantic injection stage over the
whole corpus and writes the predictions back into each graph file, so
downstream consumers (QnA, navigation goal resolution) see them without
re-running the model.

    python main/label_corpus.py --backend llm
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import semantics as S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphs", default="results/model_*.graph.json")
    ap.add_argument("--backend", default="llm", choices=["rule", "llm"])
    ap.add_argument("--model-id", default="zai-org/GLM-4-9B-0414")
    args = ap.parse_args()

    backend = S.RuleBackend() if args.backend == "rule" else S.LLMBackend(args.model_id)
    print(f"backend: {getattr(backend, 'name', '?')}")

    total = Counter()
    t0 = time.time()
    for p in sorted(glob.glob(args.graphs)):
        g = G.load(p)
        preds = S.label_rooms(g, backend, only_unlabelled=True)
        if preds:
            G.save(g, p)
        total.update(preds.values())
        n_rooms = sum(1 for _, d in g.nodes(data=True) if d.get("layer") == "space")
        print(f"  {os.path.basename(p):<24} rooms={n_rooms:4d} "
              f"labelled_now={len(preds):4d}")

    print(f"\n{sum(total.values())} rooms labelled in {time.time()-t0:.0f}s")
    for k, v in total.most_common():
        print(f"  {k:<14} {v}")


if __name__ == "__main__":
    main()
