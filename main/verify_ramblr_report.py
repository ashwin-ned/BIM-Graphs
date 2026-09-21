"""Independent re-derivation of Ramblr's BIM-GNI scene-graph accuracy report.

Ramblr (a partner company) ran an IFC-to-scene-graph pipeline over the 208
models in dataset/2025_BIMfundamentals and handed us both the graphs
(dataset/BIM-GNI-Ramblr/) and a written accuracy report, proposing their
output as pseudo ground truth for connectivity evaluation. Same discipline as
main/verify_gt.py: a claim is only as good as an independent recomputation of
it, written from scratch against the raw IFC + scene-graph JSON pairs, not
against the vendor's own checking code (which we don't have).

Ground truth for the one relation type these graphs assert -- a wall
containing a door or window -- is read directly from the source IFC via
IfcRelVoidsElement + IfcRelFillsElement, chained through the shared
IfcOpeningElement. This does NOT need Ramblr's tool at all: it is a native
IFC relation, already complete in this corpus (confirmed: this corpus has
IfcSpace and void/fill relations throughout, unlike our existing 25-model
test_set/, which is the same audit's zero-IfcRelSpaceBoundary corpus for a
*different* relation type -- see docs/DESIGN_v3.md §1).

    python main/verify_ramblr_report.py
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

import ifcopenshell

SG_DIR = "dataset/BIM-GNI-Ramblr/2025_BIMfundamentals_scene_graphs_collected"
IFC_DIR = "dataset/2025_BIMfundamentals"


def real_wall_of_opening(f):
    """opening-filling element STEP id -> its host wall STEP id, per IFC.

    IfcRelVoidsElement(wall, opening) + IfcRelFillsElement(opening, door/window)
    chained through the shared IfcOpeningElement.
    """
    void_of = {}
    for v in f.by_type("IfcRelVoidsElement"):
        if v.RelatingBuildingElement and v.RelatedOpeningElement:
            void_of[v.RelatedOpeningElement.id()] = v.RelatingBuildingElement.id()
    pairs = {}
    for r in f.by_type("IfcRelFillsElement"):
        if r.RelatingOpeningElement and r.RelatedBuildingElement:
            op = r.RelatingOpeningElement.id()
            if op in void_of:
                pairs[r.RelatedBuildingElement.id()] = void_of[op]
    return pairs


def collect_unique_nodes(node, by_id):
    """The same element node (and its `relations`) is repeated verbatim once
    per room it is assigned to (confirmed: identical id, identical relations,
    appearing 2-26 times in the tree for a single physical wall) -- a design
    choice for representing multi-room membership in a tree, not distinct
    elements. Counting every occurrence, rather than every distinct node id,
    inflates both element and relation counts by the same multiplicity.
    """
    if node.get("type") == "building_element":
        by_id[node["id"]] = node
    for c in node.get("children", []):
        collect_unique_nodes(c, by_id)


def walk_sg(node, id_to_iid, id_to_class, id_to_name, relations):
    by_id = {}
    collect_unique_nodes(node, by_id)
    seen_rel = set()
    for nid, n in by_id.items():
        id_to_iid[nid] = n.get("instance_id")
        id_to_class[nid] = n.get("class_name")
        id_to_name[nid] = n.get("name")
        for r in n.get("relations", []):
            if r.get("type") != "contains":
                continue
            # Confirmed: the identical relation object is stored verbatim on
            # BOTH the wall's and the door/window's own `relations` array
            # (same source/target on each), so it must be counted once per
            # (source, target) pair, not once per node it happens to be
            # attached to.
            key = (r["source"], r["target"])
            if key in seen_rel:
                continue
            seen_rel.add(key)
            relations.append(key)


def main():
    files = sorted(glob.glob(os.path.join(SG_DIR, "*.json")))
    print(f"{len(files)} scene-graph files found", file=sys.stderr)

    tot_relations = 0
    tot_confirmed = 0
    tot_unsupported = 0
    tot_extra = 0
    tot_mistake = 0
    tot_unjudgeable = 0
    tot_real_pairs = 0

    tot_elements_ifc = 0
    tot_elements_sg = 0
    per_class_missing = Counter()
    per_class_total = Counter()

    broken_refs = 0
    tot_sg_element_nodes = 0

    per_file_rows = []
    worst = []

    for i, sg_path in enumerate(files):
        name = os.path.splitext(os.path.basename(sg_path))[0]
        ifc_path = os.path.join(IFC_DIR, name + ".ifc")
        if not os.path.exists(ifc_path):
            print(f"  !! no IFC for {name}", file=sys.stderr)
            continue

        f = ifcopenshell.open(ifc_path)
        real_pairs = real_wall_of_opening(f)  # opening_id -> wall_id
        real_openings = set(real_pairs)

        sg = json.load(open(sg_path))
        id_to_iid, id_to_class, id_to_name, sg_rel = {}, {}, {}, []
        walk_sg(sg, id_to_iid, id_to_class, id_to_name, sg_rel)
        tot_sg_element_nodes += len(id_to_iid)

        # node-reference integrity: every instance_id must resolve in the IFC
        for iid in id_to_iid.values():
            if iid is None:
                continue
            try:
                f.by_id(iid)
            except Exception:
                broken_refs += 1

        # relation-level check
        # group SG relations by target (opening) to detect "extra wall" cases
        by_target = defaultdict(list)
        for src, tgt in sg_rel:
            by_target[tgt].append(src)

        file_conf = file_unsupported = 0
        for tgt, sources in by_target.items():
            opening_iid = id_to_iid.get(tgt)
            for src in sources:
                wall_iid = id_to_iid.get(src)
                tot_relations += 1
                if opening_iid not in real_openings:
                    tot_unjudgeable += 1
                    continue
                true_wall = real_pairs[opening_iid]
                if wall_iid == true_wall:
                    tot_confirmed += 1
                    file_conf += 1
                else:
                    tot_unsupported += 1
                    file_unsupported += 1
                    # is the correct wall ALSO asserted for this opening?
                    correct_also_present = any(
                        id_to_iid.get(s) == true_wall for s in sources)
                    if correct_also_present:
                        tot_extra += 1
                    else:
                        tot_mistake += 1

        tot_real_pairs += len(real_pairs)

        n = file_conf + file_unsupported
        prec = file_conf / n if n else None
        rec = file_conf / len(real_pairs) if real_pairs else None
        per_file_rows.append((name, n, len(real_pairs), prec, rec))

        # element coverage
        try:
            all_products = f.by_type("IfcBuildingElement")
        except Exception:
            all_products = []
        tot_elements_ifc += len(all_products)
        sg_iids_present = set(v for v in id_to_iid.values() if v is not None)
        for p in all_products:
            cls = p.is_a()
            per_class_total[cls] += 1
            if p.id() not in sg_iids_present:
                per_class_missing[cls] += 1
        tot_elements_sg += sum(1 for p in all_products if p.id() in sg_iids_present)

        if (i + 1) % 40 == 0:
            print(f"  ...{i+1}/{len(files)}", file=sys.stderr)

    print("\n=== RELATIONS ===")
    print(f"total SG relations checked: {tot_relations}")
    print(f"confirmed (TP):             {tot_confirmed}  ({tot_confirmed/tot_relations:.3f} of checked)")
    print(f"unsupported:                {tot_unsupported}")
    print(f"  of which extra-wall (correct sibling exists): {tot_extra}")
    print(f"  of which true mistake:                        {tot_mistake}")
    print(f"unjudgeable (no model-stated host):              {tot_unjudgeable}")
    print(f"precision (excl. unjudgeable): {tot_confirmed/(tot_confirmed+tot_unsupported):.3f}")
    print(f"\ntotal real wall-opening pairs in IFC (all 208): {tot_real_pairs}")
    print(f"recall: {tot_confirmed/tot_real_pairs:.4f}  (confirmed / total real pairs)")

    print("\n=== NODE REFERENCE INTEGRITY ===")
    print(f"scene-graph building_element nodes (with dup room-membership counted once per node): {tot_sg_element_nodes}")
    print(f"broken instance_id references: {broken_refs}")

    print("\n=== ELEMENT COVERAGE (IfcBuildingElement subtypes) ===")
    print(f"total IfcBuildingElement instances across corpus: {tot_elements_ifc}")
    print(f"present in scene graphs (by instance_id):         {tot_elements_sg}")
    print(f"coverage: {tot_elements_sg/tot_elements_ifc:.3f}")
    print(f"\nper-class (top 12 by total count):")
    for cls, tot in per_class_total.most_common(12):
        miss = per_class_missing[cls]
        print(f"  {cls:<22} missing {miss:>6}/{tot:<6} ({miss/tot:.1%})")

    print("\n=== PER-FILE SELECTION ===")
    zero_relations = []
    prec_1000 = []
    full_coverage = []
    for name, n, real_n, prec, rec in per_file_rows:
        if real_n == 0:
            continue
        if n == 0:
            zero_relations.append(name)
            continue
        if prec == 1.0:
            prec_1000.append((name, rec))
        if prec == 1.0 and rec == 1.0:
            full_coverage.append(name)
    print(f"files with real_pairs>0 but zero SG relations: {len(zero_relations)}")
    print(f"files with real_pairs>0, SG relations present, precision==1.0: {len(prec_1000)}")
    print(f"files with precision==1.0 AND recall==1.0 (clears every threshold): {full_coverage}")
    print("precision==1.0 files, sorted by recall (usable-with-gaps candidates):")
    for name, rec in sorted(prec_1000, key=lambda x: -(x[1] or 0)):
        print(f"  {name:<14} recall={rec:.3f}")

    out_path = "results/ramblr_verify.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump({
        "per_file": [
            {"model": n, "sg_relations": r, "real_pairs": rp, "precision": p, "recall": rc}
            for n, r, rp, p, rc in per_file_rows
        ],
        "totals": {
            "relations_checked": tot_relations, "confirmed": tot_confirmed,
            "unsupported": tot_unsupported, "extra": tot_extra, "mistake": tot_mistake,
            "unjudgeable": tot_unjudgeable, "real_pairs": tot_real_pairs,
            "elements_ifc": tot_elements_ifc, "elements_sg": tot_elements_sg,
            "broken_refs": broken_refs, "sg_element_nodes": tot_sg_element_nodes,
        }
    }, open(out_path, "w"), indent=2)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
