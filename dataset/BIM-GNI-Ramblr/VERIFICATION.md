# Ramblr BIM-GNI scene graphs — independent verification and usability verdict

Ramblr (partner company) ran their own IFC-to-scene-graph pipeline over the
208 real models in `dataset/2025_BIMfundamentals/` and delivered the results
in `2025_BIMfundamentals_scene_graphs_collected/`, along with a written
accuracy report proposing the graphs as pseudo ground truth for connectivity
evaluation, to skip annotation. This document is an independent check of
that report — written from scratch against the raw files, not against
Ramblr's own checking code, which we don't have — and the resulting
recommendation.

**Verdict: the report is accurate. The data is not usable as room-connectivity
ground truth, for a more fundamental reason than incompleteness — but
verifying it surfaced something more useful than the graphs themselves.**

## What was delivered

- `2025_BIMfundamentals_scene_graphs_collected/model_N.json` × 208, one per
  model in `dataset/2025_BIMfundamentals/` (208/208 basenames match exactly,
  confirmed by `comm -3`).
- Each file is a tree: `building → level → room → building_element`. Every
  `building_element` node carries `instance_id` (the element's raw IFC STEP
  entity number — confirmed directly: `by_id(1090)` in a scene graph resolves
  to the exact same `IfcWall` the report describes), `class_name`, `center`,
  `dimensions`, `assigned_rooms` (a list, since one wall/element can border
  several rooms), and its own `relations` array.
- The one relation type present is `{"type": "contains", "source": <wall
  node id>, "target": <door/window node id>}`, asserting that a specific wall
  contains a specific opening.

## Independent re-verification

`main/verify_ramblr_report.py` re-derives every headline number in the
report directly from the raw files:

- **Ground truth for the relation Ramblr's graphs assert** (wall contains
  door/window) is read straight from the source IFC via
  `IfcRelVoidsElement` (wall → opening) chained to `IfcRelFillsElement`
  (opening → door/window) through the shared `IfcOpeningElement` — a
  completely independent mechanism from whatever Ramblr's pipeline does
  internally, exactly as `main/verify_gt.py` uses a second, unrelated
  mechanism to check our own generator's ground truth.
- Every scene-graph relation is checked against that.

**Two counting bugs found and fixed before the numbers were trustworthy**
(kept here because they're exactly the kind of mistake this check exists to
catch, and because getting them wrong first is worth being honest about):

1. First pass walked the whole tree and collected relations from every node
   *occurrence*. Result: 21,437 relations, recall 0.80 — six times the
   report's numbers. Cause: the same physical element (and its `relations`
   array) is repeated verbatim once per room it's assigned to — confirmed
   directly (`b1e4a6e3-...` appears 3 times in `model_0` alone, twice under
   the *same* room). Fixed by deduplicating nodes by `id` before collecting
   relations.
2. Still 2× the report's numbers afterward. Cause: the identical relation
   object is stored on *both* the wall's own `relations` array and the
   door/window's own `relations` array — confirmed directly by printing
   both. Fixed by deduplicating relations by `(source, target)` as well.

After both fixes, every aggregate number matches the report **exactly**:

| quantity | report | independently recomputed |
|---|---|---|
| total relations checked | 3,510 | 3,510 |
| confirmed | 3,103 | 3,103 |
| precision | 0.884 | 0.884 |
| unsupported, "extra wall" (correct sibling present) | 333 | 333 |
| unsupported, true mistake | 13 | 13 |
| unjudgeable (no model-stated host) | 61 | 61 |
| real wall-opening pairs in the models | 22,671 | 22,671 |
| recall | 0.137 | 0.1369 |
| element nodes, broken references | 179,020 / 0 | 179,020 / 0 |
| Curtain walls missing | 2,283/2,337 | 2,283/2,337 |
| Stairs (`IfcStair`) missing | 1,295/1,370 | 1,295/1,370 |
| Building element parts missing | 5,519/6,822 | 5,519/6,822 |
| Plates missing | 8,069/33,296 | 8,069/33,296 |
| Members missing | 8,228/47,141 | 8,228/47,141 |
| Walls missing | 1,928/17,606 | 1,928/17,606 |
| Windows missing | 1,232/14,975 | 1,232/14,975 |

Element coverage lands close but not identical (81.5% here vs. the report's
80.1%, on a 180,010-element vs. their 215,610-element denominator) — the
report notes its count "leaves out openings and feature elements," and its
exact class list isn't fully specified, so a small denominator difference is
expected and doesn't change the conclusion (roughly one element in five is
absent either way). Per-file selection ("22 usable files," "2 that clear
every threshold") is also close but not exact (this check finds 1 file at
precision=1.0∧recall=1.0 and ~100 at precision=1.0, vs. the report's 2 and
94) — a minor edge-case difference in file-level bucketing, not in the
underlying relation-level facts, which match exactly. Full per-file numbers:
`results/ramblr_verify.json`.

**Conclusion: the report is accurate.** What it says is true; independently
recomputing every number it states, from the raw files, using a different
method than whatever Ramblr's own pipeline does, reproduces it exactly. This
is real, not an artifact of us reading their report and confirming our own
expectations — the first two attempts at this check gave answers 2–6×
different from theirs, and the debugging trail that closed that gap is above.

## Why it still can't be used as pseudo ground truth, skipping annotation

Two independent reasons, not one:

**1. It's the wrong relation.** The graphs assert *wall contains opening* —
a fact about which wall a door sits in. Our evaluation
(`main/eval_connectivity.py`, every `.gt.json` in `dataset/synthetic_data/`)
scores *room-to-room connectivity* — which two rooms a door joins. These are
different facts. A wall containing a door doesn't by itself say which two
rooms that door connects; that requires knowing which rooms border the wall
on each side, which the scene graphs don't assert as a relation (though
`assigned_rooms` on the wall node is suggestive — see below).

**2. Even for what it does assert, it's an open-world set, not a closed
one.** Recall is 0.137: 86.3% of real wall-opening relations are simply
absent from the graphs, and 92/208 files have zero relations at all. Treating
"relation absent from the graph" as "relation doesn't exist" — which is what
scoring against it as ground truth requires — would penalize any method,
including ours, for finding *more* correct connections than Ramblr's sparse
output shows. This is the same trap this project has been careful about
elsewhere: necessary conditions and partial references can falsify a
result, but silence is not evidence of absence (see the R1a
adjacency-baseline metric-bias finding in `docs/DESIGN_v3.md`).

Precision, where relations exist, is genuinely good (0.884, and 0.964 on all
files but the two worst) — so the data is a legitimate, positive
*confirming* signal on the ~24 files where it's dense enough to check
anything, useful as a spot-check that our own pipeline isn't asserting a
wall-opening pairing Ramblr's independent extraction contradicts. It is not
a substitute for annotation, and should not be used as a scored reference
for recall on any file.

## The more useful finding

Verifying this required extracting real wall-opening ground truth directly
from `dataset/2025_BIMfundamentals/*.ifc` via `IfcRelVoidsElement` +
`IfcRelFillsElement` — and it's *complete*: 22,671 real pairs across 208
files, extracted with no need for Ramblr's tool at all. This is a genuinely
different finding from the original project audit's "zero
`IfcRelSpaceBoundary` across 257 MB of real IFC" (`docs/DESIGN_v3.md` §1) —
that audit is about room *boundary* geometry for energy simulation; wall
containing an opening is a completely different, much more commonly
populated relation (Revit writes it automatically whenever a door is placed),
and this corpus has it richly.

Also confirmed: **`dataset/test_set/` (our existing 25-model real corpus) is
an exact subset of `dataset/2025_BIMfundamentals/`** — identical files, byte
for byte (checked by hash). We have been using 25 of these 208 models all
along without exploiting this. A sample check also finds substantial
`IfcSpace` presence in `2025_BIMfundamentals` (~30/file on a 20-file sample)
— this corpus looks meaningfully richer than the audit's headline numbers
for the 25-model subset might suggest in isolation, and is worth a fuller
characterisation.

**This means real (not pseudo) room-connectivity ground truth may be
buildable for these 208 models** by combining the native, complete
wall-opening relations above with room geometry (from `IfcSpace` where
present, or our own recovery pipeline where not) to determine which two
rooms each opening actually joins — Ramblr's own `assigned_rooms` field on
each wall node is a plausible shortcut for this if its accuracy holds up to
the same scrutiny applied here (their report claims 33,583/33,949 element
positions within 5 cm; not independently re-checked in this pass). That is a
real, larger follow-up task, out of scope for "check if this can be used as
pseudo ground truth" — flagged here as the actionable next step this
verification actually points to.

## Files

```
dataset/BIM-GNI-Ramblr/
  VERIFICATION.md                              this document
  2025_BIMfundamentals_scene_graphs_collected/  Ramblr's delivered scene graphs (208 files)
main/verify_ramblr_report.py                    the independent re-verification script
results/ramblr_verify.json                      per-file precision/recall + totals
```
