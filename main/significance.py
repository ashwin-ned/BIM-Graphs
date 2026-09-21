"""Test whether the method's advantage over each baseline is real.

At 12 buildings our margin over the strongest baseline was 0.025 F1, which is
well within the range a handful of favourable samples could produce. A method
paper resting on that would not survive a request for error bars, so this
computes paired statistics over a larger corpus.

Paired tests are the right family here: every method sees the same building and
the same room set, so per-building scores are matched observations and the
between-building variance -- which is large, since buildings differ in size and
layout -- cancels out. Wilcoxon signed-rank is used rather than a t-test because
F1 is bounded and its per-building distribution is visibly non-normal.

    python main/significance.py --tag synth150 --level ""
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

try:
    from scipy import stats as sps
except Exception:
    sps = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evaluation.scoring import f1

# ifc2graph was absent here while baselines.py registered it, so the external
# comparison was run and then left out of the only significance test.
METHODS = ["schema", "adjacency", "door_near", "ifc2graph", "ours"]


def per_building_f1(path: str) -> dict:
    """Per-building F1 from a connectivity result file.

    Reads both shapes. The current scorer wraps its rows in a record carrying
    the run manifest, and names the false positives it is confident about
    `fp_on_reference_negative` -- a prediction the reference examined and ruled
    out. Predictions the reference never examined, and predictions hung on a
    room it does not contain, are deliberately *not* folded in here: they are
    reported separately and belong in the strict figure, not in a paired test
    that is comparing methods on the pairs a reference actually decided.
    """
    d = json.load(open(path))
    rows = d if isinstance(d, list) else d.get("per_building", [])
    out = {}
    for r in rows:
        tp, fn = r["tp"], r["fn"]
        fp = r.get("fp_on_reference_negative", r.get("fp", 0))
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        out[r["building"]] = f1(p, rc)
    return out


def incomplete_runs(path: str) -> int:
    """Buildings that were expected and never scored, from the run manifest.

    A paired test over buildings that silently dropped some of them is not a
    paired test over the corpus.
    """
    d = json.load(open(path))
    if isinstance(d, list):
        return -1                       # written before manifests existed
    counts = (d.get("manifest") or {}).get("counts") or {}
    return counts.get("missing", 0) + counts.get("failed", 0)


def bootstrap_ci(d: np.ndarray, n: int = 20000, alpha: float = 0.05, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n, len(d)))
    means = d[idx].mean(axis=1)
    return (float(np.quantile(means, alpha / 2)),
            float(np.quantile(means, 1 - alpha / 2)))


def compare(tag: str, level: str) -> None:
    suffix = f"{tag}{level}"
    scores = {}
    for m in METHODS:
        p = f"results/bl_{suffix}_{m}.json"
        if os.path.exists(p):
            scores[m] = per_building_f1(p)
            gaps = incomplete_runs(p)
            if gaps > 0:
                print(f"  !! {suffix}/{m}: {gaps} building(s) missing or failed "
                      f"-- this comparison is over the rest")
            elif gaps < 0:
                print(f"  -- {suffix}/{m}: written before run manifests; its "
                      f"denominator cannot be checked")
    if "ours" not in scores:
        print(f"  no results for {suffix}")
        return

    common = set.intersection(*(set(v) for v in scores.values()))
    common = sorted(common)
    n = len(common)
    ours = np.array([scores["ours"][b] for b in common])

    label = level or "  (as generated)"
    print(f"\n=== {tag}{label}   n = {n} buildings ===")
    print(f"{'method':<12}{'mean F1':>9}{'sd':>8}{'median':>8}"
          f"{'Δ vs ours':>11}{'95% CI':>20}{'p':>10}")
    print("-" * 78)

    for m in METHODS:
        if m not in scores:
            continue
        v = np.array([scores[m][b] for b in common])
        if m == "ours":
            print(f"{m:<12}{v.mean():>9.4f}{v.std(ddof=1):>8.4f}"
                  f"{np.median(v):>8.4f}{'--':>11}{'--':>20}{'--':>10}")
            continue
        d = ours - v                      # positive means ours is better
        lo, hi = bootstrap_ci(d)
        if sps is not None and np.any(d != 0):
            try:
                p = sps.wilcoxon(ours, v, zero_method="zsplit").pvalue
            except Exception:
                p = float("nan")
        else:
            p = float("nan")
        ps = "<1e-6" if p < 1e-6 else f"{p:.2e}" if p == p else "n/a"
        print(f"{m:<12}{v.mean():>9.4f}{v.std(ddof=1):>8.4f}"
              f"{np.median(v):>8.4f}{d.mean():>+11.4f}"
              f"{f'[{lo:+.4f},{hi:+.4f}]':>20}{ps:>10}")

    # Win/loss against the strongest baseline is more legible than a mean.
    if "door_near" in scores:
        v = np.array([scores["door_near"][b] for b in common])
        d = ours - v
        w = int((d > 1e-9).sum()); l = int((d < -1e-9).sum()); t = n - w - l
        print(f"\n  vs door_near (strongest baseline): "
              f"{w} wins / {l} losses / {t} ties")
        if l:
            worst = sorted(zip(d, common))[:3]
            print("  losses on: " + ", ".join(f"{b} ({dd:+.3f})" for dd, b in worst))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="synth150")
    ap.add_argument("--levels", nargs="*", default=["", "_L2", "_L3", "_L4"])
    args = ap.parse_args()
    if sps is None:
        print("!! scipy unavailable; p-values will be omitted")
    for lv in args.levels:
        compare(args.tag, lv)


if __name__ == "__main__":
    main()
