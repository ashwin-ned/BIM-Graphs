# BIM-Graphs / RoboBIM

Recovering a navigable building prior from IFC models that do not state one.

**State, 14 September 2026.** A [code audit](docs/CODEBASE_AUDIT_2026_09_13.md)
found twelve findings that blocked new correctness claims. The architecture was
redesigned around them ([system design](docs/ROBOBIM_SYSTEM_DESIGN.md),
[rationale and research plan](docs/PIPELINE_REDESIGN_2026_09_13.md)) and
milestone **M0 — the evaluation and serialization contracts — is
[implemented](docs/M0_IMPLEMENTATION_2026_09_14.md)**. The geometry findings
(A01, A03, A04, A05) are **not** repaired; `tests/test_m0_regressions.py`
asserts they still reproduce, so a fix fails its case and has to be flipped
deliberately.

```bash
python tests/test_m0_regressions.py     # 23 repairs hold, 4 findings still open
./run_pipeline.sh                       # generate, verify, damage, recover, score
```

## The problem

IFC encodes the geometry and semantics a robot needs, but not the topology.
Measured over 25 real Revit exports (257 MB):

| | |
|---|---|
| `IfcRelSpaceBoundary` instances — the schema's own connectivity | **0** |
| Storeys containing at least one `IfcSpace` | 74 / 146 (51%) |
| Elements assigned to a room | 2,001 of 17,350 (12%) |
| Rooms carrying a functional label | 671 / 681 (98.5%) |

**IFC is a strong source of semantics and a weak source of topology.** The work
is therefore *recovery*, not translation: which rooms exist, and which of them
actually connect.

A caveat the audit added and this README previously lacked: a missing
space-boundary relation is a fact about **this corpus**, not about IFC. Other
relations — `IfcRelVoidsElement` / `IfcRelFillsElement` chains in particular —
are present and are used. And a shared boundary would not have meant a passage
anyway; [buildingSMART's definition](https://standards.buildingsmart.org/IFC/DEV/IFC4_3/HTML/lexical/IfcRelSpaceBoundary.html)
admits physical and virtual delimiters, and a solid party wall is a boundary.

## What it produces

```
building → storey → space → element                     (hierarchy)

space ↔ space   connected_by_door     {portal_id, door, width}
space ↔ space   open_passage          {portal_id, width, openness}
space ↔ space   vertically_connected  {portal_id, kind, via, rise, basis}
space ↔ space   adjacent_to           {gap}
```

Every node and edge is tagged `ifc` / `inferred` / `fallback`, so a consumer can
tell what was read from the file and what was reconstructed.

**Every transition has its own identity.** The graph is keyed on `portal_id`,
not on the relation name. Two doors between the same pair of rooms are two
portals; a robot that fits through one may not fit through the other. Until M0
they collapsed into one edge, and which one survived depended on the order IFC
products appeared in the file.

`vertically_connected` also records `basis` — `stair_solid` when a stair solid
carved it, `stacked_label` when it exists only because two rooms are both called
"Stair". They are different kinds of evidence and anything holding out a label
has to be able to tell them apart.

Traversability is **not** baked in. The planner supplies a robot profile and
edges are filtered at query time:

```python
from bimsg import graph as G, navigation as N
g = G.load("results/model_0.graph.json")
N.reachability(g, profile="wheeled")
N.plan(g, start, goal, profile="legged")
```

## Results

### Against human annotation — the one that counts

15 real buildings reviewed, 10 with complete verdict coverage of the plan.
Scored with `main/eval_connectivity.py` against
`ifc-graphx/annotated_gt/`:

Re-measured 18 September 2026, after `model_2` was rebuilt from its intact
source IFC. The previous figures in this table (0.878 / 0.982 / 0.927, strict
0.903, edge-type 380/380) were measured before that rebuild and are superseded:

| | |
|---|---|
| Precision on pairs a reviewer ruled on | **0.876** |
| Recall | **0.966** |
| F1 | **0.919** |
| Strict F1, charging out-of-scope and unmatched-endpoint claims | 0.882 |
| Edge-type accuracy on matched pairs | 366/374 = 0.979 |
| Specificity | **0.000** (53 of 53 reference negatives contradicted) |

Four things must be quoted alongside it, and none of them is optional:

1. **A trivial baseline scores higher on precision.** Joining every pair the
   reviewer examined scores 0.918 against 0.876. The reviewed negatives are 53
   against 387 positives, so this slice is not discriminative. Report base
   rates; do not adjust sampling until the proposed method wins.
2. **83% of judged items were swept in** with "the rest of this floor is
   correct". That is ground truth, but it was not looked at item by item.
   `main/evaluate.py` prints the fraction.
3. **26 predicted transitions rest on 30 rooms a reviewer rejected.** Before
   M0 these left the accounting entirely, so an invented room and the edges
   hung on it cost a method nothing.
4. **The edge-type figure was 380/380 and is now 366/374, and the drop is the
   informative part.** `annotator/compose.py:185` copies the *pipeline's own*
   relation into the reference, so this metric compared the pipeline to itself
   and could only ever read 1.000 — a tautology. Restoring `model_2`'s 8
   missing doors changed the pipeline's relation for 8 pairs the reference
   still records as `open_passage`, and every one of the 8 confusions is
   `open_passage → connected_by_door`. So the metric does not measure
   correctness either before or after: it measures *whether the pipeline has
   changed since the reference was frozen*. Do not quote it as accuracy.

**`model_2` is the sole source of the degradation and its ground truth is now
stale.** It contributes 11 of the 13 false negatives and one unmatched
reference room, scoring recall 0.780 where every other building scores 1.000
or 0.949. Its verdicts were collected against the truncated file, whose missing
doors had pushed its connectivity onto the `open_passage` mechanism. Re-judging
the delta — 3 rooms, 13 links, 1 floor link — restores it; until then this
table understates recall for a reason that has nothing to do with the method.

Verdict-level scoring over all 15 buildings (`main/evaluate.py`):
room precision 0.924 over 552 judged rooms, link precision 0.836 over 482
judged links, vertical-link precision 0.987 over 79.

### Against generated ground truth

Clean synthetic corpus, 8 buildings, one-to-one room matching:
precision 1.000, recall 1.000, edge-type accuracy 77/77, no prediction outside
the reference's scope. Ground truth independently verified by raster flood-fill,
**128/128 pairs** on this set.

**The 150-building sweep is re-run and current, not archived.** Full detail,
including specificity, significance testing (paired Wilcoxon and a 20,000-resample
bootstrap CI, n=150) and per-level caveats, is in
[docs/SYNTH_BENCHMARK_2026_09_18.md](docs/SYNTH_BENCHMARK_2026_09_18.md). F1
against known ground truth, every damage level:

| method | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| `schema` | 0.000 | 0.000 | 0.000 | 0.000 |
| `adjacency` | 0.696 | 0.635 | 0.635 | 0.635 |
| `door_near` | 0.916 | 0.858 | 0.858 | 0.858 |
| `ifc2graph` | 0.738 | 0.491 | 0.000 | 0.000 |
| **`ours`** | **0.998** | **0.899** | **0.899** | **0.899** |

`ours` beats `door_near`, its strongest baseline, at p < 1e-6 at every level
(131/0/19 win/loss/tie at L1). Ground truth independently verified by raster
flood-fill: **2,769/2,769 pairs agree** — the one figure the superseded ICRA27
draft quotes that turns out to still be exactly right. Note what it covers:
`main/verify_gt.py` rasterises one storey at a time, so those 2,769 are the
same-storey decided pairs. The corpus decides 2,919 pairs in total; the
remaining **150 are the cross-storey stair links, one per building, and this
mechanism does not check them**. Vertical connectivity in the synthetic
reference rests on the generator's own assertion.

**The pipeline is deterministic end to end, and that is measured rather than
assumed — but be precise about what it shows.** `--regenerate 150` on
18 September 2026 regenerated the corpus from `main/generate_building.py`'s
default `--seed0 0` and scored all 20 (method × level) cells **identically at
full floating-point precision**, not merely to three decimals. Checked against
the archived corpus: the 150 `.gt.json` ground-truth files are **byte-identical
150/150**, while the 150 `.ifc` files are **byte-identical 0/150** — every input
file differs in its GUIDs and header, and the ground truth it encodes does not.
So this demonstrates the parse → recover → score chain is invariant to IFC
identifier churn, which is worth having. It is **not** a fresh sample of
buildings and says nothing about variance across different layouts; the same
seed yields the same rooms by construction. A sampling-variance claim would need
`--seed0` varied, which has not been run.

The qualitative findings stand: `schema` recovers **nothing** at any damage
level because there is nothing in the file to read;
[Ifc2Graph](https://github.com/JBjoernskov/Ifc2Graph) has no room-recovery
stage, so stripping `IfcSpace` makes true connections invisible to it (hence
0.000 at L3/L4, where recovery becomes required rather than optional); and
[IFC2SB](https://github.com/RWTH-E3D/IFC2SB) builds and runs but produces no
output on any ground-truth-verified building, so no F1 exists to report for it.
Full evidence in [docs/DESIGN_v3.md](docs/DESIGN_v3.md) §10 and §13 (superseded
document, retained for its measurements).

### Real corpus, necessary conditions

25 real models → **1,027 rooms** (681 stated, 346 recovered — up from 1,025/344
now that `model_2` is built from its intact source IFC rather than the
corpus's one truncated copy), all graphs loadable, navigation and QnA
functional on every one. `main/validate.py`: R2 doors-join-two-rooms,
R3 one-storey-per-room, R4 plausible dimensions, R6 containment sanity all pass
**25/25**.

**Do not read intra-storey reachability as a quality score.** It rewards
over-connection: the `adjacency` baseline — which joins through solid walls —
scores 0.935 on it against our 0.745. Necessary conditions can falsify a graph;
they cannot rank one.

### Semantic labelling

Held-out accuracy against the labels IFC did state, leave-one-out, all **592**
eligible rooms (593 before `model_2.graph.json` was rebuilt from the corpus's
one truncated source IFC — see [docs/AUDIT_2026_09_18.md](docs/AUDIT_2026_09_18.md)).
Re-measured 21 September 2026 with the non-answer accounting fix in place
(previously the rule backend's own hypothesis was silently substituted for a
model's non-answer — see below):

| backend | accuracy | vs. majority class (0.346) |
|---|---|---|
| rule | 0.503 | +0.157 |
| **LLM (GLM-4-9B-0414)** | **0.556** | **+0.209** |
| VLM (GLM-4.1V-9B-Thinking) | 0.497 | +0.150 |

All three numbers are now clean: every non-answer is charged as wrong, not
substituted with the rule prior. The LLM number is real signal, not noise
around the rule baseline: +0.052 over rule, and office recall alone moves
0.698 → 0.839. It also runs fast — 592 rooms in under two minutes once the 9B
weights are loaded.

Both numbers use one vocabulary. `main/bimsg/semantics.py` used to carry its
own copy of the taxonomy with two dict-order and normalisation bugs; it now
delegates to `evaluation/labels.py`, versioned and covered by
`tests/test_labels.py`. The rule figure moves slightly with each vocabulary fix,
which is expected and is why the vocabulary version travels with every score
`evaluation.scoring.score_labels` produces.

**The VLM number was unreliable and has been fixed, not merely bounded.** It
renders a crop per room and asks a "thinking" model for one word after a
`<think>...</think>` trace. Two defects compounded on the 18 September 2026
run: (1) the token cap was 300, far below the trace length this model actually
needs (diagnostic probe: median 85 tokens to `</think>`, p90 245, max 1004 over
24 rooms); (2) both `VLMBackend.predict_image` and, discovered while fixing the
former, `LLMBackend.predict` silently substituted the rule backend's own
hypothesis (`_coerce(text, fallback=hyp)`) whenever the model's output didn't
parse — so a non-answer scored as the rule baseline's answer, not as wrong.
**95 of 592 rooms (16.0%) hit this on the VLM path** — coincidentally almost
exactly the model's own margin over majority-class guessing, which is what
made the original 0.505 figure look plausible while being unmeasurable.

Both are fixed: `max_new_tokens` raised to 1536 (informed by the diagnostic
probe above), and both backends now return `(label_or_None, status)` with
`status ∈ {ok, truncated, unparseable}` via a new `_coerce_strict` that never
falls back to a prior (`main/bimsg/semantics.py`; regression-guarded by
`tests/test_vlm_answer_accounting.py`, verified to fail against the pre-fix
code). Rerun 21 September 2026: **590/592 answered (99.7%), 2 truncated, 0
unparseable** — accuracy over answered rooms only 0.498, essentially identical
to the headline 0.497, confirming the fix did not just hide the problem in a
different bucket. Worst classes (all n ≤ 10): outdoor, storage, retail, service
at 0.000 recall; dominant confusions are `bathroom→storage` (53) and
`office→living`/`office→storage` (33/30), consistent with the model reading
furniture/clutter off the render rather than topology.

```bash
python main/eval_semantics.py --backend vlm --graphs 'results/model_*.graph.json'
```

`evaluation.scoring.score_labels` (added this session) charges an unparseable
answer as `pred_unmapped` rather than silently scoring it as correct-by-default;
the backends should move onto that accounting entirely, not only the
leave-one-out harness above.

The taxonomy investigation stands: a real bug (a `dining` hint returning a
non-class), German kitchen furniture names the English-only keyword list never
matched (kitchen recall 0.000 → 0.360), and a `Railing`-implies-outdoor rule
checked and **rejected** because railings appear in 19 stair and 17 circulation
rooms against 7 outdoor ones. Four classes (outdoor, storage, retail, service)
remain at 0.000 recall with ≤10 held-out examples each.

### Goal resolution

`main/eval_goal_resolution.py`, re-measured 18 September 2026 (the previous
number, 0.519, was quarantined under
[results/_pre_m0_snapshot/](results/_pre_m0_snapshot/) for finding A02 and
should not have been quoted):

| | |
|---|---|
| resolution accuracy | **298/592 = 0.503** |
| reachable, wheeled profile | 91/298 = 0.305 |
| reachable, legged profile | 252/298 = 0.846 |
| reachable, human profile | 252/298 = 0.846 |

**Two things to state alongside it, not after it.** First, this is not an
independent measurement of retrieval: per-class recall here is identical to the
rule-backend label accuracy above for 11 of 12 classes, because resolving "go to
the kitchen" mostly succeeds or fails on whether the room was labelled correctly
in the first place, not on the retrieval step. Second, `legged` and `human`
report **byte-identical** reachable counts — 252/298, exactly — which reproduces
open finding A05 (feasibility is two-valued; a route can rest on an unverified
connection) in a fresh measurement rather than only in the regression fixture.

### The annotation-free derived reference

`main/derive_real_gt.py` recovers connectivity from geometry alone by
rasterising walls and flood-filling free space — a different mechanism from the
pipeline's door-normal probing, so agreement between them is evidence.

**It was measurably broken until M0 and has been rebuilt.** It scaled
tessellated geometry by the declared unit a second time, when IfcOpenShell had
already emitted metres, and it filtered doorway voids with the room-area
threshold. Across the 25 real models:

| | doorway voids recovered |
|---|---|
| before | 445 |
| after | **1,363** |

On the **seven models that declare millimetres, the old code recovered zero
voids**. The flood-fill then found every room sealed and emitted a long list of
confident negatives that were artifacts of the declared unit.

The reference has been rebuilt over the 25-model corpus. Comparing it against
the archived one, on the same models:

| | before | after |
|---|---|---|
| usable models | 15 | **17** |
| connected pairs | 370 | **460** |
| separated pairs | 379 | **289** |

**346 of roughly 749 previously decided pairs changed verdict** — 218 from
separated to connected, 128 the other way. Both directions are corrections and
both halves of the repair are responsible: recovering the authored voids opens
pairs that were falsely sealed, and giving blocking geometry its own minimum
area means a thin wall stub now counts as a blocker instead of being discarded
with the room-sized threshold. Nothing computed against the previous reference
is comparable to anything computed against this one.

Scoring the pipeline against the rebuilt reference (`main/eval_real_connectivity.py`,
17 scorable models): precision 0.720, recall **0.932**, F1 0.812 (room-contracted);
0.789 / 0.904 without contracting recovered rooms. The recall figure is the
striking one — the old reference was marking as separated a great many pairs the
pipeline had correctly called connected, and penalising it for being right.

**This reference is discriminative, and it was never scored for that until
18 September 2026.** `docs/REAL_CORPUS_GT.md` carried a section titled "why
these numbers must not be used", arguing that 428 door-backed pairs disagreed
with the reference against only 300 that agreed. That argument predates this
rebuild. Recomputed against the reference now on disk: **362 agree, 17
disagree** — a 96% reduction nobody had re-checked. That clears the reference to
report the one number the annotated ground truth structurally cannot produce:

| | |
|---|---|
| positives | 425 |
| **negatives (genuine, independent of the pipeline's own proposals)** | 289 |
| precision | 0.789 |
| recall | 0.904 |
| **specificity** | **0.644** |

Cross-checked two independent ways — `eval_real_connectivity.py`'s
uncontracted mode (tp 384, fp 103) and `evaluation.scoring.score_connectivity`
via `evaluation.matching.match_rooms` (fp_on_reference_negative 103,
gt_negatives 289) — and they agree to the digit. For contrast, the *annotated*
reference gives specificity **0.0000**: every one of its 53 negatives is, by
construction, a link the pipeline itself proposed and a reviewer rejected, so a
true negative is unrecordable from it at all. This reference does not share
that defect, because it is derived from geometry independently of what the
pipeline proposed.

The older caveat is smaller than it was but not gone: a wall's XY projection
still seals any doorway whose void the file never authored (finding A01), which
is precisely why the door-backed disagreement fell from 428 to 17 rather than
to 0 — repairing A01 in the recovery pipeline would remove the dependence on an
authored void entirely, improving this reference and the method's own recall in
one change. `precision_strict` here is 0.406, with 378 predictions on unmatched
endpoints and 80 out of scope: the pipeline asserts a great deal outside this
reference's smaller room set, and that is charged rather than hidden. Full
detail, including the stale numbers this section corrects, in
[docs/REAL_CORPUS_GT.md](docs/REAL_CORPUS_GT.md) and
[docs/AUDIT_2026_09_18.md](docs/AUDIT_2026_09_18.md).

## The evaluation contract

One definition of ground truth, in [evaluation/](evaluation/), shared by every
scorer. Before M0 each script had its own and they disagreed with each other and
with the annotator.

| module | responsibility |
|---|---|
| `schema.py` | reads all three ground-truth dialects — generator, derived flood-fill, human annotation — into one record, including **how rooms are identified** in each. Guessing that is how a GUID-keyed human export scored zero against a name-keyed evaluator. |
| `matching.py` | assigns predicted rooms to reference rooms one-to-one; keeps unmatched predictions, reports merges and splits |
| `scoring.py` | four prediction buckets, kept apart: on a reference negative, on an unknown pair, out of scope, hung on an unmatched room |
| `verdicts.py` | the annotation vocabulary, asserted identical across `compose.py`, the evaluator and `compose.js` |
| `manifest.py` | fixes the denominator before scoring |

Three rules it enforces:

- **Unknown is not negative.** A pair a reference abstained on, or a link a
  reviewer marked unsure, never enters precision.
- **Silence is not a negative either.** Reviewers saw the links the pipeline
  proposed; a pair nobody proposed was never judged. Do not compute a
  true-negative rate over all possible pairs from this data.
- **A missing run is a failed run.** Expected units are declared before scoring;
  `missing` and `failed` stay in the denominator and only an `excluded` unit
  with a stated reason leaves it. Scorers exit non-zero unless
  `--allow-missing`.

Inter-annotator agreement characterises label uncertainty. It is **not** a
ceiling on model accuracy — a method can be right where a reviewer was wrong,
and treating it as a bound would reward an annotation process that agreed by
being careless.

### Whole-graph extraction: a model scored by the same code as the method

`main/extract_graph_llm.py` asks a model for one storey's whole graph — every
room, its function, and the pairs that open onto each other — in four
modalities: `text`, `plan` (rendered plan), `occ` (occupancy raster), and
`text+plan`. It **defines no metric of its own.** The answer is parsed into a
graph in the pipeline's own shape and handed to the same `score_connectivity`
and `score_labels` that score the deterministic method, so both are compared by
one definition of precision.

Two scope decisions, stated rather than left implicit:

- **One storey per call.** A plan image is one storey, so per-storey text keeps
  the modalities comparable instead of asking the text condition to do more.
- **Vertical links are out of scope** — and are removed from the *reference*
  too, by `GroundTruth.restrict`, which counts the pairs it drops. The
  deterministic path recovers them at precision 1.000, so a model has nothing
  to win and points to lose by accident. Restricting only the prediction would
  turn a task definition into a measured failure.

Every parse failure is counted and named (`no_json`, `invalid_json`,
`schema_invalid`, `truncated`), with per-item counters for dropped rooms and
edges, and a failed extraction yields an **empty** graph rather than a partial
one. This is the general form of the `_coerce()` defect that faked a published
number; `tests/test_graph_extraction.py` was verified against 11 deliberately
injected defects.

**Run the oracle before spending GPU time on a new corpus.** It emits the
reference through the real prompt path, parser and scorer, and must score
1.000:

```bash
python main/extract_graph_llm.py --corpus dataset/synthetic_data/synth150 \
    --backend oracle --oracle-anchor pixels --modality plan
```

Use `--oracle-anchor pixels`, not the default. The `identity` anchor joins by
room id and touches no geometry: it reported OK against a deliberately
sign-flipped pixel→metre lift *and* with the spatial anchor deleted entirely,
while `pixels` caught both (recall 0.000, 69 rooms missed and 69 invented).
`pixels` is the only mode that tests what the vision conditions depend on.
A `replay` backend re-scores a previous run's logged responses, so fixing a
scorer costs no GPU hours.

Verified at 1.000 on 4 modalities × 4 damage levels × 300 units, and on the
real corpus (583/583 rooms matched, 522/522 labels). **The vision modalities
are not yet scorable on the real corpus:** its derived reference states a
centroid and an area but no room extent, so an image-conditioned prediction —
which invents its own room ids — has nothing to match against. Those units are
recorded as `unscorable` and leave the average rather than contributing a
0.000 that would describe the reference instead of the model.

## Layout

```
main/bimsg/parser.py      IFC → geometry records, with unit/placement repair
main/bimsg/spaces.py      room recovery from free space
main/bimsg/topology.py    storey repair, doors, passages, vertical links
main/bimsg/graph.py       graph assembly, portal identity, (de)serialisation
main/bimsg/navigation.py  robot profiles, planning, local navmesh
main/bimsg/qa.py          grounded spatial QnA, pluggable LLM backend
main/bimsg/semantics.py   rule/LLM/VLM room labelling

evaluation/schema.py      one ground-truth record, three dialects
evaluation/matching.py    one-to-one room assignment, merges and splits
evaluation/scoring.py     connectivity scoring in the matched space
evaluation/verdicts.py    the canonical annotation vocabulary
evaluation/manifest.py    expected units, so a missing run stays in the denominator

tests/test_m0_regressions.py  every audit finding, as a test that runs

main/generate_building.py procedural buildings with known connectivity
main/damage_ifc.py        graduated IFC damage (the severity axis)
main/verify_gt.py         independent flood-fill check of ground truth
main/derive_real_gt.py    annotation-free connectivity reference from geometry
main/baselines.py         ablations; `ours` calls the pipeline, never copies it
main/build_graph.py       batch-run the pipeline over a glob of IFC files
main/significance.py      paired Wilcoxon, bootstrap CI
main/validate.py          necessary-condition checks on real files

main/eval_connectivity.py      connectivity vs any reference, with a manifest
main/eval_real_connectivity.py the derived reference, with room contraction
main/evaluate.py               verdict-level scoring of human annotation
main/eval_semantics.py         held-out label accuracy, with dependency masking
main/extract_graph_llm.py      whole-graph extraction by a model, oracle-verified
main/eval_goal_resolution.py   language query → room → embodiment-aware plan

main/render_plan.py       highlighted room crops for the VLM, keyed by GlobalId
main/render_floorplan.py  per-storey architectural PNG/SVG floorplans
main/audit_dataset.py     schema-level IFC audit — produced the table above
main/label_corpus.py      labels + persists predictions on the real corpus
main/demo.py              end-to-end walkthrough: hierarchy, route, QnA
main/export_plans.py      pre-extracts plans for the annotation tool

annotator/                the ground-truth annotation tool — 15 buildings reviewed
ifc-graphx/               the tool's deployed copy and the annotation corpus
```

`ifc-graphx/annotator/` is a second copy of the tool, served as a static site.
`tests/test_m0_regressions.py` fails if any of the 25 shared source files
drifts between the two.

**Reports** (`research/`, each a compiled PDF with its `.tex` source) cover the
connectivity benchmark, the semantic-injection study, the literature review and
the RGB-D perception study this design replaced. Their connectivity and
semantics figures predate M0; read them with `results/_pre_m0_snapshot/README.md`
beside them.

## Things that cost real debugging time

Kept because they are invisible in the schema and fatal if unhandled. The first
three are now enforced in code and guarded by regression cases.

- **IfcOpenShell already emits metres.** Applying the file's unit scale again
  shrinks millimetre models by 10³ — and did, silently, in the derived
  reference, for seven of 25 models. Raw attributes (`Elevation`, placements)
  *do* need it. `ParsedModel` now keeps the declared scale and the empirical
  repair apart so the two cannot be confused.
- **One model declares no `IfcUnitAssignment`** while its coordinates are
  centimetres. Units are recovered empirically from floor-to-floor height,
  which is physically bounded to 2–6 m.
- **Never key a multigraph on the relation name.** Two doors between the same
  rooms become one edge, and which one survives depends on IFC product order.
- **Never name an edge attribute `source` or `target`.** It overwrites the
  endpoints on export and the graph cannot be reloaded. The serializer writes
  endpoints and key last for exactly this reason.
- **Revit gives sibling storeys near-identical GUIDs.** Keying derived nodes on
  a truncated GUID silently merges rooms across floors — and *raises* apparent
  connectivity while destroying the hierarchy.
- **Doors are barriers during room recovery and connections afterwards.** Skip
  the first and rooms merge; skip the second and every door is also reported as
  an open passage.
- **Union wall areas, never sum them.** Overlapping or double-sided walls
  double-count and suppress genuine openings.
- **A comparison harness must call the method, not re-implement it.** A stale
  copy in `baselines.py` invalidated a full set of results.
- **A vocabulary mismatch that scores zero is worse than one that errors.** The
  scorer read `correct`/`spurious` long after the annotator had moved to
  `real`/`not_a_room`. Nothing raised; 1,034 verdicts simply produced an empty
  table that looked like a small corpus. Retired verdict words now raise.
- **Masking a field is not masking its consequences.** Holding out a room's
  label left the graph edge that label had produced, and the classifier read it
  back as a feature.

## Known limitations

1. **Room recovery is the binding constraint**, especially on open-plan and
   glazed buildings where the enclosure is curtain wall rather than `IfcWall`.
2. **Room recovery does not take a section** (finding A01). Most products
   contribute the convex hull of their whole XY projection, so a lintel seals
   the doorway beneath it. The section primitive in `render_floorplan.py` is
   correct and unused by recovery. This is the first item of M1.
3. **Door probing can connect through a wall** (A03). It samples at fixed
   heights without checking the intervening segment, and will skip a 0.5 m room
   to reach the one beyond it.
4. **Reported width is not clear width** (A04). It is the longest world-axis
   bounding-box side, so a 1.0 m door rotated 45° reports 0.742 m.
5. **Feasibility is two-valued** (A05). A missing width reads as passable, and a
   legged route exists across two rooms both named "Stair" with no stair
   geometry anywhere. There is no `unknown`, and no route preconditions.
6. **Stair gradient is not recoverable** from these compact switchback cores
   (4.89 m rise in a 2.5 × 2.5 m footprint). Rise and clear width are reported;
   slope is omitted rather than fabricated.
7. **The 346 recovered rooms have no accuracy figure of their own.** Held-out
   label evaluation uses stated `IfcSpace`, which is geometrically cleaner than
   the rooms the stage actually serves. It is an optimistic bound.
8. **Four of twelve label classes never resolve** (outdoor, storage, retail,
   service), ≤10 held-out examples each. Whether that is too little evidence or
   an under-determined taxonomy is not established.
9. **Synthetic buildings are orthogonal and clean** by design, to isolate
   connectivity recovery. Real files are harder.

## Next — M1

Per [the design](docs/ROBOBIM_SYSTEM_DESIGN.md) §13: sections and support
geometry in place of footprint projection, spatial cells kept separate from
architectural spaces, portal side incidence with a local crossing witness, and
dimensional claims carrying explicit unknowns. That is findings A01, A03 and A04
together; A05 follows in M2 with the three-valued route contract.

The three development buildings still have to be chosen on observed geometry and
review quality. A complete-coverage flag is not evidence of suitability.

Re-running the 150-building sweep and the LLM/VLM semantic evaluation under the
repaired scorers is prerequisite to quoting any of those numbers again.
