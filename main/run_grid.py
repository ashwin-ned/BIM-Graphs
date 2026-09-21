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

That takes 88 naive cells to 56.

The primary vision conditions are the **set-of-mark** ones, where each
declared room carries a numbered disc and the model answers with the number.
Asking for bare pixel coordinates measures coordinate regression rather than
plan reading; the unmarked `plan` and `occ` cells are kept as that
localisation result, and they are cheap because they need one cell each.

Each cell is a separate process, and each model brings its own generation cap
(`extract_graph_llm.cap_for`) because the requirement differs by an order of
magnitude: a thinking model spends 92-100% of its output on a trace. A model
reload costs about a minute against a vision cell that runs for hours at the
measured rate, so the per-cell process is free. A cell whose scores file
already exists is skipped, which makes the run resumable after an interruption
without a bespoke checkpoint.

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

from extract_graph_llm import MODELS, cap_for, output_tag

LEVELS = ("synth150", "synth150_L2", "synth150_L3", "synth150_L4")
CORPUS_ROOT = "dataset/synthetic_data"

TEXT_MODELS = [k for k, v in MODELS.items() if v["kind"] == "text"]
VISION_MODELS = [k for k, v in MODELS.items() if v["kind"] == "vision"]

# The comparison the paper leads with: one model family whose text and vision
# towers share a backbone, so a text-vs-vision difference is a modality result
# and not a model-family result. These cells get the full corpus.
#
# Qwen2.5 and not GLM, on measured grounds. The GLM pair was the original
# choice, but GLM-4.1V-9B-Thinking cannot complete the conditions the grid
# needs: at a 6144-token cap it truncated 4 of 4 units on both occ_som and
# text+plan_som, because its <think> trace is 92-100% of its output and cannot
# be suppressed (a prefilled closed </think> makes it reason in prose and emit
# nothing parseable). Qwen2.5-7B-Instruct and Qwen2.5-VL-7B-Instruct share a
# backbone just as well, both answer every unit, and the vision one runs at
# 7.7 s/unit against 161.
HEADLINE_MODELS = ("qwen25-7b", "qwen25-vl-7b")

# GLM-4.1V earns one cell, not eight: it is the paper's "does a reasoning
# trace help structured extraction" data point, and the answer so far is that
# it costs 21x and scores lower than a non-thinking model on the same units.
# Restricted to the one modality it can actually finish.
THINKING_VISION = "glm-9b-v"
THINKING_MODALITIES = ("plan_som",)


# Text damage has two regimes: L2, L3 and L4 declare an identical set of
# IfcSpaces (595 against L1's 869), because storey assignment is recovered
# geometrically and L4's centimetre units are absorbed by
# `detect_scale_correction`. The text modality is cheap enough to run all four
# and show the flatness; the vision modalities run the two regimes and one
# control.
REGIMES = ("synth150", "synth150_L2")


def cells() -> list:
    """Every (model, modality, corpus, mask) the grid actually needs."""
    out = []

    # -- text: the baseline, and the only cheap modality -------------------
    for m in TEXT_MODELS:
        for corpus in LEVELS:
            for mask in (False, True):
                out.append({"model": m, "modality": "text",
                            "corpus": corpus, "mask": mask})

    # -- set-of-mark: the primary vision condition -------------------------
    # Each declared room carries a numbered disc, so a model names rooms by
    # number and never has to localise anything. This is the "best conditions"
    # question: given the room set, can a model read a plan? It is also the
    # only vision condition that is damage-sensitive, because the marks follow
    # the *declared* rooms and half of those are gone from L2 on.
    for m in VISION_MODELS:
        thinking = (m == THINKING_VISION)
        for corpus in REGIMES:
            for modality in ("plan_som", "occ_som"):
                if thinking and modality not in THINKING_MODALITIES:
                    continue
                out.append({"model": m, "modality": modality,
                            "corpus": corpus, "mask": False})
            if thinking and "text+plan_som" not in THINKING_MODALITIES:
                continue
            for mask in (False, True):
                out.append({"model": m, "modality": "text+plan_som",
                            "corpus": corpus, "mask": mask})

    # -- unmarked images: the localisation result --------------------------
    # Reported because it is the honest strict condition, and small. A probe of
    # GLM-4.1V-9B-Thinking on a bare plan got the room count exactly right and
    # still matched almost nothing, because its centroids clustered in one
    # corner of the image; that is a localisation failure and it should be
    # shown as one rather than folded into a plan-reading claim. Invariant to
    # damage and to masking (the renders are byte-identical and carry no
    # names), so one cell each plus one control.
    for m in VISION_MODELS:
        if m == THINKING_VISION:
            continue            # it cannot finish the marked ones; see above
        for modality in ("plan", "occ"):
            out.append({"model": m, "modality": modality,
                        "corpus": "synth150", "mask": False})
    # Controls use a model that answers, or they control nothing.
    ctrl = HEADLINE_MODELS[1]
    out.append({"model": ctrl, "modality": "plan",
                "corpus": "synth150_L4", "mask": False,
                "note": "invariance control: must match the synth150 plan cell"})
    out.append({"model": ctrl, "modality": "plan_som",
                "corpus": "synth150_L3", "mask": False,
                "note": "regime control: must match the synth150_L2 plan_som cell"})
    return out


def is_headline(c: dict) -> bool:
    return c["model"] in HEADLINE_MODELS


def cell_tag(c: dict) -> str:
    """Delegates to the harness so the resume check and the writer agree."""
    kind = "llm" if MODELS[c["model"]]["kind"] == "text" else "vlm"
    return output_tag(c["corpus"], kind, c["model"], c["modality"], c["mask"])


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
               "--max-new-tokens", str(cap_for(c["model"], a.max_new_tokens)),
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
