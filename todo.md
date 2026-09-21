# TODO — everything required to complete `paper_plan.md`

56 days to submission (21 Sep → 16 Nov 2026 AoE). Ordered by dependency, not by
section number — items are grouped into phases where later phases genuinely
need earlier ones, and unrelated items within a phase can run in parallel.

Checkbox = not started. Every item names the `paper_plan.md` section it
unblocks, so nothing here is busywork disconnected from the paper.

---

## Phase A — quick, no design work, do first (≈1–2 days)

- [ ] **Re-run the VLM semantic backend** now that truncation is instrumented.
      `python main/eval_semantics.py --backend vlm --graphs 'results/model_*.graph.json'`.
      Unblocks §4.4's real number instead of the current [0.410, 0.602]
      bound. ~3.3 h GPU time; launch and continue other work.
- [ ] **Before that re-run, raise the generation budget or shorten the
      prompt** — 16.0% of generations hit the 300-token cap on the last run.
      Either is a one-line change in `main/bimsg/semantics.py`
      (`VLMBackend.predict_image`'s `max_new_tokens`, or trim `VLM_SYSTEM_PROMPT`).
      Re-check the truncation rate on a 20-item probe before committing to the
      full run again — the point is to not repeat the same failure at 5×
      the models in Phase C.
- [ ] **model_2 annotation top-up**: 3 rooms, 13 links, 1 floor link, against
      the now-intact plan (`annotator/data/model_2.plan.json`, 42 rooms).
      **Needs a human annotator — you or a colleague, not me.** Affects the
      real-corpus annotated reference in §4.2(a); low priority since (a) is
      already flagged not-quotable for specificity, but it does still feed
      precision/recall/F1 and the strict-precision figures.
- [ ] Sync `main/eval_semantics.py`'s instrumentation fix and the
      `evaluation/labels.py` vocabulary into `ifc-graphx/annotator/` if either
      is ever surfaced through the annotation tool (check first — likely not
      applicable, the annotator doesn't run model backends).

---

## Phase B — extraction harness (Method §3.5, unblocks all of Phase C) (≈10–12 days)

This is the largest genuinely new engineering block and the one nothing else
can start without.

- [ ] **Whole-graph extraction prompt format.** One prompt per modality
      (text-IFC, plan image, occupancy image, text+image) that asks a model to
      emit an entire building's graph — rooms with labels, edges with types —
      in one pass. Design decision needed: JSON, or a constrained grammar? A
      model's context window bounds how much of a large building fits in one
      call; decide now whether large buildings get chunked per storey or
      dropped from this experiment, and say so explicitly rather than
      silently.
- [ ] **JSON-schema-validated parser with explicit parse-failure accounting.**
      Replace `_coerce()`'s silent fallback-to-rule-baseline
      (`main/bimsg/semantics.py`) with something that records `pred_missing` /
      `pred_unmapped` per `evaluation.scoring.score_labels`'s existing
      contract, extended to whole predicted graphs, not just single labels.
      This is the same defect Phase A's VLM re-run works around for the
      per-room task; the whole-graph task needs the general version.
- [ ] **Pixel-space identity mode** in `evaluation/matching.py` + a new
      dialect in `evaluation/schema.py`'s `detect_dialect` (currently raises
      on anything unrecognised). A model reading an image emits pixel
      coordinates or its own room ids, never IFC GlobalIds or world-metre
      centroids. The lift already exists — every `*_floorplans.json` storey
      carries `transform` = `{px_per_m, tx, ty, width, height}` — so this is
      wiring, not new geometry.
- [ ] **Provenance logging**: model revision, prompt hash, raw response, token
      counts, wall-clock runtime, per prediction. Needed for the grid in
      Phase C to be auditable and for `RunManifest` to have something to key
      on beyond model × building.
- [ ] New module, e.g. `main/extract_graph_llm.py`, tying the above together
      with a CLI parallel to `main/eval_semantics.py`'s
      `--backend {rule,llm,vlm}` shape but scoring whole graphs instead of
      single labels.
- [ ] **Test it before trusting it**: feed a known-bad model response (empty
      string, truncated JSON, a graph with an invented room) through the new
      parser and confirm it is charged correctly, not silently dropped or
      silently scored as correct — the standing rule in this project after
      three separate instances of exactly that failure shipping.

---

## Phase C — the multimodal grid (Experiment §4.5) (≈8–10 days incl. GPU time)

Blocked on Phase B. Depends on Phase A's truncation fix carrying over.

- [ ] **Finalise the model list against current cached weights** before
      committing GPU time. As of this planning pass: GLM-4-9B-0414 (text),
      GLM-4.1V-9B-Thinking (vision) confirmed cached and load-tested.
      Qwen2.5-VL-7B-Instruct and InternVL3-8B are cached but not yet
      load-tested end to end in this harness — do that first, same "load
      before you plan around it" rule that caught the `transformers`/`torch`
      false alarm earlier this project.
- [ ] Decide compute budget explicitly: full grid = models × {text, V-plan,
      V-occ, text+V-plan} × 4 damage levels × {labels given, labels masked} ×
      150 (or fewer, sampled) buildings. At the VLM's measured ~20 s/room this
      is easily multi-day if run exhaustively — **size the sample per cell
      deliberately** (e.g. a fixed subsample of synth150 for the full grid,
      the full corpus only for the winning configuration) rather than
      discovering the cost mid-run.
- [ ] Run the grid, cell by cell, `RunManifest`-tracked so a missing/failed
      cell is visible rather than silently absent from an average.
- [ ] Produce the comparison table and the modality-ablation result the
      abstract's **[TODO]** clauses need: what does vision add when text
      damage increases (the whole point of the byte-identical-across-damage
      corpus from §4's vision work).

---

## Phase D — method repairs and ablation ladder (Method §3.3–3.4, Experiment §4.6) (≈6–8 days)

Independent of Phase B/C — can run in parallel with them if a second pair of
hands is available; otherwise sequence after Phase C since it doesn't block
the multimodal grid and the grid is more schedule-critical.

- [ ] **A04 repair**: `main/bimsg/topology.py:359-360`, the axis-aligned
      bounding-box width check that admits a 15.74 m "doorway". Higher
      priority than the originally-tracked `_door_width` call site
      (`topology.py:253-257`, only 0.2% of doors affected).
- [ ] **A01 repair**: wire `_section_segments`
      (`main/render_floorplan.py:83-122`, already correct, already used for
      rendering) into the recovery path in place of the XY projection. Costs
      ~0.2 s/model extra for mesh parsing.
- [ ] After each repair: **flip its assertion in
      `tests/test_m0_regressions.py`** — the suite currently asserts these
      findings still reproduce, on purpose, so a real fix must update the
      test, not just pass despite it.
- [ ] Re-score synth150 × 4 levels after each repair (reuse the harness from
      §4.1 — `main/baselines.py` + `main/eval_connectivity.py` +
      `main/significance.py`), producing the before/A04/A01/both ablation
      ladder that is currently §4.6's empty table.
- [ ] **Re-score the annotation-free real-corpus reference too** — A01's
      repair is diagnosed to improve it directly (§4's residual 17
      door-backed disagreements). Confirm the improvement is real, not
      assumed; this is the paper's specific claim that one repair helps two
      things at once, and it needs to be measured, not just argued.
- [ ] Confirm `run_pipeline.sh` and `docs/SYNTH_BENCHMARK_2026_09_18.md` /
      `report/results_2026_09_18.tex` are updated with post-repair numbers —
      do not let the compiled report go stale the way README.md's headline
      table did after the model_2 rebuild.

---

## Phase E — writing (≈10–12 days, overlaps the tail of C/D)

- [ ] **Related work literature pass** for multimodal floor-plan
      understanding — the one related-work area with no citations assembled
      yet. Everything else (Ifc2Graph, IFC2SB, open-vocabulary scene graphs,
      BIM-Edit) already has a citation list, reusable from
      `icra_paper/RoboBIM_ICRA27_Paper/paper.tex:75` for the framing sentence
      only — re-verify every factual claim around those citations before
      reuse, the surrounding text in that draft is not trustworthy (see
      `docs/AUDIT_2026_09_18.md`).
- [ ] **Figures** (none currently exist in usable form — all 17 workshop
      figures are unusable, superseded pipeline / buildings not in this
      corpus):
  - [ ] System/pipeline overview
  - [ ] Error decomposition bar chart (data already exists — §4.3, cheapest
        figure to produce, do it early as a sanity check of the figure
        pipeline)
  - [ ] Damage-axis illustration: one building's plan rendered identically
        across L1–L4 next to its degrading IFC text
  - [ ] V-plan / V-occ example pair
  - [ ] `open_passage` qualitative failure case with measured gap width
  - [ ] Ablation ladder plot (blocked on Phase D)
- [ ] **Draft §1–3** (Intro, Related Work, Method) — nothing in these depends
      on Phase C/D's numeric outcome, start as soon as Phase E's lit-review
      item is far enough along to write Related Work honestly.
- [ ] **Draft §4 Experiments / §5 Results** — blocked on Phase C and D
      landing. Fill every **[TODO]** in `paper_plan.md` with a real number
      before this starts, not during.
- [ ] **Limitations** — expand the draft list in `paper_plan.md` once §4.5/4.6
      are known; in particular state plainly whatever the grid's actual
      modality-benefit finding is, including if vision does *not* clearly
      help — that would itself be a reportable, honest result given the
      controlled corpus this project built specifically to test it.
- [ ] **Final fact-check pass**: re-verify every number that ends up in the
      submitted draft against its source file the way `docs/AUDIT_2026_09_18.md`
      did for the current README — this project has now shipped stale figures
      at least three separate times (edge-type accuracy, the LLM-margin
      arithmetic, the "22 repairs" count), each caught only by a dedicated
      audit pass. Budget a day for this specifically, not as a side effect of
      writing.
- [ ] Internal read-through against `paper_plan.md`'s contribution list: does
      every claimed contribution have a table or figure backing it in the
      draft? Cut anything that doesn't rather than leaving it asserted.

---

## Decisions needed from you before Phase C can be scoped precisely

1. **Model list for the grid** — confirm GLM-4-9B/GLM-4.1V-9B-Thinking plus
   Qwen2.5-VL-7B and InternVL3-8B (all cached) is the intended set, or add/cut.
2. **Compute budget** — how many GPU-days can Phase C realistically use. This
   sets the per-cell sample size in Phase C's second checkbox.
3. **Second annotator for model_2's top-up** (Phase A) — you, or someone else?
4. **Whether a second pair of hands can run Phase D in parallel with Phase
   C** — if not, the two phases sequence and the schedule tightens
   accordingly; the writing plan in `paper_plan.md` already assumes they
   overlap.
