"""Compute every table the docs quote, so no figure is transcribed by hand.

Reads results/grid/*.scores.json and prints the five tables README.md,
paper_plan.md and report/results_2026_09_18.tex cite. Re-run it after any
grid change rather than editing a number in prose -- this project has twice
shipped a figure that drifted out of step with the code that produced it.

Table C is the one that matters most and the one most easily misread. Set-of-
mark marks IFC-DECLARED rooms, and damage removes declared rooms, so at L2
half the storeys carry no marks and leave the condition entirely. Comparing
pooled vision numbers across damage levels therefore compares different sets
of storeys; the intersected block restricts every condition to the units where
all of them are scorable, which is the only fair reading.

    python main/grid_tables.py | tee results/grid/_tables.txt
"""
import glob, json, os, re, sys
sys.path.insert(0, "main"); sys.path.insert(0, ".")

CELLS = {}
for p in sorted(glob.glob("results/grid/*.scores.json")):
    d = json.load(open(p))
    CELLS[os.path.basename(p)[:-12]] = d

def cell(tag):
    return CELLS.get(tag)

def agg(tag, units=None):
    d = cell(tag)
    if not d: return None
    rows = d.get("per_unit") or []
    if units is not None:
        rows = [r for r in rows if r["unit"] in units]
    if not rows: return None
    tp = sum(r["tp"] for r in rows); fn = sum(r["fn"] for r in rows)
    fp = sum(r["fp_on_reference_negative"] for r in rows)
    neg = sum(r["gt_negatives"] for r in rows)
    lc = sum(r.get("label_correct", 0) for r in rows)
    ls = sum(r.get("label_scored", 0) for r in rows)
    P = tp/(tp+fp) if tp+fp else 0.0
    R = tp/(tp+fn) if tp+fn else 0.0
    return dict(n=len(rows), f1=(2*P*R/(P+R) if P+R else 0.0), p=P, r=R,
                spec=((neg-fp)/neg if neg else None),
                lbl=(lc/ls if ls else None),
                st=d.get("extraction_statuses"))

def show(title, rows):
    print(f"\n## {title}")
    for label, tag in rows:
        a = agg(tag)
        if not a:
            print(f"  {label:34s}  (cell absent)"); continue
        sp = f"{a['spec']:.3f}" if a['spec'] is not None else "  -  "
        lb = f"{a['lbl']:.3f}" if a['lbl'] is not None else "  -  "
        print(f"  {label:34s} n={a['n']:3d}  F1={a['f1']:.3f}  P={a['p']:.3f}  "
              f"R={a['r']:.3f}  spec={sp}  lbl={lb}")

print("="*78)
print("A. THE TASK, L1: deterministic method against every model")
print("   (per storey, connected_by_door + open_passage only)")
show("text", [("glm-9b","synth150_llm-glm-9b_text"),
              ("qwen25-7b","synth150_llm-qwen25-7b_text"),
              ("qwen3-8b","synth150_llm-qwen3-8b_text")])
show("vision, marked (set-of-mark)", [
    ("qwen25-vl-7b plan_som","synth150_vlm-qwen25-vl-7b_plan_som"),
    ("qwen25-vl-7b occ_som","synth150_vlm-qwen25-vl-7b_occ_som"),
    ("qwen25-vl-7b text+plan_som","synth150_vlm-qwen25-vl-7b_text-plan_som"),
    ("internvl3-8b plan_som","synth150_vlm-internvl3-8b_plan_som"),
    ("internvl3-8b occ_som","synth150_vlm-internvl3-8b_occ_som"),
    ("internvl3-8b text+plan_som","synth150_vlm-internvl3-8b_text-plan_som")])
show("vision, unmarked", [
    ("qwen25-vl-7b plan","synth150_vlm-qwen25-vl-7b_plan"),
    ("qwen25-vl-7b occ","synth150_vlm-qwen25-vl-7b_occ"),
    ("internvl3-8b plan","synth150_vlm-internvl3-8b_plan"),
    ("internvl3-8b occ","synth150_vlm-internvl3-8b_occ")])

print("\n" + "="*78)
print("B. SET-OF-MARK LIFT (same model, same corpus, marks vs none)")
for m in ("qwen25-vl-7b","internvl3-8b"):
    for base in ("plan","occ"):
        a = agg(f"synth150_vlm-{m}_{base}"); b = agg(f"synth150_vlm-{m}_{base}_som")
        if a and b:
            mult = (b['f1']/a['f1']) if a['f1'] else float('inf')
            print(f"  {m:14s} {base:5s} {a['f1']:.3f} -> {base+'_som':9s} {b['f1']:.3f}"
                  f"   ({mult:.1f}x)")

print("\n" + "="*78)
print("C. DAMAGE. Full corpus, identical 60 units at every level.")
print("   Only the text modality can be measured on damaged storeys at all.")
for m in ("glm-9b","qwen25-7b","qwen3-8b"):
    line = f"  {m:12s} "
    for lvl in ("synth150","synth150_L2","synth150_L3","synth150_L4"):
        a = agg(f"{lvl}_llm-{m}_text")
        line += f"{a['f1']:.3f}  " if a else "  --   "
    print(line + "   (L1, L2, L3, L4)")
print()
print("   Unmarked vision is invariant by construction (byte-identical renders):")
for tag,lab in (("synth150_vlm-qwen25-vl-7b_plan","L1"),
                ("synth150_L4_vlm-qwen25-vl-7b_plan","L4")):
    a = agg(tag)
    if a: print(f"     qwen25-vl plan {lab}: F1 {a['f1']:.3f}")

print()
print("   THE SELECTION EFFECT. Set-of-mark needs marks; damage removes them.")
som_l2 = cell("synth150_L2_vlm-qwen25-vl-7b_plan_som")
common = {r["unit"] for r in (som_l2.get("per_unit") or [])} if som_l2 else set()
print(f"   Units where set-of-mark is scorable at L2: {len(common)} of 60.")
print("   On those SAME units, L1 -> L2:")
for lab, a_tag, b_tag in [
        ("text glm-9b","synth150_llm-glm-9b_text","synth150_L2_llm-glm-9b_text"),
        ("text qwen25-7b","synth150_llm-qwen25-7b_text","synth150_L2_llm-qwen25-7b_text"),
        ("text qwen3-8b","synth150_llm-qwen3-8b_text","synth150_L2_llm-qwen3-8b_text"),
        ("plan_som qwen25-vl","synth150_vlm-qwen25-vl-7b_plan_som","synth150_L2_vlm-qwen25-vl-7b_plan_som")]:
    a = agg(a_tag, common); b = agg(b_tag, common)
    if a and b:
        print(f"     {lab:22s} {a['f1']:.3f} -> {b['f1']:.3f}  ({b['f1']-a['f1']:+.3f})  n={a['n']}")

print("\n" + "="*78)
print("D. LABELS: given vs masked (the masked column is the real test)")
for m in ("glm-9b","qwen25-7b","qwen3-8b"):
    g = agg(f"synth150_llm-{m}_text"); k = agg(f"synth150_llm-{m}_text_masked")
    if g and k:
        print(f"  text   {m:14s} given {g['lbl']:.3f}   masked {k['lbl']:.3f}")
for m,mod in (("qwen25-vl-7b","text-plan_som"),("internvl3-8b","text-plan_som")):
    g = agg(f"synth150_vlm-{m}_{mod}"); k = agg(f"synth150_vlm-{m}_{mod}_masked")
    if g and k:
        print(f"  vision {m:14s} given {g['lbl']:.3f}   masked {k['lbl']:.3f}")

print("\n" + "="*78)
print("E. EXTRACTION RELIABILITY (parse outcome per model, all its cells)")
per = {}
for tag, d in CELLS.items():
    mm = re.search(r"_(?:llm|vlm)-([a-z0-9.-]+)_", tag)
    if not mm: continue
    st = d.get("extraction_statuses") or {}
    acc = per.setdefault(mm.group(1), {})
    for k, v in st.items():
        acc[k] = acc.get(k, 0) + v
for m, st in sorted(per.items()):
    tot = sum(st.values()); okp = 100*st.get("ok",0)/tot if tot else 0
    print(f"  {m:14s} {okp:5.1f}% parsed   {st}")
