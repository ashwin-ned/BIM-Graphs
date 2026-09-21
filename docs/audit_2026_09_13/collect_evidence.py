"""Recompute audit summaries without overwriting existing experiment outputs."""
from __future__ import annotations

import ast
from collections import Counter
import glob
import hashlib
import importlib.metadata as md
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "main"), str(ROOT / "annotator")]
os.chdir(ROOT)
from bimsg import graph as G, parser as P, semantics as S
from eval_semantics import collect
from eval_connectivity import score_one
from validate import check
from verify_gt import verify


def main():
    out = Path(__file__).parent
    report = {"versions": {}, "python": sys.version}
    for k in ["numpy", "shapely", "networkx", "ifcopenshell", "scipy", "transformers", "torch", "flask"]:
        try:
            report["versions"][k] = md.version(k)
        except md.PackageNotFoundError:
            report["versions"][k] = "no distribution metadata"
    report["versions"]["ifcopenshell"] = P.ifcopenshell.version
    items = collect("results/model_*.graph.json")
    rule = S.RuleBackend()
    report["rule_recomputed"] = {"n": len(items), "correct": sum(rule.predict(f) == t for _, _, f, t in items),
                                  "labels": dict(Counter(t for _, _, _, t in items))}
    report["saved_graphs"] = [check(p) for p in sorted(glob.glob("results/model_*.graph.json"))]
    report["connectivity_saved"] = {}
    for tag in ["synth150", "synth150_L2", "synth150_L3", "synth150_L4"]:
        for method in ["schema", "adjacency", "door_near", "ours"]:
            rs = json.loads(Path(f"results/bl_{tag}_{method}.json").read_text())
            tp, fp, fn = (sum(r[k] for r in rs) for k in ["tp", "fp", "fn"])
            report["connectivity_saved"][f"{tag}/{method}"] = {
                "n": len(rs), "tp": tp, "fp": fp, "fn": fn,
                "micro_f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0,
                "macro_f1": sum(2*r["tp"]/(2*r["tp"]+r["fp"]+r["fn"]) if 2*r["tp"]+r["fp"]+r["fn"] else 0 for r in rs)/len(rs),
                "unmapped_rooms": sum(r.get("unmapped_rooms", 0) for r in rs)}
    report["annotations"] = []
    for p in Path("ifc-graphx/annotated_gt").glob("*.graph.json"):
        d = json.loads(p.read_text())
        report["annotations"].append({"file": str(p), "complete": d.get("complete"), "counts": d.get("counts"),
            "held_out": len(d.get("held_out", [])), "requests": len(d.get("requests", [])), "missing_rooms": len(d.get("missing_rooms", []))})
    (out / "evidence_metrics.json").write_text(json.dumps(report, indent=2))

    manifest = []
    for root in ["main", "annotator", "ifc-graphx/annotator"]:
        for p in sorted(Path(root).rglob("*")):
            if not p.is_file() or p.suffix not in [".py", ".js", ".html", ".css"]:
                continue
            raw = p.read_bytes()
            if p.suffix == ".py":
                ast.parse(raw, filename=str(p))
            manifest.append({"path": str(p), "lines": len(raw.splitlines()), "sha256": hashlib.sha256(raw).hexdigest()})
    (out / "source_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"rule": report["rule_recomputed"], "saved_graphs": len(report["saved_graphs"]), "source_files": len(manifest), "annotations": report["annotations"]}), flush=True)

    fresh = []
    for rel in ["dataset/synthetic_data/synthetic/synth_0000.ifc", "dataset/synthetic_data/synthetic_L2/synth_0000.ifc", "dataset/test_set/model_9.ifc"]:
        t0 = time.monotonic()
        model = P.parse(rel)
        g = G.build(model)
        label = Path(rel).parent.name + "_" + Path(rel).stem
        gp = out / (label + ".graph.json")
        G.save(g, str(gp))
        row = {"input": rel, "seconds": round(time.monotonic() - t0, 2), "stats": g.graph["stats"], "checks": check(str(gp))}
        gt = Path(rel).with_suffix(".gt.json")
        if gt.exists():
            row["connectivity"] = score_one(str(gt), str(gp))
            if "_L2" not in rel:
                row["gt_verification"] = verify(rel, str(gt))
        fresh.append(row)
        (out / "fresh_runs.json").write_text(json.dumps(fresh, indent=2))
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
