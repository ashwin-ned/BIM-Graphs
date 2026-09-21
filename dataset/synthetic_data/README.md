# Synthetic dataset

> **The corpus is unchanged; the figures quoted from it are not.** Every
> connectivity F1 in this file was produced by the scorer repaired on
> 14 September 2026, which dropped edges touching unmatched rooms and let
> several recovered rooms stand for one reference room. Re-run before quoting:
> `./run_pipeline.sh --regenerate 150`. The generator, damage levels and the
> independent flood-fill verification described below are unaffected. See
> [the M0 record](../../docs/M0_IMPLEMENTATION_2026_09_14.md) and
> `results/_pre_m0_snapshot/README.md`.

Procedurally generated IFC buildings with **known, exact connectivity ground
truth**. This is what makes precision/recall measurable at all: no real IFC
file states which rooms are actually connected (see the audit in
`docs/DESIGN_v3.md` §1 — zero `IfcRelSpaceBoundary` across 257 MB of real
models), so the recovery method has nothing to be scored against unless the
answer is fixed by construction. Here it is: we choose the room layout and
decide which neighbouring rooms get a door, then emit an IFC that expresses
the geometry. Edge precision/recall against that choice is exact and free.

This directory holds two generations of that corpus (§ Two generations
below) and, for the current one, four levels of controlled damage applied on
top of it. Nothing in `2025_BIMfundamentals/` or `test_set/` (the real-IFC
corpora one level up) belongs here or was touched by this reorganisation.

## Layout

```
synthetic_data/
  synth150/           150 buildings, clean            (current, primary corpus)
  synth150_L2/         "      "      L2 damage
  synth150_L3/         "      "      L3 damage
  synth150_L4/         "      "      L4 damage
  synthetic/           8 buildings, clean              (pilot corpus, superseded)
  synthetic_L1/         "     "     L1 damage
  synthetic_L2/         "     "     L2 damage
  synthetic_L3/         "     "     L3 damage
  synthetic_L4/         "     "     L4 damage
```

Each building is a pair of files sharing a basename, `synth_NNNN.ifc` (a real,
valid IFC4 model) and `synth_NNNN.gt.json` (the connectivity ground truth for
that model — see below). Every damage level directory holds the *same*
`synth_NNNN.ifc` basenames as its clean counterpart, mangled per the recipe
below; **the `.gt.json` ground truth is identical across all damage levels of
the same building**, because damage is applied only to the file describing
the connectivity, never to the connectivity itself.

## Two generations

| | `synthetic*` (pilot) | `synth150*` (main) |
|---|---|---|
| buildings | 8 | 150 |
| total rooms | 99 | 2,049 |
| total connectivity edges | 77 | 1,708 |
| total non-adjacent negatives | 59 | 1,211 |
| damage levels present | L1–L4 | L2–L4 (L1 omitted, see below) |
| status | superseded, kept for reference | current, used for all reported results |

The pilot corpus was the first working version of the generator, built to
validate the approach on a handful of buildings before committing to a full
run. `synth150` is the scaled-up corpus used for every connectivity result in
`README.md` and `docs/DESIGN_v3.md` (F1 vs. baselines, significance testing,
the Ifc2Graph/IFC2SB external-baseline comparisons) — it exists because 8
buildings is too few for the paired Wilcoxon significance test to mean much
(`main/significance.py`). The pilot corpus generated an explicit `L1` damage
level; by the time `synth150` was generated, `main/damage_ifc.py`'s own
docstring already recorded that L1 (stripping `IfcRelSpaceBoundary`) is a
**no-op on generated files** — the authoring API never emits space boundaries
in the first place — so regenerating an identical-to-L0 L1 folder for 150
buildings was skipped as redundant. `synth150`/`synth150_L0` are the same
files; there is no separate `synth150_L0` or `synth150_L1` directory.

Both generations use the same generator, ground-truth schema, and damage
recipe described below; nothing about the *method* changed between them,
only the scale.

## How it was generated

`main/generate_building.py` lays out each floor by recursive rectangular
subdivision (`subdivide()`, default `depth=3`, default plate `18.0 × 12.0` m,
`STOREY_H=3.20` m, `WALL_T=0.20` m), assigns each resulting room a name drawn
from a fixed vocabulary (`ROOM_NAMES` — Office, Meeting Room, Store, Lab,
Bedroom, Kitchen, Living Room, Bathroom, Corridor, Reception, Utility, plus
Stair rooms for vertical circulation), then decides, for every pair of
geometrically adjacent rooms, whether they are actually connected:

- a random `door_ratio` (default 0.6) of adjacent pairs get a real door
  (`kind: "door"` in the ground truth, an `IfcDoor` with a proper
  world-frame placement in the emitted IFC — door orientation is generated
  correctly per wall, not a fixed identity rotation);
- a further `open_ratio` (default 0.15) get a door-less opening
  (`kind: "open_passage"` — a gap at least `OPENING_W=1.60` m wide, no
  `IfcDoor`);
- **the remainder are left as solid wall** — geometrically adjacent, but not
  connected. These are the `adjacent_not_connected` negatives in the ground
  truth, and they are the single most important design decision in the
  generator: if every adjacent pair were connected, a trivial "join every
  neighbour" baseline would score perfect recall and near-perfect precision,
  and the benchmark could not distinguish it from a real method. With
  negatives present, `adjacency` (the naive baseline in `main/baselines.py`)
  scores 0.632 F1 on `synth150` — nowhere near ours (0.996).

Stairs connect vertically adjacent storeys (`type: "vertical"` edges).
`emit_ifc()` writes a real IFC4 file via `ifcopenshell.api` — proper
`IfcSpace`, `IfcBuildingStorey` containment, and declared units — deliberately
clean, so that damage can be applied afterwards as a controlled variable
(next section) rather than baked into the generator itself. Room footprints
are inset by `WALL_T/2` so a shared wall between two rooms reads as genuinely
solid, matching how real `IfcSpace` stops at the wall face rather than its
centreline — a fix that mattered for passage detection during development
(`docs/DESIGN_v3.md`, "Things that cost real debugging time").

Regenerate or extend the corpus with:
```bash
python main/generate_building.py --out dataset/synthetic_data/synthetic --n 20 \
    [--storeys 2] [--depth 3] [--door-ratio 0.6] [--seed0 0]
```
`synth150` was generated with `--n 150` and otherwise-default parameters.

## Ground truth format (`synth_NNNN.gt.json`)

```json
{
  "building": "synth_0000",
  "seed": 0,
  "storeys": 2,
  "rooms": [
    {"rid": "S0R000", "x0": 0.0, "y0": 0.0, "x1": 4.83, "y1": 5.31,
     "storey": 0, "name": "Stair"},
    ...
  ],
  "edges": [
    {"a": "S0R000", "b": "S0R001", "type": "door"},
    ...
  ],
  "n_rooms": 16, "n_edges": 13,
  "adjacent_not_connected": [
    {"a": "S0R000", "b": "S0R002"},
    ...
  ],
  "n_negatives": 9
}
```

`rid` matches the `Name` attribute the *generator* writes onto the
corresponding `IfcSpace` in the sibling `.ifc` file (`S{storey}R{index}`),
which is how `main/eval_connectivity.py` and every baseline in
`main/baselines.py` join a recovered room back to its ground-truth row: by
name, not by geometry. `edges[].type` is one of `door`, `open_passage`,
`vertical`; `adjacent_not_connected` pairs are geometrically touching rooms
with no door and no opening — the discriminative negatives described above.
Room coordinates (`x0,y0,x1,y1`) are the *generator's* layout rectangle in
plan, storey-local, metres.

## Ground truth is independently verified, not assumed

The generator and the recovery pipeline share an author, so a shared
misconception in either could produce a perfect-looking score that means
nothing. `main/verify_gt.py` checks the ground truth against a *second,
unrelated mechanism*: rather than reasoning about polygon strips between room
footprints (what the pipeline does), it rasterises the wall solids at 0.05 m
resolution and flood-fills free space at door height, asking whether one
room's interior can physically reach another's. Agreement between two
independent methods is evidence the ground truth is real; disagreement would
localise a bug in one of them.

```bash
python main/verify_gt.py --dir dataset/synthetic_data/synth150
```

Current result: **2,769/2,769 room-pairs agree (100.0%)** across all 150
buildings.

## Damage model (L1–L4)

`main/damage_ifc.py` applies graduated, *measured* damage on top of a clean
building — each level reproduces a pathology found in the real 25-model
corpus, at (or, for L2, at exactly) its measured real-world prevalence, not
an invented one. The ground truth (`.gt.json`) is never touched by damage:
the connectivity the generator chose remains the answer regardless of how
badly the file describing it is mangled.

| level | what changes | matches (real corpus) |
|---|---|---|
| L0 | pristine, no-op | not observed in the wild — generated files start "ahead" of real ones |
| L1 | strip `IfcRelSpaceBoundary`/`1stLevel`/`2ndLevel` | 100% of real models have none — but this is a **no-op on generated files**, which never had any either |
| L2 | delete `IfcSpace` on a random fraction of storeys (default 49%) | 49% of real storeys have no `IfcSpace` |
| L3 | sever remaining spaces from their storey (`IfcRelAggregates`) | observed in `model_2` of the real corpus |
| L4 | drop all `IfcRelContainedInSpatialStructure`, detach `IfcUnitAssignment`, and rescale every coordinate ×100 (undeclared centimetre units) | observed in `model_2` of the real corpus |

Levels are cumulative (`LEVELS = {2: ["boundaries","spaces"], 3: [...,
"orphan"], 4: [...,"containment","units"]}`), so L4 carries L2 and L3's
damage too. Regenerate a level with:
```bash
python main/damage_ifc.py --src dataset/synthetic_data/synthetic \
    --out dataset/synthetic_data/synthetic_L2 --level 2 [--fraction 0.49] [--seed 0]
```

Reported result (`docs/DESIGN_v3.md` §1, §7): L2, L3 and L4 land
**identically** on connectivity F1 across all 150 buildings — hierarchy
repair and empirical unit detection absorb their damage completely, and only
the missing rooms L2 introduces degrade connectivity. This is itself a
finding, not an assumption: it says room recovery, not hierarchy or unit
repair, is the binding constraint under damage.

## Corpus statistics (current, `synth150*`)

- 150 buildings × 2 storeys, `18 × 12` m plate, `depth=3` subdivision
- 2,049 rooms total (mean 13.7/building), 1,708 ground-truth edges
  (1,293 door, 265 open-passage, 150 vertical/stair), 1,211
  adjacent-but-unconnected negatives
- room-name vocabulary and counts: Stair 300, Living Room 181, Kitchen 174,
  Corridor 166, Lab 166, Bathroom 165, Store 164, Office 163, Meeting Room
  151, Utility 144, Reception 141, Bedroom 134
- clean `.ifc` files average ~100 KB; L2–L4 variants are comparable in size
  (damage removes/detaches entities, it does not add bulk)

## Who reads this data

| script | reads |
|---|---|
| `main/verify_gt.py` | independent GT cross-check (raster flood-fill) |
| `main/eval_connectivity.py` | scores a recovered graph against `.gt.json` — precision/recall/F1, edge-type accuracy |
| `main/baselines.py` | runs every method (`schema`, `adjacency`, `door_near`, `ours`, `ifc2graph`) over `*.ifc` |
| `main/significance.py` | paired Wilcoxon + bootstrap CI over per-building scores already computed by the two above |
| `main/damage_ifc.py` | generates the `*_L2`/`*_L3`/`*_L4` directories from a clean one |

All defaults in these scripts point at `dataset/synthetic_data/synthetic`
(the pilot corpus) unless a full corpus run is intended, in which case
`--src`/`--gt`/`--dir dataset/synthetic_data/synth150` (and its `_L2`/`_L3`/
`_L4` siblings) is passed explicitly, matching every reported result in
`README.md` and `docs/DESIGN_v3.md`.
