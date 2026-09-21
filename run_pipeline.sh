#!/usr/bin/env bash
# BIM-Graphs: full pipeline, end to end.
#
#   ./run_pipeline.sh [n_buildings]
#   BIMSG_PYTHON=/path/to/python ./run_pipeline.sh
#   ./run_pipeline.sh --regenerate 20     # rebuild the synthetic corpus first
#
# 1. generate synthetic buildings with known connectivity ground truth
# 2. verify that ground truth against the emitted geometry, independently
# 3. apply graduated IFC damage (the severity axis)
# 4. recover graphs at every damage level, plus baselines
# 5. score connectivity against ground truth
# 6. run the real 25-model corpus through necessary-condition checks
#
# Every stage's failure is visible and fatal. The previous version ran under
# `set -u` alone, sent each stage's stderr to /dev/null, and printed DONE
# whatever happened -- so a stage that died left the scorers to average over
# whichever outputs happened to exist, which quietly shrank the denominator and
# improved the reported score. It also deleted the synthetic corpus on every
# run, which made a result impossible to reproduce against the inputs that
# produced it.
set -euo pipefail

cd "$(dirname "$0")" || exit 1

PY=${BIMSG_PYTHON:-$HOME/miniforge3/envs/bim-sg/bin/python}
if ! [ -x "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=$(command -v python3)
    else echo "no usable Python: set BIMSG_PYTHON" >&2; exit 1; fi
fi

REGENERATE=0
if [ "${1:-}" = "--regenerate" ]; then REGENERATE=1; shift; fi
N=${1:-12}

DS=dataset/synthetic_data
LOGS=results/run_logs
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$LOGS"
FAILED=()

hr()  { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
note(){ printf '  %s\n' "$1"; }

# Run a stage, keep its log, and remember a failure instead of hiding it.
stage() {
    local name=$1; shift
    local log="$LOGS/${RUN_ID}_${name}.log"
    if "$@" >"$log" 2>&1; then
        return 0
    fi
    FAILED+=("$name")
    note "FAILED: $name (see $log)"
    return 1
}

hr "0. ENVIRONMENT"
note "python   $($PY -c 'import sys;print(sys.version.split()[0])') at $PY"
note "run id   $RUN_ID"
note "logs     $LOGS/${RUN_ID}_*.log"

hr "0b. CORPUS INTEGRITY"
# A truncated IFC does not raise -- IfcOpenShell opened a half-copied
# dataset/test_set/model_2.ifc without complaint, reported a plausible 6 storeys
# and 35 spaces, and a person annotated the result. So the corpus is checked
# against its manifest before anything reads it, and unlike every other stage
# here a failure ABORTS: scoring a corpus you cannot vouch for wastes the run
# and, worse, produces numbers that look fine.
CORPUS_BAD=0
for d in dataset/test_set "$DS"/synth150 "$DS"/synth150_L2 "$DS"/synth150_L3 "$DS"/synth150_L4; do
    [ -d "$d" ] || continue
    if stage "verify_$(basename "$d")" "$PY" main/verify_corpus.py "$d"; then
        note "ok       $d"
    else
        CORPUS_BAD=1
        note "CORRUPT  $d -- see the log above"
    fi
done
if [ "$CORPUS_BAD" = "1" ]; then
    printf '\n\033[1mAborting: a source corpus is not intact.\033[0m\n'
    note "Fix the file, or re-record the manifest with --write if the change was intended."
    exit 1
fi

hr "1. GENERATE  ($N synthetic buildings)"
if [ "$REGENERATE" = "1" ] || ! compgen -G "$DS/synthetic/*.gt.json" >/dev/null; then
    # Only ever removed on an explicit request. An experiment whose inputs were
    # deleted by the script that scored it cannot be reproduced.
    if [ "$REGENERATE" = "1" ]; then rm -rf "$DS/synthetic" "$DS"/synthetic_L*; fi
    stage generate "$PY" main/generate_building.py --out "$DS/synthetic" --n "$N" || true
    note "generated into $DS/synthetic"
else
    note "reusing $(ls "$DS"/synthetic/*.gt.json | wc -l) existing buildings "\
"(pass --regenerate to rebuild)"
fi

hr "2. VERIFY GROUND TRUTH  (independent flood-fill)"
stage verify_gt "$PY" main/verify_gt.py --dir "$DS/synthetic" || true
tail -2 "$LOGS/${RUN_ID}_verify_gt.log" 2>/dev/null || true

hr "3. DAMAGE  (severity levels)"
for L in 1 2 3 4; do
    if [ "$REGENERATE" = "1" ] || ! compgen -G "$DS/synthetic_L$L/*.ifc" >/dev/null; then
        stage "damage_L$L" "$PY" main/damage_ifc.py --src "$DS/synthetic" \
            --out "$DS/synthetic_L$L" --level "$L" || true
    fi
    note "L$L: $(compgen -G "$DS/synthetic_L$L/*.ifc" >/dev/null && ls "$DS/synthetic_L$L"/*.ifc | wc -l || echo 0) files"
done

hr "4. RECOVER  (ours + baselines, every level)"
for D in synthetic synthetic_L2 synthetic_L3 synthetic_L4; do
    stage "baselines_$D" "$PY" main/baselines.py --src "$DS/$D" \
        --out "results/bl_$D" || true
    for m in schema adjacency door_near ifc2graph ours; do
        # ifc2graph was missing from this loop while baselines.py generated its
        # output by default (all 5 registered methods) -- so the external-tool
        # comparison was computed and never scored. Same gap main/significance.py
        # had; fixed there too.
        #
        # --allow-missing here only so one incomplete method does not abort the
        # sweep; the manifest in each result file still records what was
        # missing, and stage 5 reads it back rather than trusting the average.
        stage "score_${D}_$m" "$PY" main/eval_connectivity.py --gt "$DS/$D" \
            --graphs "results/bl_$D/$m" --out "results/bl_${D}_$m.json" \
            --allow-missing || true
    done
    note "$D done"
done

hr "5. CONNECTIVITY SCORES  (F1 vs known ground truth)"
"$PY" - <<'PY'
import json, os
lvls = [("synthetic", "L0/L1"), ("synthetic_L2", "L2"),
        ("synthetic_L3", "L3"), ("synthetic_L4", "L4")]

def read(p):
    d = json.load(open(p))
    if isinstance(d, list):
        # Written by the previous scorer, before pooled figures and manifests.
        return None, None
    pooled, man = d.get("pooled") or {}, d.get("manifest") or {}
    return pooled, man.get("counts") or {}

print(f"{'method':<12}" + "".join(f"{l:>10}" for _, l in lvls))
print("-" * 52)
for m in ("schema", "adjacency", "door_near", "ifc2graph", "ours"):
    row = f"{m:<12}"
    for lvl, _ in lvls:
        p = f"results/bl_{lvl}_{m}.json"
        if not os.path.exists(p):
            row += f"{'-':>10}"
            continue
        pooled, _ = read(p)
        row += f"{'stale':>10}" if pooled is None else f"{pooled.get('f1', 0):>10.3f}"
    print(row)
print()
for lvl, lab in lvls:
    p = f"results/bl_{lvl}_ours.json"
    if not os.path.exists(p):
        continue
    pooled, counts = read(p)
    if pooled is None:
        print(f"  ours @ {lab:<6} result file predates the current scorer -- "
              f"re-run before quoting it")
        continue
    incomplete = counts.get("missing", 0) + counts.get("failed", 0)
    # A score over a corpus that lost buildings is not a score for that corpus.
    flag = (f"   [{incomplete} of {sum(counts.values())} buildings missing or "
            f"failed -- this average is over the rest]" if incomplete else "")
    print(f"  ours @ {lab:<6} precision {pooled.get('precision', 0):.3f}  "
          f"recall {pooled.get('recall', 0):.3f}  "
          f"F1 {pooled.get('f1', 0):.3f}  "
          f"strict F1 {pooled.get('f1_strict', 0):.3f}{flag}")
PY

hr "6. REAL CORPUS  (25 models, necessary conditions)"
stage build_real "$PY" main/build_graph.py 'dataset/test_set/*.ifc' --out results || true
stage validate "$PY" main/validate.py || true
grep -E "R1a|R1b|R2_|R3_|R4_|R5_|R6_|mean|total rooms" \
    "$LOGS/${RUN_ID}_validate.log" 2>/dev/null || true

hr "7. SEMANTICS AND GOAL RESOLUTION  (rule backend, no GPU)"
# The rule backend costs nothing and was going stale here: this project's own
# audit found these two never ran as part of a standard pipeline execution, so
# "pending re-measurement" sat in README.md well past the fix it was blocked
# on. The LLM and VLM backends are NOT run here -- they need a free GPU and, at
# ~20 s/room for the VLM, an hours-long budget -- run them by hand:
#   python main/eval_semantics.py --backend llm
#   python main/eval_semantics.py --backend vlm
stage semantics_rule "$PY" main/eval_semantics.py --backend rule \
    --graphs 'results/model_*.graph.json' --out results/semantics_eval.json || true
stage goal_resolution "$PY" main/eval_goal_resolution.py \
    --graphs 'results/model_*.graph.json' --out results/goal_resolution_eval.json || true
grep -E "accuracy|resolution:|reachable" \
    "$LOGS/${RUN_ID}_semantics_rule.log" "$LOGS/${RUN_ID}_goal_resolution.log" \
    2>/dev/null || true

if [ ${#FAILED[@]} -gt 0 ]; then
    hr "FAILED"
    for f in "${FAILED[@]}"; do echo "  $f  -> $LOGS/${RUN_ID}_$f.log"; done
    echo
    echo "  Scores printed above are over whatever survived. Fix these stages"
    echo "  before quoting any number from this run."
    exit 1
fi

hr "DONE"
echo "  synthetic GT   $DS/synthetic/*.gt.json"
echo "  graphs         results/  results/bl_*/"
echo "  scores         results/bl_*_*.json  (+ .manifest.json beside each)"
echo "  logs           $LOGS/${RUN_ID}_*.log"
