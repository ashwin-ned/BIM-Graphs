# Synthetic connectivity benchmark — 18 September 2026

The first evaluation in this project from which **specificity is computable**, and
the first in which the method's advantage over its strongest baseline is
statistically significant.

Corpus: `dataset/synthetic_data/synth150{,_L2,_L3,_L4}` — 150 procedurally
generated buildings per damage level, 2049 rooms, **1708 positive pairs and 1211
explicit adjacent-but-not-connected negatives**. The negatives are the point:
`main/generate_building.py:8-12` sets `door_ratio` precisely so that some
adjacent rooms are not joined, "if every neighbouring pair has a door, naive
adjacency scores a perfect result and the benchmark cannot distinguish any two
methods."

Reproduce:

```bash
python main/baselines.py --src dataset/synthetic_data/synth150 \
    --out results/baselines/synth150                      # ~55 s per level
python main/eval_connectivity.py --gt dataset/synthetic_data/synth150 \
    --graphs results/baselines/synth150/ours \
    --out results/bl_synth150_ours.json
python main/significance.py --tag synth150
```

## Pooled results

Precision / recall / F1 / **specificity**, pooled over all 150 buildings:

| method | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| `schema` | 0.000 / 0.000 / 0.000 / **1.000** | same | same | same |
| `adjacency` | 0.563 / 0.912 / 0.696 / **0.000** | 0.546 / 0.757 / 0.635 / 0.113 | same as L2 | same as L2 |
| `door_near` | 1.000 / 0.845 / 0.916 / **1.000** | 0.985 / 0.761 / 0.858 / 0.983 | same as L2 | same as L2 |
| `ifc2graph` | 0.585 / 1.000 / 0.738 / **0.000** | 0.562 / 0.436 / 0.491 / 0.520 | 0.000 / 0.000 / 0.000 / 1.000 | same as L3 |
| **`ours`** | **0.995 / 1.000 / 0.998 / 0.993** | 0.983 / 0.827 / 0.899 / 0.980 | same as L2 | 0.985 / 0.827 / 0.899 / 0.982 |

## Significance — paired Wilcoxon, 20,000-resample bootstrap CI, n = 150

| level | `ours` mean F1 | strongest baseline | Δ | 95% CI | p | W/L/T |
|---|---|---|---|---|---|---|
| L1 | 0.9980 | `door_near` 0.9131 | +0.0849 | [+0.0743, +0.0962] | <1e-6 | 131 / 0 / 19 |
| L2 | 0.8907 | `door_near` 0.8502 | +0.0405 | [+0.0319, +0.0497] | <1e-6 | 81 / 2 / 67 |
| L3 | 0.8907 | `door_near` 0.8502 | +0.0405 | [+0.0319, +0.0497] | <1e-6 | 81 / 2 / 67 |
| L4 | 0.8912 | `door_near` 0.8502 | +0.0410 | [+0.0324, +0.0502] | <1e-6 | 81 / 2 / 67 |

`main/significance.py` could not run at all until today: `:30` called
`sys.path.insert` with no `import sys`, raising `NameError` on import, and no
test covered it. Every p-value the ICRA draft quoted predates that fix.

## What this changes

**The real-corpus comparison was not discriminative, and that was the problem —
not the method.** On `ifc-graphx/annotated_gt/` the same contrast gives
`door_near` precision 0.994 and specificity 0.962 against `ours` at 0.876 and
**0.000**, with `ours` vs `door_near` at p = 0.805, 4 wins / 4 losses / 2 ties.
That reference records a negative only where the pipeline proposed a link and a
reviewer rejected it (`negatives_cover: "links the pipeline proposed and a
reviewer rejected"`), so all 53 of its negatives are by construction the
pipeline's own false positives and a true negative is unrecordable. Given a
corpus that *can* discriminate, the ordering reverses and the margin is
significant at every damage level.

## Three things that must be quoted alongside the table

**`schema` has perfect specificity and predicts nothing.** A method that asserts
no edges cannot produce a false positive, so its specificity is 1.000 by
definition. Specificity must never be reported without recall beside it. The
same degeneracy is why `schema` scores 0.000 F1 throughout: it reads
`IfcRelSpaceBoundary`, and generated files never carry one — L1 *is* the
"no space boundaries" level that real Revit exports already arrive at
(`main/damage_ifc.py:11-13`).

**The damage axis has two regimes, not four.** L2, L3 and L4 give identical
pooled metrics for `adjacency`, `door_near` and `ours`. The files genuinely
differ — on `synth_0000`, `IfcRelAggregates` drops 5 → 4 → 3 and
`IfcRelContained` 2 → 0 — but the graphs the pipeline builds from L2 and L3 are
byte-identical for all three methods, and only `ours` and `adjacency` change at
all at L4. Orphaning spaces from their storey (L3) is absorbed completely,
because storey assignment is recovered geometrically rather than read from
containment; the undeclared centimetre units at L4 are absorbed by
`parser.detect_scale_correction`. This is a result about robustness, not a
broken axis, but the paper must not claim a four-level severity gradient for
connectivity. What actually moves the number is **missing `IfcSpace`** — the
L1 → L2 step, where half the storeys lose their rooms and recovery has to supply
them.

**`ifc2graph` collapses by construction, not on the merits.** It has no
room-recovery stage (`main/baselines.py:25-28`), so once L2 removes `IfcSpace`
from half the storeys those rooms are invisible to it, and at L3/L4 without
spatial containment it emits nothing at all. Reporting "we beat a published
external tool" on L3/L4 would be hollow. Its L1 number (F1 0.738, specificity
0.000) is the fair one to quote.

## Two buildings where `ours` loses to `door_near`

At L2 and above: `synth_0070` (−0.133) and `synth_0028` (−0.056). Worth reading
before the method work in P4 — they are the cases the oriented door probe gets
wrong and the naive nearest-two-rooms rule gets right, which is the same shape
as open finding A03 ("door probing samples at fixed heights without checking the
intervening segment, and reaches past a 0.5 m room through a wall").
