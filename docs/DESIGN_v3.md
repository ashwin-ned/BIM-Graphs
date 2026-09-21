# BIM-Graphs — Design v3

> **Superseded, 14 September 2026 — retained for its measurements.** The
> architecture here is replaced by the
> [RoboBIM system design](ROBOBIM_SYSTEM_DESIGN.md) and the
> [pipeline redesign](PIPELINE_REDESIGN_2026_09_13.md); the
> [code audit](CODEBASE_AUDIT_2026_09_13.md) lists twelve findings against the
> implementation it describes. This document is kept because code, dataset
> READMEs and third-party notes cite specific sections for evidence that was
> genuinely measured — §10 (Ifc2Graph), §11 (goal resolution), §12 (taxonomy
> repair), §13 (IFC2SB). Its **connectivity and semantics figures predate the
> M0 scorer repairs** and should not be quoted; see the
> [M0 record](M0_IMPLEMENTATION_2026_09_14.md) and
> `results/_pre_m0_snapshot/README.md`. It also refers to
> `Hybrid_BIM2Graph_Design_Spec_v2.pdf`, which has been deleted as superseded.

Supersedes `Hybrid_BIM2Graph_Design_Spec_v2.pdf`. Records the decisions taken in
the design session of 2026-08-30, after the zero-shot perception study was
concluded and its infrastructure removed.

---

## 1. What we know (design constraints, all measured)

1. **IFC is strong on semantics, weak on topology.** 98.5% of stated rooms carry
   a functional label; **zero** `IfcRelSpaceBoundary` exist in 257 MB across 25
   real models. Only 51% of storeys contain any `IfcSpace`.
2. **Perception-from-renders is the wrong tool here.** Object-centric
   open-vocabulary pipelines discard walls and floors as background while
   hunting for furniture that BIM does not model. Confirmed against a Replica
   control; see `research/benchmark_report.pdf`.
3. **We still have no accuracy number of our own.** Necessary conditions can
   falsify a graph but never confirm one. This is the gap v3 exists to close.
4. **Exactness is the unfair advantage.** Every node traceable to a `GlobalId`,
   controlled ablation possible, topology measurable.

## 2. The claim

> **A method for recovering navigable connectivity from IFC files that provide
> none.**

Scored by **edge precision/recall against known ground truth**, as a function of
how badly the IFC export has been damaged.

Connectivity is the right target on three grounds: it is 100% absent in real
files, it is what navigation actually consumes, and no existing work recovers it
without assuming well-formed spaces.

## 3. Decisions

| Question | Decision |
|---|---|
| Primary contribution | A **method** (IFC → hybrid graph) |
| Primary consumer | A **robot planner** |
| Goal type | **Language goals** ("go to the kitchen") |
| Embodiment | **Parameterised** robot profile, applied at query time |
| Learned component | **LLM over graph features** for labels + affordances (no vision) |
| Output contract | **Topological + clearance metadata** |
| Correctness | **Synthetic buildings with known GT** |
| Difficulty axis | **IFC damage severity** |
| Damage model | **Strip space boundaries** — superseded during implementation by a graduated L1–L4 model, since boundary-stripping proved a no-op on generated files (§7, `main/damage_ifc.py`) |
| Generator | **Procedural** (multi-storey and building type as free parameters) |
| Real corpus | **Co-equal evaluation set** (annotated subset) |
| Novelty | **Connectivity recovery without space boundaries** |

### Consequences worth stating

- **Language goals make labelling load-bearing.** "Go to the kitchen" fails if
  the kitchen is `room_7`, and 344 of our 1,025 rooms are unlabelled — all of
  them recovered ones. Labelling therefore needs its own metric
  (goal-resolution accuracy), not a hand-wave.
- **Labelling needs no vision.** A 5 m² room containing an
  `IfcSanitaryTerminal` and adjoining a bedroom is an ensuite. That is symbolic
  inference over graph features, and our own ArchSIBench finding is that VLMs
  are weakest at exactly the configuration reasoning a floorplan would demand.
- **The benchmark is easier than reality, deliberately.** Generated IFC has
  clean spaces, containment and units; only space boundaries are stripped. This
  isolates the claim, but real files are harder — which is why the real corpus
  is co-equal rather than decorative.

## 4. Output contract

```
NODES
  building
  storey   {elevation}
  space    {label, area, centroid, height, source}
  element  {ifc_type, label, centroid, bbox, source}

EDGES (hierarchy)
  contains          building→storey→space→element

EDGES (connectivity)  ← the contribution
  connected_by_door     {door_id, width, height}
  open_passage          {width, openness}
  vertically_connected  {kind, via, rise, run, headroom}

Every node and edge carries provenance ∈ {ifc, inferred, fallback}.
Traversability is NOT baked in: the planner supplies a robot profile
(width, height, climb ability) and edges are filtered at query time.
```

The planner does its own metric planning. We expose topology plus the clearance
metadata needed to filter it — no navmesh, matching the novelty claim.

## 5. Evaluation

**Primary.** Edge precision/recall vs generator ground truth, swept over damage
severity.

**Secondary.** Goal-resolution accuracy for language goals; room
precision/recall; necessary conditions retained as regression tests.

**Real corpus.** Annotated subset of the 25 real models, via the existing
annotation tool. Requires inter-annotator agreement on at least one model to
separate method error from genuine ambiguity.

### Baselines

Planned at design time, with what actually happened once each was attempted:

| Baseline | Availability | Outcome |
|---|---|---|
| Schema-trusting (read `IfcRelSpaceBoundary` as-is) | we implement | scored: **0.000** at every damage level (§7) |
| **Ifc2Graph** (JBjoernskov) | ✅ public Python | scored: **0.501** clean / **0.476** damaged (§10) |
| **IFC2SB** (RWTH-E3D) | ✅ public C++/IfcOpenShell | built and run, **not scorable** (§13) |
| IFC-Graph (Zhu 2025) | ❌ no public code | not attempted |
| Lilis CBIP | ❌ no public code | not attempted |

Ablations of our own method (naive adjacency, nearest-door without orientation
probing) isolate where the gain comes from; both are scored in §7.

## 6. Retained and removed

**Retained.** Core pipeline (`parser`, `spaces`, `topology`, `graph`,
`navigation`, `qa`) including the robustness fixes that cost real debugging:
empirical unit detection, full-GUID node keys, placement-derived door normals,
`provenance` never named `source` on edges. Annotation tool. Evaluation harness.

**Removed.** RGB-D renderer, procedural textures, clutter injection,
ConceptGraphs/HOV-SG adapters, detector probe, image statistics — together with
the third-party stacks and Replica download (≈20 GB reclaimed). Findings are
preserved in `research/benchmark_report.pdf` and `results/*.json`.

## 7. Work plan

1. ~~Procedural building generator + GT export~~ — done; 150 buildings,
   ground truth independently verified (2769/2769 pairs, raster flood-fill)
2. ~~Refactor core pipeline to the v3 output contract~~ — done; connectivity
   F1 0.996 clean / 0.868 damaged vs. verified ground truth, beats all three
   ablation baselines at p<1e-6 (paired Wilcoxon, n=150)
3. ~~Damage model~~ — done; L2 and L3 land identically and L4 differs on only
   two buildings (both marginally *higher*), isolating room recovery as the
   sole binding constraint under damage
4. Baselines: ~~Ifc2Graph~~ — done, see §10. ~~IFC2SB~~ — build done, run
   attempted, found not scorable on this benchmark — see §13
5. ~~LLM labelling over graph features~~ — done, see §9. ~~Goal-resolution
   metric~~ — done, see §11
6. Real-corpus annotation (long pole — needs two annotators) — **not started,
   now the single largest gap**: neither connectivity nor semantic-labelling
   accuracy has been measured on the real corpus, only on synthetic/held-out
   proxies

## 9. Semantic injection — results

Implemented per §3's decision (LLM over graph features, no vision as the
default path). A code audit found the first implementation discarded the most
informative available signal (element names) before either the deterministic
or learned backend could use it; restoring it plus a system prompt plus fusing
the deterministic prediction into the LLM's own prompt as a hypothesis took
held-out accuracy from 0.200 (below majority-class) to 0.580 (+0.235 over
majority). Full results, per-class breakdown and threats to validity:
`research/semantic_injection_report.pdf`. Model: **zai-org/GLM-4-9B-0414**
(text) / **zai-org/GLM-4.1V-9B-Thinking** (vision, same backbone, for a fair
comparison). 333 real recovered rooms labelled and persisted via
`main/label_corpus.py`; those specific predictions have not been checked
against truth — see limitation below. (0.580/0.200 predate the taxonomy
repair in §12: post-repair, LLM is 0.590, rule is 0.545.)

**Vision vs. text was measured, not assumed.** First attempt at the vision
comparison silently returned the deterministic prior for all 200 rooms — a
6-token generation cap truncated GLM's reasoning trace before any answer, and
the fallback fired with no error, producing a "VLM accuracy" that matched the
rule backend exactly on the same sample. Caught by direct comparison against
the baseline, not by output inspection. Fixed (300 tokens, parse after
`</think>`, truncation now counted): on the matched sample, VLM (0.520) does
not exceed its own prior (0.530); text (0.580) exceeds its prior by 5 points.
15% of vision calls still hit the cap, so 0.520 is a lower bound.

## 10. External baseline — Ifc2Graph

[Ifc2Graph](https://github.com/JBjoernskov/Ifc2Graph) (Bjornskov, MIT,
published) does its own independent IfcOpenShell extraction and 3D
voxel-grid adjacency test over `IfcSpace` meshes — no code shared with our
parser. Run as-is (default 0.5 m voxel pitch) via `main/baselines.py
--methods ifc2graph`, scored by the same `eval_connectivity.py` used for
everything else, on the identical room set our pipeline recovers (a room we
infer geometrically that has no `IfcSpace` is invisible to this method by
construction, and correctly gets zero edges rather than a faked one).

One required NumPy 2.x compatibility patch (`np.int` → `np.int64`,
behaviour-preserving, one line, `third_party/Ifc2Graph/ifc2graph/ifcgeometry.py:180`).
Ran clean on all 300 buildings (150 clean + 150 L2-damaged), zero crashes.

| corpus | method | precision | recall | F1 |
|---|---|---|---|---|
| L0 (clean) | ours | 0.991 | 1.000 | 0.995 |
| L0 (clean) | ifc2graph | 0.335 | 1.000 | 0.501 |
| L2 (damaged) | ours | 0.923 | 0.831 | 0.874 |
| L2 (damaged) | ifc2graph | 0.523 | 0.436 | 0.476 |

Two findings, neither favourable to the baseline, both worth reporting
plainly rather than only the numbers that flatter our own method:

1. **On clean input, ifc2graph's precision (0.335) is below even our own
   naive full-connect ablation's reference bound (0.585).** It reports two
   spaces adjacent whenever they share a voxel boundary at 0.5 m pitch —
   coarser than our 0.20 m wall thickness, so a thin wall often doesn't get
   its own voxel row. It also treats rooms stacked directly on top of each
   other (a floor slab between them, no stair) as adjacent, which the GT
   correctly does not count as connected — confirmed directly: on
   `synth_0000` it reports `S0R000 -> S1R000` with no stair anywhere near
   either room.
2. **Recall collapses under damage: 1.000 -> 0.436.** It has no room-recovery
   stage — it only ever sees stated `IfcSpace` — so L2's ~49%-of-storeys
   `IfcSpace` stripping makes roughly half the true connections literally
   invisible to it. Vertical-connection recall specifically goes 150/150 (L0)
   -> 0/150 (L2): a stair edge needs `IfcSpace` on both ends, and damage only
   has to remove one. This is the same failure mode our own room-recovery
   stage exists to fix, measured this time in a tool that doesn't have it.

## 11. Goal resolution — does a language query find the right room?

Every result so far scores one pipeline stage in isolation. This closes the
loop the design turns on: consumer = robot planner, goal = language
(§1). `main/eval_goal_resolution.py` asks, for each held-out real room,
whether querying its true functional class actually retrieves it, and
whether the resulting room is then reachable by a concrete plan.

Method: reuses §9's leave-one-out masking exactly (`eval_semantics.collect`)
— one room's real label is hidden, `RuleBackend` predicts a label from graph
context alone (as it would for a room the pipeline recovered and nobody ever
named), and the query is that room's true canonical class. Resolution
(`bimsg.qa.BuildingIndex.find_rooms`, extended to also match a room's
`predicted_label`, not only its stated one — the change that makes queries
resolve unlabelled rooms at all) runs against the masked room's predicted
label sitting among every other room's real one — the actual deployment
mix. Success = the true room is in the returned candidate set.

Numbers below are post-taxonomy-repair (§12); the repair was found and
applied by investigating exactly this table's zero-recall row, then this
eval was re-run to confirm the fix reached end-to-end. Pre-repair: overall
299/593 = 0.504, kitchen 0/25 = 0.000.

| | resolved | n |
|---|---|---|
| overall | 308/593 | 0.519 |
| office | 144/205 | 0.702 |
| bathroom | 68/130 | 0.523 |
| stair | 36/45 | 0.800 |
| bedroom | 22/28 | 0.786 |
| kitchen | 9/25 | 0.360 |
| circulation | 22/82 | 0.268 |
| meeting | 4/24 | 0.167 |
| living | 3/21 | 0.143 |
| outdoor, storage, retail, service | 0/33 | 0.000 |

Tracks §9's per-class recall closely, as it should — it is the same
predictions, asked a different question of. The four classes still stuck at
0.000 recall there are stuck at 0.000 here too: a query never finds a room
the labeller never predicts.

**Downstream:** for every resolved goal, `navigation.plan()` from the
building's largest circulation room to the resolved room, per embodiment
profile:

| profile | reachable |
|---|---|
| wheeled | 52/308 = 0.169 |
| legged | 142/308 = 0.461 |
| human | 142/308 = 0.461 |

(A different denominator from the whole-building reachability figures
elsewhere in this doc — this is reachability *to the specific resolved
goal room*, not the fraction of all rooms reachable from some reference
point. Not directly comparable to those numbers.)

**Threat to validity, stated plainly:** any candidate in the returned set
counts as resolved. A building with several offices returns several
candidates (82% of hits return 5+, the histogram cap) and any one of them
satisfies the query — this measures "can the pipeline find *a* room of the
right kind", not "can it find *the specific* room a robot was told to go
to" ("the kitchen on the 2nd floor"). Disambiguation among same-class
instances is unaddressed and should be named as future work, not implied.

## 12. Taxonomy repair — the five 0.000-recall classes, investigated

§9/§11 both showed five classes (kitchen, outdoor, storage, retail, service)
stuck at exactly 0.000 recall. Rather than guess at a fix, pulled the actual
held-out feature records for all 58 rooms in these five classes and read
them directly (`RuleBackend.predict_with_evidence` output, contained-element
names, furniture hints) before changing anything.

**Found a real bug, not just weak evidence.** `FURNITURE_HINTS` had a
`"dining"` key whose keywords, if they matched hardest, made
`predict_with_evidence` return the literal string `"dining"` —  not a
taxonomy class (`canonical()` folds "dining" into `"living"`). That
prediction can never equal any truth value; it was a guaranteed miss by
construction, independent of evidence quality. Fixed by merging its keywords
into `"living"`.

**Found kitchen's actual cause.** Real kitchen furniture in this corpus is
frequently named in German (`Küchenzeile-02 Kombination`, `Küchenzeile-05
...` — 16 occurrences across true-kitchen rooms, checked against every other
class first: 1 stray hit in `living`, 0 elsewhere) which the English-only
`FURNITURE_HINTS["kitchen"]` list never matched, so kitchens fell through to
generic area-based rules and landed on `living` or `office`. Added
`"küchenzeile"` and bare `"kitchen"` as keywords, verified against the full
corpus first for false-positive risk.

**Measured effect** (rule backend, same n=200 sample as the documented
table, before -> after):

| | before | after |
|---|---|---|
| kitchen recall | 0.000 (0/25 in the n=593 pool) | 0.360 (9/25) |
| overall accuracy (n=200) | 0.504 | 0.545 |
| overall accuracy (n=593) | — | 0.519 |
| goal resolution overall (n=593, §11) | 0.504 | 0.519 |

**A hypothesis that was tested and rejected, reported as such rather than
silently dropped:** `outdoor` rooms in the samples inspected had `Railing`
elements, suggesting `"Railing" in contains -> outdoor` as a cheap geometric
rule. Checked against the full held-out set before writing it:
`Railing` appears in 19 `stair` rooms and 17 `circulation` rooms against only
7 `outdoor` ones — stairwells and balustraded corridors dominate the signal.
The rule would have done more harm than good and was not added.

**Not fixed, and not papered over:** `outdoor`, `storage`, `retail`,
`service` remain at or near 0.000. Inspected content directly:
- `storage`'s true examples in this corpus are furnished with cabinets whose
  family name contains *both* `"wardrobe"` (a bedroom keyword) and
  `"storage"` substrings in the same string, in the same building, at equal
  counts — an actual tie in the evidence, not a missing keyword. Breaking it
  correctly needs a specificity-aware tiebreak in `furniture_hints()`, not a
  new keyword; left for future work rather than forced with an
  order-of-insertion hack.
- `retail`, `service`, `outdoor` (net of the railing finding) show no
  consistent area, degree, or content signal in the rooms actually held out
  — office desks, dining furniture, generic cabinets, no distinguishing
  vocabulary. With ≤10 held-out examples each, this may be too little
  evidence, or the taxonomy may be asking these rooms to declare something
  their contents don't actually encode. Neither has been established either
  way, and shouldn't be claimed.

## 13. External baseline — IFC2SB (built, run, not scorable)

[IFC2SB](https://github.com/RWTH-E3D/IFC2SB) (RWTH-E3D, LGPL v3, published)
generates `IfcSpace` and `IfcRelSpaceBoundary1stLevel`/`2ndLevel` entities
from raw IFC geometry via a C++/OpenCASCADE pipeline — the tool the schema
route (`m_schema`, §-none) is written to consume, if a file ever had one.
Unlike Ifc2Graph, it could not be scored end to end. The finding is the
inability itself, evidenced below rather than asserted.

**Build.** Required three fixes to the vendor's own files, none of them
algorithmic: (1) `FROM ubuntu:20.04 # comment` is rejected by current
BuildKit's Dockerfile parser — trailing comment removed; (2) `ADD ifc2sb
/tmp/ifc2sb/code` followed by `RUN cd code && mkdir build` fails because no
`WORKDIR` change follows the `ADD` — `WORKDIR /tmp/ifc2sb` inserted; (3)
`src/Clip.cpp` requires `CGAL/Exact_predicates_exact_constructions_kernel.h`,
a dependency the README's prerequisites list never mentions —
`libcgal-dev` added. Build parallelism capped at `-j4` (down from the
Dockerfile's own `-j16`), being a shared host. After these three fixes the image
builds clean and `./IFC2SB -h` runs. The patched Dockerfile and the exact diff
are preserved at `third_party/IFC2SB/Dockerfile.patched` with
`third_party/IFC2SB/PATCH_NOTES.md`.

**Run, synthetic corpus (where we have verified ground truth).** 2/2 tested
buildings (`synth_0000`, `synth_0005`) crash identically:
`terminate called after throwing an instance of
'IfcParse::IfcInvalidTokenException' what(): Token $ at 1081 invalid
instance name`, no output file written. Same exception, same token
position, on two different buildings — a systematic incompatibility with
our generator's IFC output, not per-building geometry noise. Not debugged
further inside IFC2SB's own C++ source: no debug symbols, no test suite,
and the earlier build fixes already cost more time than a fourth baseline
data point is worth relative to a genuine C++ crash investigation in
unfamiliar OpenCASCADE code. **Zero of our 300 verified synthetic buildings
can be scored.**

**Run, real corpus (where nothing has verified ground truth, ours
included).** Behaviour depends on how much `IfcSpace` the input already
states — consistent with IFC2SB being a *space-boundary refinement* tool
(closer to Lilis/Fichter's second-level-boundary literature, §-space) than
a room-recovery one:
- `model_9` (2 stated rooms, of 7 total): completes without crashing, but
  every one of its 3,035 boundary relations is against a single generated
  `IfcExternalSpatialElement` ("facade") — zero interior `IfcSpace`, zero
  usable room-to-room signal.
- `model_12` (53 stated rooms, of 68 total): produces real interior data —
  51 `IfcSpace`, 3,058 boundaries against real spaces, 81 candidate
  room-room connections (elements shared by exactly two spaces) — but the
  process itself then segfaults (`SIGSEGV`, exit 139) in some step after
  the core output. Confirmed the output file is nonetheless complete and
  valid up to that point (proper STEP `ENDSEC;`/`END-ISO-10303-21;`
  termination, parses cleanly in `ifcopenshell`), so the crash cost nothing
  usable here — but it is the same class of instability as the synthetic
  failure, on the same binary.

**Verdict.** IFC2SB is not included in the significance table. Not because
it loses — because there is nothing to score it against: it cannot run on
the corpus with verified ground truth, and the corpus without a crash
problem has no verified ground truth for *any* method to be scored
against, our own pipeline included. Reported as a diagnosed negative
result rather than a silent omission, in the same spirit as the
`IfcBuildingElementProxy` and `Railing→outdoor` findings elsewhere in this
document.

## 14. Ramblr BIM-GNI scene graphs — not usable as connectivity ground truth

A partner company (Ramblr) delivered IFC-to-scene-graph output over 208 real
models (`dataset/2025_BIMfundamentals/`) plus a written accuracy report,
proposed as pseudo ground truth to skip annotation. Independently
re-verified from raw files (not the vendor's own checking code) via
`main/verify_ramblr_report.py`: every headline number in the report — 3,510
relations checked, precision 0.884, recall 0.137 against 22,671 real
wall-opening pairs, 179,020/179,020 valid node references, the full
per-class element-coverage table — reproduces **exactly**. Two counting bugs
found and fixed along the way (nodes and relations both duplicated across
multi-room membership, 6x then 2x over-count) are documented in
`dataset/BIM-GNI-Ramblr/VERIFICATION.md`, including the debugging trail.

**Verdict: not usable, for two reasons, not one.** (1) The relation asserted
— wall contains door/window — is not room-to-room connectivity, the thing
`eval_connectivity.py` scores. (2) Even for what it does assert, recall is
0.137 and 92/208 files have zero relations; scoring against it as a closed
reference would penalize any method, ours included, for finding correct
connections it doesn't show — the same "necessary conditions falsify but
don't confirm" trap as the R1a metric-bias finding (§ above).

**More useful than the verdict itself:** verifying it required extracting
real wall-opening ground truth directly from IFC
(`IfcRelVoidsElement`+`IfcRelFillsElement`) — complete, no vendor tool
needed, 22,671 real pairs across all 208 files. `dataset/test_set/` (our
existing 25-model real corpus) is confirmed an exact byte-for-byte subset of
these 208 models. Real (not pseudo) room-connectivity ground truth may be
buildable for this corpus by combining that native relation with room
geometry — flagged as the actionable follow-up, not yet started.

## 8. Open questions

- How much architectural variety must the generator produce for connectivity
  recovery to be non-trivial? Dead-end rooms, ambiguous adjacencies and
  open-plan zones are what make the problem hard; a generator of simple
  rectangular grids would flatter every method equally.
- ~~Does the LLM labeller need the real corpus for evaluation~~ — resolved:
  evaluated by holding out the 681 IFC-*stated* real labels (leave-one-out
  masking), not synthetic ones, since synthetic rooms carry no independent
  ground truth for function. This is an optimistic bound (stated rooms are
  geometrically cleaner than recovered ones) but is a real-corpus number.
- Whether to report vertical connectivity separately, given one real model has
  no vertical circulation of any kind.
- Four taxonomy classes (outdoor, storage, retail, service) score 0.000
  recall under both rule and LLM backends with ≤10 held-out examples each —
  insufficient evidence, or a genuinely under-determined taxonomy? Deliberately
  deferred to isolate the context/prompting result; unresolved.
