"""Enumerate and run the Phase C grid, one cell at a time, resumably.

The grid is not the cross product. Three of its axes do nothing for some of
its cells, and running them anyway would spend days producing identical
numbers:

* **The plan and occupancy renders are byte-identical across L1-L4** (verified
  40/40 per kind), because `damage_ifc.py` degrades IFC *text* and never
  touches geometry. With greedy decoding a `plan` cell at L2 is the same input
  as at L1, so it is run at L1 only -- plus one deliberate L4 control, so the
  invariance is demonstrated rather than asserted.
* **The renders carry no room names** (that is what `render_blind.py` is for),
  so the labels-given/labels-masked condition does nothing to a pure image
  modality. `plan` and `occ` therefore have no masked row, and its absence is
  a consequence of the corpus rather than a gap.
* **Text damage has two regimes, not four.** L2, L3 and L4 produce identical
  declared-space counts (595 each against L1's 869), because storey assignment
  is recovered geometrically and L4's centimetre units are absorbed by
  `detect_scale_correction`. All four are still run for the text modalities --
  they are the cheap ones, and the flatness is itself a result to show.

That takes 88 naive cells to 55.

Each cell is a separate process. A model reload costs about a minute against a
cell that takes over an hour at the vision models' measured rate, so the
simplicity is free. A cell whose scores file already exists is skipped, which
makes the run resumable after an interruption without a bespoke checkpoint.

    python main/run_grid.py --buildings 30 --dry-run     # see the plan first
    python main/run_grid.py --buildings 30               # exploratory pass
    python main/run_grid.py --buildings 150 --only-headline
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_graph_llm import MODELS

LEVELS = ("synth150", "synth150_L2", "synth150_L3", "synth150_L4")
CORPUS_ROOT = "dataset/synthetic_data"

TEXT_MODELS = [k for k, v in MODELS.items() if v["kind"] == "text"]
VISION_MODELS = [k for k, v in MODELS.items() if v["kind"] == "vision"]

# The comparison the paper leads with: one model family whose text and vision
# towers share a backbone, so a text-vs-vision difference is a modality result
# and not a model-family result. These cells get the full corpus.
HEADLINE_MODELS = ("glm-9b", "glm-9b-v")


def cells() -> list:
    """Every (model, modality, corpus, mask) the grid actually needs."""
    out = []

    # Text-bearing modalities: damage and label masking both matter.
    for m in TEXT_MODELS:
        for corpus in LEVELS:
            for mask in (False, True):
                out.append({"model": m, "modality": "text",
                            "corpus": corpus, "mask": mask})
    for m in VISION_MODELS:
        for corpus in LEVELS:
            for mask in (False, True):
                out.append({"model": m, "modality": "text+plan",
                            "corpus": corpus, "mask": mask})

    # Image-only modalities: invariant to damage and to masking, so one cell
    # each -- plus a single L4 control to prove the invariance empirically.
    for m in VISION_MODELS:
        for modality in ("plan", "occ"):
            out.append({"model": m, "modality": modality,
                        "corpus": "synth150", "mask": False})
    out.append({"model": VISION_MODELS[0], "modality": "plan",
                "corpus": "synth150_L4", "mask": False,
                "note": "invariance control: must match the synth150 plan cell"})
    return out


def is_headline(c: dict) -> bool:
    return c["model"] in HEADLINE_MODELS


def cell_tag(c: dict) -> str:
    kind = "llm" if MODELS[c["model"]]["kind"] == "text" else "vlm"
    return (f"{c['corpus']}_{kind}-{c['model']}_"
            f"{c['modality'].replace('+', '-')}"
            f"{'_masked' if c['mask'] else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--buildings", type=int, default=30,
                    help="buildings per cell; each contributes 2 storeys. 30 "
                         "for the exploratory pass, 150 for a headline cell "
                         "(the paired Wilcoxon in main/significance.py wants "
                         "the full corpus)")
    ap.add_argument("--max-new-tokens", type=int, default=3072)
    ap.add_argument("--out", default="results/grid")
    ap.add_argument("--only-headline", action="store_true",
                    help="only the model family whose text and vision towers "
                         "share a backbone")
    ap.add_argument("--only-models", nargs="*", default=None)
    ap.add_argument("--only-modalities", nargs="*", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=0,
                    help="per-cell wall-clock cap in seconds; 0 for none")
    a = ap.parse_args(argv)

    plan = cells()
    if a.only_headline:
        plan = [c for c in plan if is_headline(c)]
    if a.only_models:
        plan = [c for c in plan if c["model"] in a.only_models]
    if a.only_modalities:
        plan = [c for c in plan if c["modality"] in a.only_modalities]

    os.makedirs(a.out, exist_ok=True)
    todo, done = [], []
    for c in plan:
        scores = os.path.join(a.out, f"{cell_tag(c)}.scores.json")
        (done if os.path.exists(scores) else todo).append(c)

    print(f"grid: {len(plan)} cells  ({len(done)} already done, "
          f"{len(todo)} to run)")
    print(f"  {a.buildings} buildings/cell = {a.buildings * 2} units/cell")
    print(f"  models: {len(TEXT_MODELS)} text {TEXT_MODELS}, "
          f"{len(VISION_MODELS)} vision {VISION_MODELS}")
    by_mod = {}
    for c in plan:
        by_mod[c["modality"]] = by_mod.get(c["modality"], 0) + 1
    print(f"  cells by modality: {by_mod}")
    if a.dry_run:
        for c in todo:
            print(f"    {cell_tag(c)}"
                  + (f"   <- {c['note']}" if c.get("note") else ""))
        return 0

    t0 = time.time()
    failed = []
    for i, c in enumerate(todo, 1):
        kind = "llm" if MODELS[c["model"]]["kind"] == "text" else "vlm"
        cmd = [sys.executable, "main/extract_graph_llm.py",
               "--corpus", os.path.join(CORPUS_ROOT, c["corpus"]),
               "--backend", kind, "--model-id", c["model"],
               "--modality", c["modality"],
               "--limit", str(a.buildings * 2),
               "--max-new-tokens", str(a.max_new_tokens),
               "--progress-every", "25", "--out", a.out]
        if c["mask"]:
            cmd.append("--mask-labels")
        el = time.time() - t0
        print(f"\n=== [{i}/{len(todo)}] {cell_tag(c)}   "
              f"({el/3600:.1f} h elapsed)", flush=True)
        try:
            r = subprocess.run(cmd, timeout=a.timeout or None)
            if r.returncode != 0:
                failed.append((cell_tag(c), f"exit {r.returncode}"))
        except subprocess.TimeoutExpired:
            failed.append((cell_tag(c), f"timeout after {a.timeout}s"))

    print(f"\ngrid finished in {(time.time() - t0)/3600:.1f} h")
    if failed:
        # Named, not swallowed: a cell that failed must be visible or the
        # grid's average is over a denominator nobody chose.
        print(f"  {len(failed)} cell(s) did not complete:")
        for tag, why in failed:
            print(f"    !! {tag}: {why}")
    summary = os.path.join(a.out, "_grid_summary.json")
    with open(summary, "w") as fh:
        json.dump({"cells_planned": len(plan), "cells_run": len(todo),
                   "failed": failed, "buildings_per_cell": a.buildings,
                   "hours": round((time.time() - t0) / 3600, 2)}, fh, indent=2)
    print(f"  wrote {summary}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
