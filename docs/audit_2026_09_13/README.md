# Audit evidence and reproduction

> **These are a snapshot of 13 September 2026, not a maintained test suite.**
> The M0 repairs ([record](../M0_IMPLEMENTATION_2026_09_14.md)) mean several
> probes here deliberately no longer reproduce. The live regression suite is
> `tests/test_m0_regressions.py`, which asserts the repairs hold *and* asserts
> that the four unrepaired geometry findings are still present.

These artifacts support the [codebase audit](../CODEBASE_AUDIT_2026_09_13.md) and [pipeline redesign](../PIPELINE_REDESIGN_2026_09_13.md). They describe the source snapshot of 13 September 2026. Production source and existing experiment outputs were not changed.

| Artifact | Contents |
|---|---|
| `source_manifest.json` | Paths, line counts and SHA-256 hashes for the audited first-party source inventory, including the duplicate annotator copy. |
| `probes.py`, `probe_results.json` | Small fixtures demonstrating current defects. The output records observed behavior; successful script execution does **not** mean the implementation is correct. |
| `collect_evidence.py`, `evidence_metrics.json` | Fresh rule predictions on saved graphs, current graph checks, summaries of saved synthetic results, annotation export counts and environment versions. |
| `fresh_runs.json`, three `*.graph.json` files | Fresh IFC builds for one clean synthetic example, its L2 variant and real model 9; conditional connectivity scores where synthetic truth exists. |
| `test_results.json`, `test_*.log` | Results from all eight existing annotation suites. The browser suite passed 57 checks. The `skip_lines` entry in the checklist result is a passed test about a skipped model, not a skipped test suite. |
| `collect_evidence.log` | Output from the evidence collection and fresh builds. |

From the workspace root, the diagnostic scripts can be rerun using the existing environment:

```bash
/mnt/data/home/anedung/miniforge3/envs/bim-sg/bin/python docs/audit_2026_09_13/probes.py
/mnt/data/home/anedung/miniforge3/envs/bim-sg/bin/python docs/audit_2026_09_13/collect_evidence.py
```

`probes.py` prints its observations and uses temporary directories for fixtures. `collect_evidence.py` rewrites this folder's metrics, source manifest and three fresh graph artifacts; preserve the folder first if the original audit snapshot is needed. It reads the existing `results/` files and does not rerun the 150-building experiments. Neither script invokes an LLM.

The existing test suite paths are recorded in `test_results.json`. They were run from the workspace root with the same environment's Python and its `bin` directory added to `PATH` so browser/parity tests could find Node. No additional regression suite was added to production code: these probes are the starting cases for the proposed repairs.
