# Annotation-free connectivity reference for the real corpus

> **Superseded in part, 14 September 2026.** Two things changed after this was
> written. Manual annotation is **no longer blocked** — 15 buildings have been
> reviewed and 10 have complete verdict coverage, so the corpus now has a human
> reference and this derived one is a cross-check rather than the only option
> (see the [M0 record](M0_IMPLEMENTATION_2026_09_14.md)). And the deriver had a
> unit defect: it scaled tessellated geometry by the declared unit a second
> time, recovering **zero** doorway voids on the seven millimetre-declaring
> models, so every number below was computed on a reference that was blind to
> most openings. The deriver is fixed and the reference rebuilt; the measured
> figures in this document have **not** been recomputed and should be read as a
> record of the method's design, not as current results. The reasoning about
> what the reference can and cannot support still holds.
>
> How much the repair moved it, measured against the archived reference on the
> same 25 models: usable models 15 → 17, connected pairs 370 → 460, separated
> pairs 379 → 289, and **346 of roughly 749 previously decided pairs changed
> verdict** (218 separated → connected, 128 the other way). Both halves of the
> repair act and both are corrections — recovered voids open falsely sealed
> pairs, and giving blocking geometry its own minimum area means a thin wall
> stub blocks rather than being discarded with the room-sized threshold. Scored
> against the rebuilt reference the pipeline reaches precision 0.728, recall
> 0.932, F1 0.817 over 17 models, where the old reference gave recall 0.661: it
> had been penalising the pipeline for connections it had called correctly.

Status as of 2026-09-02. Manual annotation is blocked, so this is an attempt to
obtain real-corpus connectivity ground truth from geometry alone. The machinery
works and is calibrated. **The resulting reference is not yet good enough to
rank methods, and this document says exactly why.** Nothing here should be cited
as a real-corpus connectivity result.

Code: [`main/derive_real_gt.py`](../main/derive_real_gt.py),
[`main/eval_real_connectivity.py`](../main/eval_real_connectivity.py).

## The idea

The corpus states **no** `IfcRelSpaceBoundary` (0 across 208 models), so
room-to-room connectivity cannot be read from the schema. But the *rooms* are
stated -- 188/208 files carry `IfcSpace` with real footprints. Only the edges
are missing, and an edge is a geometric fact: two rooms are joined iff free
space at door height runs continuously from one into the other.

So we do not need an annotator to draw rooms. We need only to decide, for each
adjacent pair of already-stated rooms, whether a passage exists.

The deriver rasterises wall solids and flood-fills free space, restricted to
`A | B | (A.buffer(t) & B.buffer(t))` -- the two rooms plus only the strip
between them, which stops the fill wrapping around a wall end. It never reads a
door's placement normal, which is what `topology.py` does, so the two are
different mechanisms rather than the same idea twice.

`t` is not a tuned constant. Two `IfcSpace` footprints stop at opposite inner
wall faces, so the distance between them *is* the wall thickness there, and each
pair's buffer is read off its own geometry. A fixed constant cannot work: on the
synthetic corpus anything below 0.20 m fails to bridge a 0.20 m wall (69 false
negatives) and anything above 0.25 m wraps around wall ends (4 false positives).

## Calibration — the instrument is sound

Run against the synthetic corpus, where ground truth is known by construction:

```
python main/derive_real_gt.py --dir dataset/synthetic_data/synthetic --calibrate
```

| | pooled |
|---|---|
| precision | **1.000** |
| recall | **1.000** |
| F1 | **1.000** |
| tp / fp / fn / tn | 69 / 0 / 0 / 59 |
| abstentions | 0 |

Perfect on all 8 buildings. Whatever is wrong downstream is not the flood-fill.

## A bug this exposed, worth remembering

A wall footprint is the **XY projection of the whole solid**. A doorway stops
under a lintel at about 2.1 m, so in plan the projection seals it. Every doorway
reads as solid wall and buildings come out as sets of disconnected rooms --
`model_10` produced 19 rooms and 0 edges.

The fix recovers the passage from `IfcOpeningElement`, the void the modeller
authored, classified through the native `IfcRelFillsElement` chain (an opening
filled by an `IfcDoor` is a doorway; one filled by an `IfcWindow` is not). That
chain is richly present in the corpus even though `IfcRelSpaceBoundary` is not.

This is the second time the plan-projection assumption has produced a wrong
answer, and it is the root of the limitation below.

## Applied to the real corpus (25 models)

```
python main/derive_real_gt.py --dir dataset/test_set --out results/real_gt25
```

15 of 25 models pass the trust gate, giving **593 rooms, 331 connected pairs,
367 separated pairs**. Excluded: 4 with no candidate-adjacent pairs, 3 with no
separated pair at all, 2 with fewer than two usable `IfcSpace`, 1 with doors but
no traversable opening.

The gate exists because some files place `IfcDoor` products against unbroken
wall solids and cut no void. There the geometry simply does not contain the
answer, and a flood-fill would emit a long list of confident negatives that are
artifacts of modelling style.

## CORRECTION, 18 September 2026 — this section is superseded

**The measurements below describe the reference as it stood before the
14 September rebuild recorded in [the M0 record](M0_IMPLEMENTATION_2026_09_14.md).
Re-measured against the reference now on disk, the argument does not hold:**

| | stated below | measured 18 Sep 2026 |
|---|---|---|
| door-backed pairs the reference agrees with | 300 | **362** |
| door-backed pairs called separated | **428** | **17** |

The void table below is stale in the same way. It reports `model_6` with 93 doors
and 0 voids and `model_16` with 38 and 0; both now carry 93/93 and 38/51, and no
model in the corpus has doors without authored voids.

Scoring `ours` against this reference through `evaluation/` now gives, over the
17 gated models: precision 0.788, recall 0.904, F1 0.842, and a
**specificity of 0.644** over 289 genuine negatives — a discriminative
real-corpus number, where the *annotated* reference gives specificity 0.0000
because every one of its negatives is by construction one of the pipeline's own
false positives.

Two caveats still apply: 17 of 25 models pass the trust gate, so this is not the
whole corpus; and `precision_strict` is 0.406 with 378 predictions on unmatched
endpoints, so the pipeline asserts a great deal outside this reference's room
set. See [the 18 September audit](AUDIT_2026_09_18.md) §B1.

The reasoning below is retained as the record of what was measured at the time,
and its diagnosis was correct: the cause was the projection sealing doorways
without authored voids, which is open finding A01 living inside the ground-truth
producer. That is why the rebuild helped, and why repairing A01 would remove the
dependence on authored voids entirely.

## Why these numbers must not be used (SUPERSEDED — see the correction above)

**1. The negatives are unreliable.** Across the 15 gated models there are
**428 pairs where the pipeline found an actual `IfcDoor` joining two rooms and
the reference calls them separated** -- more than the 300 door-backed pairs it
agrees with. A physically placed door is strong evidence of a connection.

The cause is the same projection problem, only partly fixed. We cut voids that
were authored, but only 2 of the 15 gated models have voids for all their doors:

| | doors | voids |
|---|---|---|
| model_0 | 25 | 25 |
| model_2 | 11 | 11 |
| model_6 | 93 | **0** |
| model_16 | 38 | **0** |
| model_8 | 46 | 1 |

Three models passed the gate with **zero** voids -- they cleared it only because
some walls happen to have genuine plan gaps. Where a door has no authored void,
the projected wall seals it, the reference says "separated", and a correct
prediction is scored as a false positive.

So a derived **"connected" is strong evidence** (the flood found a real path);
a derived **"separated" is weak evidence** (it may be a projection artifact).
Recall against this reference is meaningful. Precision is not.

**2. The base rate makes F1 useless here.** On the two models where the
reference *is* trustworthy, 59 of 67 candidate pairs are connected -- an 88%
positive rate. Predicting every adjacent pair therefore scores precision 0.881
and recall 1.000. The `ADJ_TOL = 0.6 m` candidate set admits almost only pairs
that share a wall closely, and those are nearly all connected.

Measured, on the fully-voided subset:

| method | P | R | F1 |
|---|---|---|---|
| `adjacency` (predict every adjacent pair) | 0.881 | 1.000 | **0.937** |
| `ours` | 0.848 | 0.661 | 0.743 |

The trivial baseline wins. The original conclusion drawn here — that this is a
statement about the benchmark rather than about the methods — is too strong, and
the [audit](CODEBASE_AUDIT_2026_09_13.md) corrected it. On a trustworthy and
representative distribution, "predict everything" winning is evidence the method
needs improvement, not evidence the benchmark is broken. A high base rate does
make F1 uninformative, and the honest response is to report base rate,
specificity and MCC alongside precision-recall, and to keep a separate balanced
diagnostic slice clearly marked as such — **not** to resample until the proposed
method wins. For contrast the synthetic benchmark runs at 57% positive (77
edges, 59 negatives), where `adjacency` scores 0.605 and `ours` 1.000.

The same pattern appears against human annotation, where the reference *is*
trustworthy: joining every reviewed pair scores precision 0.918 against our
0.878. That is not a broken benchmark. It is a high base rate on a slice where
reviewers only ever saw links the pipeline proposed.

**3. A room-set mismatch confounds the comparison.** The reference knows only
stated rooms; the pipeline additionally recovers rooms the file omits and routes
connectivity through them. On `model_0`, 14 of 16 apparently missing reference
edges are present as a path through a recovered room. The scorer therefore
contracts recovered rooms by default and reports the uncontracted number
alongside. **No model in the corpus has zero recovered rooms**, so this
confound is never absent. Contraction is also not free: a large recovered
circulation blob links everything touching it, which is why out-of-scope
predictions (416 for `ours`) outnumber the in-scope pairs.

## What would fix it

1. **Cut a true horizontal section instead of projecting.** Slice the wall mesh
   at door height rather than projecting the solid to XY. This removes the
   entire artifact class at its root and does not depend on any file authoring
   voids. `parser.parse(with_meshes=True)` already retains what is needed. This
   is the single highest-value fix and should come first.
2. **Rebalance the candidate set.** Widen `ADJ_TOL`, or sample negatives from
   pairs separated by exactly one wall regardless of distance, so the positive
   rate approaches the synthetic corpus's 57% and the metric can discriminate.
3. **Report balanced accuracy or MCC, not F1**, while any base-rate skew
   remains. On the current gated set `adjacency` reaches balanced accuracy
   0.508 and `ours` 0.480 -- both near chance, which F1 hides.
4. **Tighten the gate to require void coverage**, not merely a nonzero edge
   count, once (1) is done and the gate's purpose changes.

## Independence, honestly stated

This reference is a *second algorithm's opinion on the same geometry*, not an
annotation. It shares its inputs with the pipeline and differs only in method:
authored voids and a raster flood-fill, versus door placement normals and vector
probing. Agreement is real evidence but weaker than agreement with a human, and
it should never be described as ground truth in the paper.

There is also a trap to avoid. Adding void-awareness to `topology.py` is
attractive and would probably improve real-corpus recall -- but it would make
the pipeline and the reference share a mechanism, and the reference would stop
being independent evidence about it. If we adopt voids in the pipeline, this
reference must be retired for evaluating that component.

## Ramblr cannot substitute

Checked directly: all **3,510** relations across all 208 Ramblr scene graphs are
of type `contains`, and none is room-to-room connectivity. Ramblr offers no
independent connectivity signal at any precision. See
[`dataset/BIM-GNI-Ramblr/VERIFICATION.md`](../dataset/BIM-GNI-Ramblr/VERIFICATION.md).

## Reproduce

```bash
# calibrate the instrument against known-true synthetic ground truth
python main/derive_real_gt.py --dir dataset/synthetic_data/synthetic --calibrate

# derive the reference for the real corpus
python main/derive_real_gt.py --dir dataset/test_set --out results/real_gt25

# score (read the caveats above before quoting any number)
python main/eval_real_connectivity.py --gt results/real_gt25 \
    --graphs results/bl_real/ours --out results/real_connectivity_ours.json
```

Artifacts: `results/real_gt25/*.realgt.json`, `results/real_gt_calibration.json`,
`results/real_connectivity_{ours,adjacency}.json`.
