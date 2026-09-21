# Paper plan — RoboBIM: a multimodal benchmark for structured graph extraction from building models

Target venue: **CVPR 2027**. Deadline **16 November 2026, 23:59 AoE** (56 days
from today, 21 September 2026); supplementary until 23 November. Not movable —
stated in the CFP as final.

Every number below is one of two kinds, marked explicitly, because this project
has twice shipped a number that turned out to be untrue:

- **[VERIFIED]** — measured this session, source file cited. Safe to quote.
- **[TODO]** — the experiment that would produce it has not been run. Never
  write these into a draft as if they were real; `todo.md` is the list that
  closes each one.

---

## Working title

**RoboBIM: A Multimodal Benchmark for Structured Graph Extraction from
Building Information Models**

Alternative, if the downstream framing is emphasized more: *From IFC to
Graphs: Benchmarking Multimodal Extraction of Building Topology and Semantics*.

---

## Abstract (draft)

> Robots and language models that reason about buildings need a structured
> representation of what a building *is* — which rooms exist, which of them
> connect, and what each is for — not the raw geometry and text an IFC file
> contains. Prior work shows this representation cannot be produced by a
> language model reading IFC text alone: on BIM-Edit, a benchmark of 324
> real-world IFC editing tasks, the best current LLM scores 49.5% and no model
> fully solves more than 3.4% of tasks. We argue the missing piece is
> structure, not scale, and that recovering it is a genuinely multimodal
> problem — the same building is available as machine-readable text and as
> drawable geometry, and the two carry complementary evidence. We introduce a
> benchmark for structured graph extraction from building models that scores a
> predicted graph against a reference under **[VERIFIED]** an evaluation
> contract that never treats an unproposed pair as a negative and always
> reports specificity alongside recall, and we show why that distinction
> matters empirically: scoring against a correction-collected human reference
> yields a specificity of exactly zero, while a reference collected
> independently of any method's proposals yields **[VERIFIED]** 0.644 on real
> buildings and **[VERIFIED]** 0.993 on a controlled synthetic corpus.
> Decomposing error by relation type shows it is concentrated entirely in one
> mechanism — **[VERIFIED]** every door- and stair-mediated connection in our
> real corpus is correctly recovered (precision 1.000, 240/240 and 67/67),
> while open, doorless passages are correct only 55.8% of the time and carry a
> geometric signal that is measurably absent in both directions — motivating a
> **hybrid** method: deterministic where IFC authors the answer, learned where
> it does not. We render two leak-free visual conditions for every building —
> an architectural plan and a binary occupancy raster — and show they are
> **[VERIFIED]** exactly invariant to four levels of graduated IFC damage that
> remove up to half of a building's stated rooms, giving a controlled axis
> along which to measure what vision contributes when text degrades. On this
> axis, **[TODO]** we compare N open-weights text and vision-language models
> against the deterministic baseline, and show **[TODO]**. We repair
> **[TODO]** the geometric root cause common to both the method's connectivity
> errors and the evaluation reference's own residual noise, closing a gap that
> improves both simultaneously. We release the benchmark, the annotation tool,
> the corpus, and the evaluation harness.

The three **[TODO]** clauses are the paper's outstanding empirical content —
the model comparison, its outcome, and the repair. Nothing else in the
abstract is a placeholder.

---

## Contributions

1. **A benchmark for structured (topology + semantics) graph extraction from
   building models**, scored under an evaluation contract that distinguishes
   *unreviewed* from *negative* and reports specificity as a first-class
   metric — not a convenience derived from recall. **[VERIFIED]**
   `evaluation/` package, `docs/M0_IMPLEMENTATION_2026_09_14.md`.

2. **A methodological finding about ground truth**, general beyond this
   project: a reference collected by *correcting a method's own proposals*
   cannot measure that method's specificity, because every negative it can
   record is, by construction, something the method already proposed and a
   reviewer rejected. **[VERIFIED]** specificity is exactly 0.000 on the
   annotated reference (53/53 reference negatives are also pipeline-asserted
   edges); an annotation-free reference collected independently of the
   pipeline does not share the defect and yields specificity 0.644 (real) /
   0.993 (synthetic). Section 4 of `docs/AUDIT_2026_09_18.md`.

3. **An error decomposition that localises the entire research problem.**
   Precision is 1.000 on both mechanisms where IFC authors the answer
   (`connected_by_door`, `vertically_connected`) and 0.558 on the one inferred
   relation (`open_passage`), whose geometric signal is measurably absent —
   confirmed vs. rejected passages differ by as little as 0.01 m in one
   building's free-gap width. **[VERIFIED]** `docs/AUDIT_2026_09_18.md` §5.

4. **A leak-free, damage-invariant multimodal corpus.** Two visual conditions
   (architectural plan, binary occupancy) rendered for 208 real + 600
   synthetic buildings, shown to be pixel-identical across four levels of
   IFC-text damage that remove up to half a building's stated rooms — a
   controlled axis for measuring what vision adds when text degrades.
   **[VERIFIED]** `main/render_blind.py`, `tests/test_blind_render.py`.

5. **A multimodal model comparison on that axis.** **[TODO]** — the grid has
   not been run. See `todo.md` P3.

6. **A hybrid extraction method** that is deterministic where the error
   decomposition shows nothing can be improved, and learned only where
   geometry is silent, with the geometric root cause (A01) repaired because it
   independently damages both the method and the evaluation reference.
   **[TODO]** — findings A01/A04 are open by design; see `todo.md` P4.

**Explicitly not a contribution, by decision:** downstream task improvement
(e.g. on BIM-Edit) is cited as motivation only, not measured. Double
annotation / inter-annotator agreement is deferred. Both decisions were made
18 September 2026 and are recorded in the plan file, not revisited here unless
reviewer feedback (imaginary, pre-submission) forces it.

---

## Related work (pointers, not drafted)

- **IFC → graph / scene-graph methods**: Ifc2Graph, IFC2SB — both run as
  baselines in this project (§ Experiments), not just cited. Ifc2Graph has no
  room-recovery stage (fails by construction once `IfcSpace` is stripped);
  IFC2SB builds and runs but produces no scorable output on any
  ground-truth-verified building in our corpus. **[VERIFIED]**
  `main/baselines.py`, `docs/DESIGN_v3.md` §10/§13.
- **Open-vocabulary / language-grounded scene graphs** (ConceptGraphs, HOV-SG,
  Clio, SayPlan, EmbodiedRAG, Taskography): solve semantic richness *without*
  a prior map; this work solves it *with* one (the IFC), which changes what is
  worth estimating vs. verifying. Citation list already assembled in
  `icra_paper/RoboBIM_ICRA27_Paper/paper.tex:75` — reusable, the framing sentence is
  reusable, the surrounding claims are not (see `todo.md` P5).
- **LLM-for-IFC / BIM editing**: BIM-Edit (arXiv 2606.20146, user co-authored)
  is the direct motivating result — 49.5% best model, ≤3.4% full-task solve —
  and should be the paper's opening citation, not a related-work aside.
- **Multimodal floor-plan understanding**: needs a literature pass.
  **[TODO]**, `todo.md` P5.

---

## Method

### 3.1 Representation

Building → storey → space → element hierarchy; `connected_by_door`,
`open_passage`, `vertically_connected`, `adjacent_to` edge types, each with its
own attributes (`portal_id`, width, `basis` for vertical edges). Every node and
edge tagged by provenance (`ifc` / `inferred` / `fallback`). **[VERIFIED]**,
already built and stable — `README.md` "What it produces".

### 3.2 The hybrid principle

Motivated directly by the error decomposition (Contribution 3): a method
should not be learned where a deterministic signal is already exact.
`connected_by_door` and `vertically_connected` stay deterministic
(door/opening products, stair solids). `open_passage` and room semantics are
where geometry is silent or IFC states nothing — the only places a learned
component can add value that the deterministic path cannot already provide.

### 3.3 Geometric repair (A01, and why it is prioritised)

Room recovery currently takes an XY projection instead of a section, so a
lintel seals a doorway. This is finding A01, tracked as an open, deliberately
unrepaired defect (`tests/test_m0_regressions.py` asserts it still
reproduces). **New this planning pass**: A01 is not only a method defect — it
is *also* the diagnosed cause of the residual disagreement in the
annotation-free real-corpus reference (17 of 379 door-backed pairs still
disagree post-rebuild, all attributable to doors lacking an authored
`IfcOpeningElement` void, which the XY projection then seals). Repairing it
therefore improves the method's recall **and** the only independent
real-corpus reference's reliability in one change — the paper's strongest
argument for why this specific repair, and not A03/A04/A05, is the one worth
spending the method budget on. **[VERIFIED]** diagnosis,
`docs/AUDIT_2026_09_18.md` §4; **[TODO]** the repair itself, `todo.md` P4.
`_section_segments` (`main/render_floorplan.py:83-122`) is the primitive —
already correct, already used for rendering, never wired into recovery.

### 3.4 Width repair (A04)

The call site that actually admits false positives is
`main/bimsg/topology.py:359-360` (axis-aligned bounding box; median width of
*rejected* passages is 3.51 m, maximum 15.74 m) — not `_door_width`
(`topology.py:253-257`), which the original audit tracked but which affects
only 0.2% of doors. **[TODO]**.

### 3.5 Multimodal extraction — **[BUILT 21 Sep 2026, unrun]**

Two extraction regimes:
- *Per-room classification with graph context* (§4.4). A proxy task, not the
  paper's claim, and reported as such.
- *Whole-graph extraction* — the paper's actual multimodal claim. Given one
  storey as text, as a rendered plan, as an occupancy raster, or as text plus
  plan, the model emits that storey's rooms (with functions) and the pairs
  that open onto each other, in one pass.

The harness exists: `main/bimsg/graph_extract.py` (prompts, parser, graph
builder) and `main/extract_graph_llm.py` (CLI, backends, provenance,
manifest). **It defines no new metric.** The model's answer is parsed into a
graph in the pipeline's own shape and scored by the same
`evaluation.scoring.score_connectivity` / `score_labels` that scores the
deterministic method — so the multimodal comparison and the method comparison
share one definition of precision, and the paper cannot be accused of grading
its own method on a friendlier curve.

Two scope decisions to state in the paper rather than bury:
1. **One storey per call**, because a plan image is one storey and per-storey
   text keeps the modalities comparable.
2. **Vertical links are out of scope**, removed from the prediction *and* the
   reference (`GroundTruth.restrict`, which counts what it drops). The
   deterministic path recovers them at precision 1.000, so a model has nothing
   to win there.

Parse failures are counted and named, never repaired: a non-answer scores as
an empty prediction. This is the general form of the `_coerce()` defect that
faked a published number (§4.4), and the guard is verified against 11
deliberately injected defects. An `oracle` backend scores the reference
through the real parser and scorer and must return 1.000; it does, on
4 modalities × 4 damage levels × 300 units and on the real corpus.

**The vision task is set under the best conditions available**, because the
question is whether a VLM can reason about a plan, not whether it can regress
pixel coordinates. The primary vision condition is **set-of-mark**: each
IFC-declared room carries a numbered disc and the model refers to rooms by
number, so localisation is removed from the task and what is measured is room
function and connectivity. Asking for bare pixel centroids instead does not
measure plan reading -- probed on an 1800x1262 render, GLM-4.1V-9B-Thinking
got the room count exactly right and still matched almost nothing, because
every centroid landed on a round 50 px grid in one corner of the image. That
strict condition is still reported, as the localisation result it is.

Two things any set-of-mark number must carry: the marks reveal the room
partition, so **room recovery is not measured and room recall is 1.000 by
construction** for the declared set; and marks follow *declared* rooms only,
so at L2+ whole storeys carry no marks and drop out of the condition.

**Known limitations to report:**
- The unmarked vision modalities are scorable only on the synthetic corpus.
  The real corpus's derived reference states no room extent, so an
  image-conditioned prediction has nothing to match against and is recorded
  `unscorable` rather than scored 0.000. Set-of-mark does work there, because
  a mark resolves to the room's GlobalId, which the derived reference keys on.
- **The synthetic corpus cannot test vision labelling.** Its buildings hold
  only `IfcWall`, `IfcDoor` and `IfcStair` -- no furniture, no windows -- so a
  render carries almost no functional cue and a probe scored labels 0/22.
  Vision labelling belongs on the real corpus.

---

## Experiments

### 4.1 Connectivity recovery, synthetic corpus — **[VERIFIED], done**

150 buildings × 4 damage levels (L1 pristine-as-Revit-exports through L4
undeclared units + no spatial containment), 1,708 positive and **1,211
explicit negative** pairs. `ours` vs. `schema`, `adjacency`, `door_near`,
`ifc2graph`.

| method | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| `schema` | 0.000 | 0.000 | 0.000 | 0.000 |
| `adjacency` | 0.696 | 0.635 | 0.635 | 0.635 |
| `door_near` | 0.916 | 0.858 | 0.858 | 0.858 |
| `ifc2graph` | 0.738 | 0.491 | 0.000 | 0.000 |
| **`ours`** | **0.998** | **0.899** | **0.899** | **0.899** |

`ours` beats `door_near` at $p<10^{-6}$ (paired Wilcoxon, 20,000-resample
bootstrap CI, $n=150$) at every level; specificity 0.993 at L1. Independently
verified ground truth: 2,769/2,769 same-storey pairs agree (raster flood-fill,
a different mechanism from the scoring pipeline). Reproduced at full
floating-point precision by an independent pipeline run.
`docs/SYNTH_BENCHMARK_2026_09_18.md`, `report/results_2026_09_18.tex` §3.

Caveats the paper must carry: the damage axis has two regimes not four (L2–L4
converge; what moves the number is loss of `IfcSpace`, not the later levels);
`schema`'s specificity of 1.000 is degenerate (it predicts nothing);
`ifc2graph`'s collapse at L3/L4 is structural, not a fair loss.

### 4.2 Connectivity recovery, real corpus — **[VERIFIED], done, two references**

25 real Revit exports, 1,027 rooms. Two independent references, deliberately
contrasted as the paper's central methodological point:

**(a) Human-annotated** (correction-based): P 0.876 / R 0.966 / F1 0.919,
**specificity structurally 0.000** — not quotable as a correctness signal, and
the paper must say why, not just report the number.

**(b) Annotation-free, flood-fill-derived** (independent of the pipeline): P
0.789 / R 0.904 / F1 0.842, **specificity 0.644** — cross-validated two
independent ways to the digit. This is the paper's real-corpus headline.

`report/results_2026_09_18.tex` §4.

### 4.3 Error decomposition — **[VERIFIED], done**

`connected_by_door` 240/240 confirmed, precision 1.000. `vertically_connected`
67/67, precision 1.000. `open_passage` 67/120, precision 0.558 — every measured
error in the system. Geometric signal absent in both directions (inscribed-gap
measurements per building). `report/results_2026_09_18.tex` §5.

### 4.4 Semantic labelling — **[VERIFIED, all three backends]**

Leave-one-out over 592 IFC-stated rooms, re-measured 21 September 2026 with a
non-answer accounting fix in place (both `VLMBackend` and `LLMBackend`
previously substituted the rule backend's own hypothesis for a non-answer;
95/592 VLM rooms hit this, ~coincidentally the model's entire margin over
majority-class guessing — the mechanism is documented in
`tests/test_vlm_answer_accounting.py`, verified to fail against the pre-fix
code):

| backend | accuracy | vs. majority (0.346) |
|---|---|---|
| rule (hand priors) | 0.503 | +0.157 |
| **LLM (GLM-4-9B-0414)** | **0.556** | **+0.209** |
| VLM (GLM-4.1V-9B-Thinking) | 0.497 | +0.150 |

Fix: `max_new_tokens` raised 300→1536 (a diagnostic probe found the model's
`<think>` trace needs a median 85, p90 245, max 1004 tokens before closing),
and both backends now return `(label_or_None, status)` via `_coerce_strict`,
which never substitutes a prior. Rerun: 590/592 answered (99.7%, 2 truncated,
0 unparseable); accuracy over answered rooms only (0.498) is within noise of
the headline (0.497), confirming the fix didn't just relocate the defect.

### 4.5 Multimodal extraction grid — **harness [BUILT], results [TODO]**

The paper's core empirical gap. The harness is built and verified (Method
§3.5); no model has been run through it yet, so every number in this section
is still outstanding.

Grid: **[TODO — finalise model list against cached weights]** open-weights
text models × open-weights VLMs × {text, plan image, occupancy image,
text+plan} × 4 damage levels × {labels given, labels masked}. Primary corpus
synth150, where specificity is computable; the real corpus secondary, and
**text-only there** until the derived reference carries room extents.

The headline the grid is built to produce: the plan and occupancy renders are
**byte-identical across L1–L4** while the text loses half its `IfcSpace`
declarations, so the modality gap as a function of text damage is measurable
directly rather than inferred. That is the controlled experiment the whole
damage axis was for. `todo.md` Phase C.

Run the `oracle` backend on each new corpus before spending GPU time on it —
it costs seconds and it is the only thing standing between a harness bug and
an unfalsifiable model number.

### 4.6 Ablation ladder (method repairs) — **[TODO]**

Score synth150 × 4 before/after A04, before/after A01, before/after both. This
is the paper's main results table for the method contribution — currently
empty. `todo.md` P4.

### 4.7 Goal resolution — **[VERIFIED], secondary result**

298/592 = 0.503, but explicitly flagged as *not* an independent measurement:
per-class recall matches label accuracy for 11 of 12 classes. `legged` and
`human` reachability are byte-identical (252/298) — a fresh reproduction of
open finding A05 (two-valued feasibility), useful as a limitations point, not
a headline. `report/results_2026_09_18.tex` §7.

---

## Limitations (draft list, to expand once §4.5–4.6 land)

- Specificity on the real corpus is only available via the annotation-free
  reference, which covers 17 of 25 models (its own trust gate).
- Four of twelve semantic classes never resolve in evaluation, each with ≤10
  held-out examples — genuine evidence gap, not claimed as a method failure.
- The damage axis's L3 step is fully absorbed by the recovery pipeline; the
  paper should state plainly that severity is effectively 2-valued for
  connectivity, not oversell 4 levels.
- Vertical (cross-storey) connectivity in the synthetic reference is asserted
  by the generator and not independently checked by the flood-fill verifier,
  which only rasterises one storey at a time.
- Downstream task benefit is argued from BIM-Edit's numbers, not measured on
  this system.

---

## Figures needed (none currently exist in a usable form)

1. System / pipeline overview.
2. Error decomposition bar chart (the three-relation precision split) — this
   is probably the paper's best single figure; the numbers already exist.
3. Damage-axis illustration: one building's rendered plan, byte-identical
   across L1–L4, next to its degrading IFC text — makes the multimodal
   argument visually in one panel.
4. V-plan / V-occ example pair for one building.
5. Qualitative failure case for `open_passage` (a rejected passage with its
   measured gap width annotated).
6. Ablation ladder plot (F1 vs. repair stage).
All 17 workshop-paper figures are unusable (superseded pipeline, buildings not
in this corpus) — none are salvageable starting points. `todo.md` P5.

---

## Writing plan (calendar-scoped against the 16 Nov deadline)

| Window | Content |
|---|---|
| Now → the multimodal grid landing | §4.5 design + build (Method 3.5, `todo.md` P2–P3) |
| Grid running / method repairs | §4.6 ablation ladder (`todo.md` P4), draft §1–3 in parallel — nothing in them depends on the grid's outcome |
| After both land | Fill remaining **[TODO]** numbers in this file and in the abstract, draft §4–6, figures |
| Final ~10 days | Related work literature pass, limitations, polish, internal review pass against `docs/AUDIT_2026_09_18.md`-style fact-checking before submission |

Do not draft §4.5/§4.6's prose before the numbers exist — every other section
in this project that did that had to be walked back at least once.
