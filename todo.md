# TODO — everything required to complete `paper_plan.md`

56 days to submission (21 Sep → 16 Nov 2026 AoE). Ordered by dependency, not by
section number — items are grouped into phases where later phases genuinely
need earlier ones, and unrelated items within a phase can run in parallel.

Checkbox = not started. Every item names the `paper_plan.md` section it
unblocks, so nothing here is busywork disconnected from the paper.

---

## Phase A — quick, no design work, do first (≈1–2 days)

- [x] **Re-run the VLM semantic backend** now that truncation is instrumented.
      Done 21 Sep 2026: `max_new_tokens` raised 300→1536, both `VLMBackend`
      and `LLMBackend`'s silent-fallback-to-rule-prior defect fixed
      (`_coerce_strict`, regression-guarded by
      `tests/test_vlm_answer_accounting.py`). Result: 294/592 = 0.497
      (590/592 answered, 2 truncated, 0 unparseable) — real number now in
      §4.4, `README.md`, and `report/results_2026_09_18.tex`/`.pdf`.
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

## Phase B — extraction harness (Method §3.5, unblocks all of Phase C) — **BUILT 21 Sep 2026**

Landed as `main/bimsg/graph_extract.py` (library), `main/extract_graph_llm.py`
(CLI) and `tests/test_graph_extraction.py` (guard), plus two additions to the
evaluation contract. No new metric was written: a model's answer is parsed into
a graph in the pipeline's own shape and handed to the **existing**
`score_connectivity` / `score_labels`, so a model and the deterministic method
are compared by one definition of precision.

- [x] **Whole-graph extraction prompt format** — four modalities (`text`,
      `plan`, `occ`, `text+plan`), one output contract, JSON rather than a
      grammar. **Size decision, stated not discovered: one storey per call**,
      because a plan image is already one storey and per-storey text keeps the
      modalities comparable rather than asking the text condition to do more.
- [x] **Vertical links are out of scope**, on *both* sides — removed from the
      reference by the new `GroundTruth.restrict`, which counts the pairs it
      drops (`dropped_straddling`). Rationale: the deterministic path already
      recovers them at precision 1.000, so there is nothing for a model to win
      and points for it to lose by accident. Restricting only the prediction
      would have turned a task definition into a measured failure.
- [x] **JSON-schema-validated parser with explicit failure accounting** —
      `Extraction.status` ∈ {ok, no_json, invalid_json, schema_invalid,
      truncated}, plus per-item `dropped_rooms` / `dropped_edges` counters
      (duplicate id, undeclared endpoint, self loop, unknown type, …). A
      failed extraction yields an **empty** graph, never a partial one. Reads
      only what follows `</think>`, so a thinking model's discarded first
      draft is not mined as its answer.
- [x] **Pixel-space matching** — `px_to_m` inverts the stored
      `transform`, verified by round-trip. Implemented as a lift at parse time
      rather than a new `detect_dialect` branch: `detect_dialect` reads
      *ground-truth* files and a prediction is not ground truth, so adding a
      dialect there would have been the wrong seam. Metres and pixels are kept
      in separate keys — pooling them puts a room ~19 m from its true position
      and reads as a recall failure rather than a unit error (measured while
      breaking it on purpose).
- [x] **Silent-zero guard** — `matching._diagnose` names the two cases where
      zero matches is *not* the prediction's fault: a reference dialect that
      states no room extent, and a prediction never lifted into metres. Both
      are recorded as `failed: unscorable` and leave the average, rather than
      contributing a 0.000 that describes the harness.
- [x] **Provenance logging** — per prediction: prompt SHA-256, raw response,
      token counts, wall-clock seconds, extraction summary, the reference's
      restricted scope; per run: model id and `_commit_hash` revision. Written
      alongside a `RunManifest` and a scores file.
- [x] **An `oracle` backend, and this is the part that earned its keep.** It
      emits the reference through the real prompt path, real parser and real
      scorer, and must score 1.000. Three anchor modes, because the obvious one
      is not enough: `identity` reported OK with the spatial anchor deleted
      **and** against a deliberately sign-flipped `px_to_m`, while `pixels`
      caught both (recall 0.000, 69 rooms missed and 69 invented). Only
      `pixels` tests what the vision conditions depend on. A `replay` backend
      re-scores logged responses so a scorer fix costs no GPU hours.
- [x] **Verified against known-bad input** — 11 deliberate defects injected one
      at a time and each confirmed to fail the suite: the `label` /
      `predicted_label` self-scoring tautology, think-trace mining, a partial
      extraction reported as success, uncounted item drops, `restrict`
      forgetting and `restrict` over-keeping, a deleted diagnosis, a sign-flipped
      lift, a metre value lifted as pixels, a guessed bare centroid, and the
      unrestricted reference. Harness verified end to end at 1.000 on
      **4 modalities × 4 damage levels × 300 units** and on the real corpus
      (583/583 rooms, 522/522 labels).

**Surfaced by the oracle rather than by a wasted run, then resolved by the
set-of-mark condition: unmarked vision cannot be scored on the real corpus,
but marked vision can.** The derived reference
(`results/real_gt/*.realgt.json`) records a centroid and an area but no room
extent, so an *unmarked* image-conditioned prediction -- which invents its own
room ids -- has nothing to match against, and all 7 units of a 12-unit probe
came back `failed: unscorable` with 0 scored. A **mark resolves to the room's
GlobalId**, which is exactly what that reference keys on, so `plan_som` /
`occ_som` join it exactly: 268/268 marks resolve across 9 usable models, and
the oracle scores 1.000 over 60 scorable units with 522/522 labels. Real-corpus
vision is therefore in scope after all, and it is the corpus where vision
labelling has any chance -- its buildings carry windows, columns and railings
where the synthetic ones carry none. Emitting extents from
`main/derive_real_gt.py` is no longer needed for this, only for the unmarked
conditions.

---

## Phase C — the multimodal grid (Experiment §4.5) — scoped and probed 21 Sep 2026

Decisions taken (all approved). `main/run_grid.py` enumerates **56 cells**;
`--dry-run` prints them.

- [x] **Model list: 3 text + 3 vision**, registered in `extract_graph_llm.MODELS`
      with `pairs_with` recording shared backbones.
      Text: `glm-9b`, `qwen25-7b`, `qwen3-8b` (downloaded 21 Sep, 16 GB).
      Vision: `glm-9b-v`, `qwen25-vl-7b`, `internvl3-8b`.
      Llama-3.1-8B-Instruct and gemma-2-9b-it were the obvious third text
      picks and are **gated** (HTTP 401 without a token); Qwen3-8B is the
      strongest ungated model in the weight class. InternVL3-8B's language
      tower is Qwen2.5-7B, so its text counterpart was already in the list.
- [x] **Grid shape.** Image-only modalities are invariant to damage (renders
      byte-identical, verified 40/40) and to label masking (renders carry no
      names), so they get one cell each plus two deliberate controls. Text
      gets all four levels because it is the cheap modality and the flat
      L2=L3=L4 result is itself a finding.
- [x] **Sampling: staged.** Exploratory pass at 30 buildings (60 units/cell),
      headline cells at 150 (the paired Wilcoxon in `main/significance.py`
      wants the full corpus).
- [x] **Greedy decoding** (`do_sample=False`), one run per cell. This is what
      makes the image-only invariance argument exact.
- [x] **Set-of-mark is the primary vision condition** — see Phase B's closing
      notes and `render_blind.render_som_png`. Unmarked `plan`/`occ` are still
      run, as the honest strict condition and the localisation result.
- [ ] **Load-test `qwen25-vl-7b`, `internvl3-8b` and `qwen3-8b` in this
      harness before planning around them.** Only the GLM pair has been run
      end to end. `VisionBackend` uses `AutoModelForImageTextToText` with a
      `{"type": "image"}` message part; InternVL historically wants its own
      `model.chat()` interface, so this is the likeliest breakage.
- [ ] Run the grid (`main/run_grid.py --buildings 30`), then the headline
      cells at 150.
- [ ] Produce the comparison table and the modality-ablation result.

### Measured on 21 Sep 2026 — use these, not estimates

| | s/unit | truncation | notes |
|---|---|---|---|
| text, GLM-4-9B, 3072 tok | **24** | 0/6 | 6/6 parsed |
| plan, GLM-4.1V-9B, 2048 tok | 89 | **4/6** | cap far too low |
| plan, GLM-4.1V-9B, 4096 tok | **139** | 1/6 | usable |

Budget at 30 buildings/cell: 24 text cells ≈ 9.6 h + 32 vision cells ≈ 74 h
= **≈ 3.5 GPU-days** for the exploratory pass. **Use `--max-new-tokens 4096`
or higher for any vision cell**; 2048 truncates two thirds of them.

The apples-to-apples deterministic bar, scored on the *identical* restricted
task (per storey, `connected_by_door` + `open_passage` only, n=300 storeys):

| corpus | `ours` F1 | `door_near` F1 |
|---|---|---|
| L1 | **0.997** | 0.907 |
| L2 / L3 / L4 | **0.897** | 0.852 |

Cross-checks against the published whole-task 0.998 / 0.899, so the
restriction is not distorting anything. A model number is only meaningful
against these.

### Three things Phase C must state rather than discover

1. **The synthetic corpus cannot test vision labelling.** Its buildings
   contain only `IfcWall`, `IfcDoor` and `IfcStair` — no furniture and **no
   windows at all** — so a plan render carries almost no functional cue. A
   probe scored labels 0/22 with every prediction wrong in a plausible way
   (`bathroom` for a stair, `living`/`bedroom` for corridors). Vision label
   accuracy on synth150 is measuring near-chance by construction. The real
   corpus has windows, columns and railings; report vision labelling there,
   or state this explicitly.
2. **The damage axis is a text axis.** The renders are byte-identical across
   levels, so vision performance cannot move with damage. That *is* the
   headline — text degrades, vision does not — but it means "vision is robust
   to damage" is true by construction and must be presented as such.
3. **Set-of-mark at L2+ has a selection effect.** Damage strips `IfcSpace`
   from whole storeys, so 150 of 300 units have no marks at all and are
   recorded `missing`. The surviving 150 are exactly the storeys that kept
   their spaces, so an L1-vs-L2 SOM comparison is not over the same units.
   Per-unit scores are written to `*.scores.json`, so intersect the unit sets
   post hoc rather than comparing pooled numbers.

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
