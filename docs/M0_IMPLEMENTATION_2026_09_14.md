# M0 implemented — the trustworthy baseline

**Status:** done, 14 September 2026. This is milestone M0 of the
[system design](ROBOBIM_SYSTEM_DESIGN.md) §13 and phase 0 of the
[redesign](PIPELINE_REDESIGN_2026_09_13.md): repair the evaluation and
serialization contracts, and turn the [audit](CODEBASE_AUDIT_2026_09_13.md)
reproductions into regression cases. The geometry findings are **not** repaired;
they are M1 and are asserted as still-open below.

M0's acceptance criteria were: *every audit reproduction is an executable
regression case; human annotations round-trip into the evaluator; no missing run
silently changes denominators.* All three now hold.

```bash
python tests/test_m0_regressions.py          # 20 repairs, 4 open findings
python main/eval_connectivity.py --gt ifc-graphx/annotated_gt --graphs results
python main/evaluate.py --annotations ifc-graphx/annotations
```

## The headline: the annotation effort can now be measured against

387 positive and **53 negative** link judgements and **30 rejected rooms** were
being discarded during composition. Scoring the pipeline against the 10
complete-coverage buildings is possible for the first time:

> **Superseded as current results, 18 September 2026.** The table below was
> correct on 14 September and is retained as the record of what M0 measured.
> `model_2` has since been rebuilt from its intact source IFC (the corpus copy
> was truncated, missing 8 of 19 doors), which moves these figures to
> 0.876 / 0.966 / 0.919, strict 0.882, and edge-type to 366/374. See
> [the 18 September audit](AUDIT_2026_09_18.md) and the README's Results
> section for the live numbers; quote those, not these.

| | value |
|---|---|
| Precision on pairs a reviewer ruled on | **0.878** |
| Recall | **0.982** |
| F1 | **0.927** |
| Strict F1 (charging out-of-scope and unmatched-endpoint claims) | 0.903 |
| Edge-type accuracy on matched pairs | 380/380 |

Three things must be quoted with it:

1. **A trivial baseline scores higher on precision.** Joining every pair the
   reviewer examined would score 0.918 against 0.878. The reviewed negatives are
   53 against 387 positives, so the base rate is high and precision on this
   slice is not discriminative. This is the audit's fourth interpretation point,
   now measured rather than argued: report base rates and a balanced diagnostic
   slice, and do not adjust sampling to win.
2. **83% of judged items were swept in with "rest of this floor is correct."**
   `evaluate.py` prints this fraction. It is ground truth, but it was not looked
   at item by item.
3. **22 predicted transitions rest on rooms the reference does not contain** —
   30 predicted rooms, every one of them a region a reviewer explicitly
   rejected. Under the old scorer these left the accounting entirely, so an
   invented room and the edges hung on it cost a method nothing.

## What was repaired

| ID | Finding | Repair |
|---|---|---|
| A02 | Two doors between one pair of rooms collapsed to one; route existence depended on IFC product order | Every lateral edge carries a portal id and is keyed on it. Candidates are sorted before they are given ids, so identity is a property of the building rather than of the parse order. `to_json` writes the key and `load` restores it; a keyless legacy file still loads every edge it holds. `baselines._add` was keyed the same way and is fixed with it. |
| A06 | `unsure` rooms entered ground truth | Held. Was repaired on 14 September; now has a regression case. |
| A07 | The scorer read a retired verdict vocabulary, so 1,034 human verdicts produced zero judged items | One vocabulary in `evaluation/verdicts.py`, asserted equal across `compose.py`, the evaluator and `compose.js`. A retired word now raises instead of scoring silently. `evaluate.py` also reads annotation bundles, scores floor links separately, and reports review coverage and the bulk fraction. |
| A08 | Human GT was positives-only and raised `KeyError` in the evaluator it was written for | `compose` keeps rejections as `negatives` and states its `review_scope`; `connectivity_gt` emits `adjacent_not_connected` and `rooms_rejected`. `evaluation/schema.py` reads the generator, derived and annotated dialects through one record, including how rooms are identified in each. |
| A09 | The derived reference scaled tessellated geometry by the declared unit a second time, and filtered doorway voids with the room-area threshold | `parser` now exposes the empirical correction apart from the declared unit; `doorway_voids` takes the correction only. Minimum areas are per class. Measured effect below. |
| A10 | Edges touching unmatched rooms were dropped, and many predictions could stand for one reference room | `evaluation/matching.py` assigns one to one, keeps unmatched predictions and reports merges and splits. `scoring.py` counts every prediction: on a reference negative, on an unknown pair, out of scope, or hung on an unmatched room. Unknown never enters precision. |
| A11 | Masking a room's label left the vertical edge that label had produced, so `n_vertical` carried the answer to the classifier | `vertical_connections` records `basis`: `stair_solid` or `stacked_label`. `eval_semantics.mask_room` removes the label *and* the links that rest on it, and restores both. Geometry-backed links survive masking, as they should. |
| A12 | VLM crops were selected by room number, which repeats in `model_6` and `model_17` | `PlanCache.crop` takes a GlobalId and the storey the graph puts the room on, and returns nothing rather than a plausible wrong crop. An ambiguous room number is refused. |
| — | The schema baseline read any shared boundary as a door, so a party wall became a doorway | Only door, opening and virtual boundaries are read; a virtual boundary becomes an open passage, which is what it is. |
| — | `run_pipeline.sh` suppressed stage failures, deleted its own inputs and printed DONE regardless | `set -euo pipefail`, per-stage logs under `results/run_logs/`, a non-zero exit listing what failed, and the synthetic corpus is only deleted on `--regenerate`. The score summary reads each run's manifest and flags an average taken over a corpus that lost buildings. |
| — | Scorers globbed for whatever existed, so a crashed building shrank the denominator | `evaluation/manifest.py`. Expected units are declared before scoring; `missing` and `failed` stay in the denominator and only an `excluded` unit with a stated reason leaves it. `eval_connectivity` exits non-zero unless `--allow-missing`. |
| — | Inter-annotator agreement was documented as a ceiling on model accuracy | Corrected in `build_gt.py` and `evaluate.py`: it characterises label uncertainty. A method can be right where a reviewer was wrong. |
| — | `ifc-graphx/annotator/` is a second copy of the tool and had drifted | Re-synced, and a regression case fails if any of the 25 shared source files diverge again. |

### What A09 was actually costing

Measured over the 25 real models:

| | doorway voids recovered |
|---|---|
| Room-area threshold on voids (old) | 445 |
| Per-class threshold (new) | **1,363** |

Seven of the 25 models declare millimetres. On every one of them the old call
recovered **zero** voids — geometry already in metres was multiplied by 0.001,
so every opening fell below any threshold. The flood-fill then found every room
sealed and emitted a list of confident negatives that were artifacts of the
declared unit. `model_9` and `model_10` went from 0 voids to 8 and 24.

The reference has since been rebuilt over the 25-model corpus. Against the
archived one: usable models 15 → **17**, connected pairs 370 → **460**,
separated pairs 379 → **289**. **346 of roughly 749 previously decided pairs
changed verdict** (218 separated → connected, 128 the other way). Both halves of
the repair act, in opposite directions and both correctly: recovered voids open
falsely sealed pairs, and the per-class blocker threshold means a thin wall stub
now blocks instead of being discarded with the room-sized minimum. Verified by
re-deriving with each half in isolation.

Scored against the rebuilt reference, the pipeline reaches precision 0.728,
recall **0.932**, F1 0.817 over 17 models — recall was 0.661 against the old
one. The reference had been marking as separated a large number of pairs the
pipeline correctly called connected. The two scores are not comparable and the
old one should not be quoted as a prior measurement of the same thing.

### What A11 was actually costing

The rule baseline on all 593 eligible real rooms, leave-one-out:

| | accuracy |
|---|---|
| Audit's fresh measurement, 13 September (label blanked only) | 308/593 = 0.519 |
| With the label's own topology masked too | **301/593 = 0.508** |

Seven rooms were being predicted correctly from a feature that existed only
because of the label being held out. The effect is small, and that is worth
saying plainly: it does not overturn any conclusion. What it does establish is
that the masking was not doing what the method section said it did, and the
same dependency exists for every future feature derived from a name.

### The stored graphs were rebuilt

Not one of the 25 saved real graphs contained a single parallel
`(source, target, relation)` entry, because the builder collapsed them before
the file was written. Rebuilding with portal identity recovered **92
transitions** across the corpus and changed **no room identity**, so the human
annotations still join. The previous files are kept under
`results/_pre_m0_snapshot/`; `validate.py` reports the same 1,025 rooms
(681 stated, 344 recovered) and the same means, confirming the rebuild changed
edge identity and nothing else.

Every result file under `results/` older than this was produced by a repaired
scorer and should be regenerated before it is quoted. `run_pipeline.sh` marks
such files `stale` rather than reading them.

## Architecture recheck

The [system design](ROBOBIM_SYSTEM_DESIGN.md) §5 lists ten enforced invariants.
M0 owns some of them and not others, and it is worth being exact about which,
because "the contracts are repaired" could otherwise be read as more than it is.

| # | Invariant | State after M0 |
|---|---|---|
| 1 | Every relation endpoint exists; endpoint types match the schema | **Endpoints: held** and guarded. Zero dangling endpoints across the 25 real graphs. Type checking against a relation schema does not exist — there is no schema to check against yet. |
| 2 | Canonical ownership acyclic, one canonical owner | **Held** and guarded. Zero cycles, zero multi-parent nodes, zero self-loops across the corpus. |
| 3 | Every geometry reference declares frame and units | **Not started.** No frame registry. The parser now keeps the declared unit and the empirical repair apart, which is a precondition, not the invariant. |
| 4 | A resolved portal has two sides and validated incidence | **Not started.** Portals have identity but no sides; incidence is still the unchecked probe of finding A03. |
| 5 | Adjacency is not connectivity | **Held.** `adjacent_to` is a distinct relation and is excluded from every traversability set; the regression suite asserts it. |
| 6 | Exterior-facing portals and supported outdoor cells | **Not started.** |
| 7 | Every navigation edge carries direction, dependencies, feasibility and validation status | **Not started** — this is finding A05, and belongs to M2. |
| 8 | Save/load preserves entity IDs, edge IDs, claims, geometry, unknowns | **Partly held.** Entity and edge IDs round-trip and are guarded. Claims, geometry references and unknowns do not exist yet, so nothing is lost — but nothing is preserved either. |
| 9 | No edge involving an unconfirmed endpoint becomes confirmed | **Held, in the annotation contract.** A link whose endpoint room was not confirmed goes to `held_out` with a stated reason, never into the positives, and a rejection on an unconfirmed room is not counted as a negative either. |
| 10 | Aggregate views retain references to the detail they summarise | **Not started.** |

Three held, one partly, six not started. That is the expected shape: M0 was the
evaluation and serialization contract, and six of the ten are geometry and
runtime invariants that M1 and M2 introduce.

### Single source of truth

Checked by grep, not by assumption. Three scripts had their own copy of the F1
expression; they now call `evaluation.scoring.f1`. One deliberate exception
remains and is documented in place: `main/eval_real_connectivity.py` keeps its
own scoring because its room contraction answers a different question — "can you
reach B from A without passing through another *stated* room" — and folding it
into the shared scorer would hide that. `derive_real_gt.calibrate` also computes
its own figures, which is correct: it is a producer checking itself against
generator truth, not a scorer ranking methods.

### Gaps this recheck found

- **There is no split manifest.** The redesign requires splitting by building
  and by source design family before any tuning, and treating buildings already
  inspected for debugging as development material. Nothing in the code enforces
  that, so a threshold tuned on a building can still be evaluated on it. The
  mechanism belongs beside `evaluation/manifest.py`; which buildings go where is
  a research decision that has not been taken.
- **There is no schema validator.** Invariant 1's type half, and invariants 2
  and 8 in general, are checked by the regression suite on a fixture and by an
  ad-hoc sweep over the corpus, not by a validator the pipeline runs on every
  output. Stage G of the design calls for one.
- **`results/real_gt/` had to be rebuilt**, and anything that consumed it before
  14 September 2026 consumed a reference that was blind to most openings.

## What is still open

The geometry findings are unrepaired and the regression suite asserts they are
still present, so that a fix arrives with a demand to flip the case rather than
leaving a stale expectation:

| ID | Still true | Belongs to |
|---|---|---|
| A01 | Room recovery takes the XY projection, not a section, so a lintel seals a doorway | M1 |
| A03 | Door probing samples at fixed heights without checking the intervening segment, and reaches past a 0.5 m room through a wall | M1 |
| A04 | Reported width is the longest world-axis bounding-box side: a 1.0 m door rotated 45° reports 0.742 m | M1 |
| A05 | Feasibility is two-valued; a missing width reads as passable, and a legged route exists across stacked "Stair" labels with no stair solid | M2 |

`docs/audit_2026_09_13/probes.py` remains the 13 September snapshot and is not
maintained against the repaired code — several of its probes deliberately no
longer reproduce. `tests/test_m0_regressions.py` is the live version.

## Next

M1, per the design: sections and support geometry in place of footprint
projection, spatial cells, portal side incidence with a local crossing witness,
and dimensional claims with explicit unknowns. The three development buildings
still need selecting on observed geometry and review quality — a
complete-coverage flag is not evidence of suitability.
