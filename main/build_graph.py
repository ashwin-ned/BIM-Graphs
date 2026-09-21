"""BIM-Graphs: build a hybrid hierarchical scene graph from an IFC model.

    python main/build_graph.py dataset/test_set/model_0.ifc
    python main/build_graph.py dataset/test_set/*.ifc --out results/
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import graph as G
from bimsg import parser as P


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="IFC file(s) or glob")
    ap.add_argument("--out", default="results", help="output directory")
    ap.add_argument("--no-infer", action="store_true",
                    help="disable geometric room recovery (IFC spatial tree only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    paths = []
    for p in args.inputs:
        paths.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])
    os.makedirs(args.out, exist_ok=True)

    for path in paths:
        t0 = time.time()
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"\n=== {name} ===")
        model = P.parse(path)
        g = G.build(model, infer_missing_rooms=not args.no_infer)
        out = os.path.join(args.out, f"{name}.graph.json")
        G.save(g, out)
        print(G.summary(g))
        print(f"  elapsed              {time.time() - t0:.1f}s -> {out}")


if __name__ == "__main__":
    main()
