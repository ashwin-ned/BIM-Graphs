# Annotation import — 14 September 2026

Imported the supplied latest bundle, retained earlier buildings absent from it, and removed redundant files from the active annotation directory. No individual verdict, label, note, addition or pin was edited.

- Canonical bundle: `ifc-graphx/annotations/bimsg-annotations__AN.json` (15 buildings).
- New buildings: 10; retained older buildings: 5; overlaps with the new bundle: 0.
- Complete-coverage exports: 10; partial exports: 5, under `ifc-graphx/annotated_gt/incomplete/`.
- Refreshed GraphML, GEXF and HTML views for the complete-coverage exports in `ifc-graphx/graphs/`.
- Verified backup of previous annotations, GT, graph views and supplied input: `.annotation_backups/annotations-before-20260913T221454Z.tar.gz`.
- Corrected both Python/JavaScript composer copies to hold out unsure rooms and their incident connections; 75 randomized parity comparisons passed with independent uncertainty assertions.

| Model | Source | Verdict coverage | Unjudged rooms / links / vertical | Held out | Merge/split requests | Missing-room pins |
|---|---|---|---|---|---|---|
| model_0 | retained | partial | 1 / 12 / 0 | 1 | 0 | 3 |
| model_2 | retained | complete | 0 / 0 / 0 | 0 | 0 | 0 |
| model_3 | retained | partial | 1 / 11 / 8 | 0 | 0 | 0 |
| model_7 | new | complete | 0 / 0 / 0 | 0 | 0 | 0 |
| model_8 | new | complete | 0 / 0 / 0 | 0 | 0 | 1 |
| model_9 | new | complete | 0 / 0 / 0 | 0 | 0 | 2 |
| model_10 | new | partial | 2 / 0 / 0 | 4 | 1 | 13 |
| model_11 | new | complete | 0 / 0 / 0 | 1 | 0 | 14 |
| model_12 | new | complete | 0 / 0 / 0 | 0 | 0 | 6 |
| model_13 | new | partial | 10 / 6 / 4 | 8 | 0 | 2 |
| model_15 | new | complete | 0 / 0 / 0 | 1 | 0 | 5 |
| model_16 | new | complete | 0 / 0 / 0 | 2 | 0 | 4 |
| model_17 | new | complete | 0 / 0 / 0 | 0 | 0 | 13 |
| model_20 | retained | partial | 0 / 0 / 10 | 0 | 0 | 4 |
| model_23 | retained | complete | 0 / 0 / 0 | 1 | 0 | 0 |

“Complete” describes verdict coverage of the existing plan, not resolution of missing geometry or all benchmark questions. Partial annotations, missing-room pins and merge requests remain available without fabricating geometry or judgments.

The legacy `.gt.json` positive-pair format remained unchanged at import time, with negative and uncertain verdicts retained only in the canonical bundle. That scoring-contract repair was then carried out the same day: the exports now carry negatives, rejected rooms and review scope, and were regenerated from this same bundle without re-editing a single verdict. See [the M0 record](M0_IMPLEMENTATION_2026_09_14.md).

The supplied root-level file is unchanged. Previous audit artifacts remain historical snapshots.
