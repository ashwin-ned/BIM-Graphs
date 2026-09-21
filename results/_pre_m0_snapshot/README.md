# Results as they stood before the M0 repairs

Everything here was produced **before 14 September 2026** by code the
[codebase audit](../../docs/CODEBASE_AUDIT_2026_09_13.md) then found defective.
It is kept because it is the evidence behind the ICRA workshop paper and the
ICRA 2027 draft, and deleting it would make those numbers unverifiable. It is
kept *here*, out of the active tree, because none of it can be quoted as a
current result.

## Do not quote these

| Contents | Why it cannot stand |
|---|---|
| `scores/bl_synth150*.json` — the 150-building sweep, four damage levels, four methods | Produced by the pre-repair `eval_connectivity`: edges touching an unmatched room were dropped from the accounting, and several recovered rooms could stand for one reference room, so an invented room and its edges cost a method nothing (A10). The `schema` baseline also read any shared boundary as a door, including solid party walls. |
| `scores/bl_synthetic*.json`, `conn*.json`, `connectivity_eval.json` | Same scorer. Superseded by fresh runs. |
| `scores/real_connectivity_*.json`, `real_gt_calibration.json` | Scored against a derived reference built while `derive_real_gt` scaled tessellated geometry by the declared unit a second time. On the seven millimetre-declaring models that discarded **every** doorway void, so the reference declared every adjacent pair separated (A09). |
| `scores/semantics_eval.json`, `semantics_eval_prerepair.json` | Label masking blanked the room's label but left the vertical link that label had produced, so `n_vertical` carried the held-out answer to the classifier (A11). The repaired measurement is `../semantics_eval_rule_m0.json`. |
| `scores/goal_resolution_eval.json`, `scenegraph_eval.json` | Computed over graphs whose parallel portals had been collapsed at build time (A02). |
| `scores/detector_probe.json`, `probe_clutter_early.json` | One-off probes from early development. |
| `graphs-and-references-before-m0.tar.gz` | Every graph directory and derived reference behind the above: `bl_real`, `bl_real208`, `bl_synth150{,_L2,_L3,_L4}`, `bl_synthetic{,_L2,_L3,_L4}`, `real_gt`, `real_gt25`, `synthetic{,_L1,_L2,_L3,_L4}`. |
| `graphs-before-m0-*.tar.gz` | The 25 real graphs as built before portal identity existed. Not one of them contains a single parallel `(source, target, relation)` entry, because the builder collapsed two doors between the same pair of rooms before the file was written. That is the evidence for A02. |

## What replaced them

`main/eval_connectivity.py`, `main/evaluate.py` and `main/eval_real_connectivity.py`
now score through `evaluation/`, write a run manifest beside every result, and
exit non-zero when an expected building was never produced. See the
[M0 record](../../docs/M0_IMPLEMENTATION_2026_09_14.md).

To regenerate the synthetic sweep from scratch:

```bash
./run_pipeline.sh --regenerate 150
```

That rebuilds inputs as well as outputs, so it will not reproduce these files
byte for byte; the generator is seeded, but the damage levels and the scorer
have both changed.
