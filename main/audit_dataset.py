"""Schema-level audit of the IFC test set.

Quantifies how much of the topology a BIM2Graph pipeline needs is actually
present in the source files, versus how much has to be recovered geometrically.
Cheap: no tessellation, only entity/relationship inspection.
"""

import sys
import glob
import json
import os

import ifcopenshell

# Relationships a naive spatial-tree traversal would rely on.
TOPOLOGY_RELS = [
    "IfcRelSpaceBoundary",
    "IfcRelSpaceBoundary1stLevel",
    "IfcRelSpaceBoundary2ndLevel",
    "IfcRelConnectsPathElements",
    "IfcRelReferencedInSpatialStructure",
]

ELEMENT_TYPES = [
    "IfcWall", "IfcSlab", "IfcDoor", "IfcWindow", "IfcStair", "IfcStairFlight",
    "IfcColumn", "IfcBeam", "IfcRailing", "IfcFurniture", "IfcCurtainWall",
    "IfcRoof", "IfcCovering", "IfcBuildingElementProxy",
]


def audit(path):
    f = ifcopenshell.open(path)
    rec = {
        "model": os.path.basename(path),
        "schema": f.schema,
        "size_mb": round(os.path.getsize(path) / 1e6, 1),
        "total_entities": sum(1 for _ in f),
    }

    storeys = f.by_type("IfcBuildingStorey")
    spaces = f.by_type("IfcSpace")
    rec["storeys"] = len(storeys)
    rec["spaces"] = len(spaces)

    # How many storeys actually carry any IfcSpace?
    storeys_with_space = 0
    for st in storeys:
        n = 0
        for rel in getattr(st, "IsDecomposedBy", []) or []:
            n += sum(1 for o in rel.RelatedObjects if o.is_a("IfcSpace"))
        if n:
            storeys_with_space += 1
    rec["storeys_with_spaces"] = storeys_with_space
    rec["storey_space_coverage"] = (
        round(storeys_with_space / len(storeys), 2) if storeys else 0.0
    )

    # Spaces carrying a usable functional label.
    labelled = 0
    generic = {"room name", "room", "", "none"}
    for s in spaces:
        ln = (s.LongName or s.Name or "").strip().lower()
        if ln and ln not in generic:
            labelled += 1
    rec["spaces_with_semantic_label"] = labelled

    for r in TOPOLOGY_RELS:
        try:
            rec[r] = len(f.by_type(r))
        except Exception:
            rec[r] = 0

    for e in ELEMENT_TYPES:
        try:
            rec[e] = len(f.by_type(e))
        except Exception:
            rec[e] = 0

    # Elements placed in a storey but never assigned to a space.
    contained = 0
    for rel in f.by_type("IfcRelContainedInSpatialStructure"):
        contained += len(rel.RelatedElements)
    rec["elements_contained_in_storey"] = contained

    in_space = 0
    for s in spaces:
        for rel in getattr(s, "ContainsElements", []) or []:
            in_space += len(rel.RelatedElements)
    rec["elements_contained_in_space"] = in_space

    return rec


def main():
    paths = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1
                             else "dataset/test_set/*.ifc"))
    rows = [audit(p) for p in paths]

    keys = ["model", "schema", "size_mb", "storeys", "spaces",
            "storeys_with_spaces", "spaces_with_semantic_label",
            "IfcRelSpaceBoundary", "IfcRelSpaceBoundary2ndLevel",
            "IfcRelConnectsPathElements",
            "elements_contained_in_storey", "elements_contained_in_space",
            "IfcDoor", "IfcWall", "IfcStair"]
    print("\t".join(keys))
    for r in rows:
        print("\t".join(str(r.get(k, "")) for k in keys))

    print("\n--- AGGREGATE ---")
    n = len(rows)
    print(f"models: {n}")
    print(f"total storeys: {sum(r['storeys'] for r in rows)}")
    print(f"total spaces: {sum(r['spaces'] for r in rows)}")
    print(f"storeys with >=1 space: {sum(r['storeys_with_spaces'] for r in rows)}")
    print(f"spaces w/ semantic label: {sum(r['spaces_with_semantic_label'] for r in rows)}")
    print(f"space boundaries (all levels): "
          f"{sum(r['IfcRelSpaceBoundary'] + r['IfcRelSpaceBoundary2ndLevel'] for r in rows)}")
    print(f"elements assigned to a space: {sum(r['elements_contained_in_space'] for r in rows)}")
    print(f"elements assigned to a storey: {sum(r['elements_contained_in_storey'] for r in rows)}")

    os.makedirs("results", exist_ok=True)
    with open("results/dataset_audit.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    print("\nwrote results/dataset_audit.json")


if __name__ == "__main__":
    main()
