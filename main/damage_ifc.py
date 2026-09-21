"""Apply controlled, measured damage to a clean IFC export.

The severity axis needs more than one level to be an axis. Note that stripping
`IfcRelSpaceBoundary` -- the defect that is 100% prevalent in real files -- is a
*no-op* on generated ones, because the IfcOpenShell authoring API never emits
space boundaries in the first place. The generated corpus therefore starts at
the damage level that real Revit exports already arrive at, and the levels below
degrade it further.

Each level reproduces a pathology measured in the 25-model real corpus, at its
measured prevalence, rather than an invented one:

  L0  pristine            (not observed in the wild)
  L1  no space boundaries (100% of real models)      <- generated files start here
  L2  IfcSpace missing on a fraction of storeys (49% of real storeys)
  L3  spaces orphaned from their storey (model_2)
  L4  no spatial containment at all + undeclared units in cm (model_2)

Ground truth is never touched: the connectivity that the generator chose remains
the answer regardless of how badly the file describing it is mangled.

    python main/damage_ifc.py --src dataset/synthetic_data/synthetic --out dataset/synthetic_data/synthetic_L2 --level 2
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shutil

import ifcopenshell


def _remove_all(f, ifc_class: str) -> int:
    ents = list(f.by_type(ifc_class))
    for e in ents:
        try:
            f.remove(e)
        except Exception:
            pass
    return len(ents)


def strip_space_boundaries(f) -> dict:
    """L1: the universal real-world defect. A no-op on generated files."""
    n = 0
    for cls in ("IfcRelSpaceBoundary", "IfcRelSpaceBoundary1stLevel",
                "IfcRelSpaceBoundary2ndLevel"):
        try:
            n += _remove_all(f, cls)
        except Exception:
            pass
    return {"space_boundaries_removed": n}


def remove_spaces_on_storeys(f, fraction: float, rng) -> dict:
    """L2: delete IfcSpace on a fraction of storeys, forcing room recovery."""
    storeys = f.by_type("IfcBuildingStorey")
    if not storeys:
        return {"storeys_cleared": 0, "spaces_removed": 0}
    k = max(1, int(round(len(storeys) * fraction)))
    victims = rng.sample(list(storeys), min(k, len(storeys)))

    removed = 0
    for st in victims:
        spaces = []
        for rel in list(getattr(st, "IsDecomposedBy", []) or []):
            spaces.extend([o for o in rel.RelatedObjects if o.is_a("IfcSpace")])
        for sp in spaces:
            # Drop the aggregation link first, then the space itself, so no
            # relationship is left pointing at a deleted entity.
            for rel in list(f.get_inverse(sp)):
                if rel.is_a("IfcRelAggregates"):
                    rest = [o for o in rel.RelatedObjects if o != sp]
                    if rest:
                        rel.RelatedObjects = rest
                    else:
                        try:
                            f.remove(rel)
                        except Exception:
                            pass
            try:
                f.remove(sp)
                removed += 1
            except Exception:
                pass
    return {"storeys_cleared": len(victims), "spaces_removed": removed}


def orphan_spaces(f) -> dict:
    """L3: sever spaces from their storey, as in model_2."""
    n = 0
    for rel in list(f.by_type("IfcRelAggregates")):
        try:
            if rel.RelatingObject and rel.RelatingObject.is_a("IfcBuildingStorey") \
                    and any(o.is_a("IfcSpace") for o in rel.RelatedObjects):
                keep = [o for o in rel.RelatedObjects if not o.is_a("IfcSpace")]
                if keep:
                    rel.RelatedObjects = keep
                else:
                    f.remove(rel)
                n += 1
        except Exception:
            pass
    return {"aggregations_severed": n}


def drop_containment(f) -> dict:
    """L4a: remove element-to-storey containment entirely."""
    return {"containment_removed": _remove_all(f, "IfcRelContainedInSpatialStructure")}


def corrupt_units(f) -> dict:
    """L4b: undeclared units with centimetre coordinates, as in model_2.

    The project's IfcUnitAssignment is detached and every length rescaled, so a
    parser that trusts the declared unit produces a building 100x too large.
    """
    scaled_pts = 0
    for p in f.by_type("IfcCartesianPoint"):
        try:
            p.Coordinates = tuple(c * 100.0 for c in p.Coordinates)
            scaled_pts += 1
        except Exception:
            pass
    scaled_depth = 0
    for s in f.by_type("IfcExtrudedAreaSolid"):
        try:
            s.Depth = float(s.Depth) * 100.0
            scaled_depth += 1
        except Exception:
            pass
    for st in f.by_type("IfcBuildingStorey"):
        try:
            if st.Elevation is not None:
                st.Elevation = float(st.Elevation) * 100.0
        except Exception:
            pass
    detached = 0
    for proj in f.by_type("IfcProject"):
        if proj.UnitsInContext is not None:
            proj.UnitsInContext = None
            detached += 1
    return {"points_scaled": scaled_pts, "solids_scaled": scaled_depth,
            "unit_assignment_detached": detached}


LEVELS = {
    0: [],
    1: ["boundaries"],
    2: ["boundaries", "spaces"],
    3: ["boundaries", "spaces", "orphan"],
    4: ["boundaries", "spaces", "orphan", "containment", "units"],
}


def damage(src: str, dst: str, level: int, fraction: float, seed: int) -> dict:
    f = ifcopenshell.open(src)
    rng = random.Random(seed)
    log = {"level": level}
    for op in LEVELS[level]:
        if op == "boundaries":
            log.update(strip_space_boundaries(f))
        elif op == "spaces":
            log.update(remove_spaces_on_storeys(f, fraction, rng))
        elif op == "orphan":
            log.update(orphan_spaces(f))
        elif op == "containment":
            log.update(drop_containment(f))
        elif op == "units":
            log.update(corrupt_units(f))
    f.write(dst)
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="dataset/synthetic_data/synthetic")
    ap.add_argument("--out", required=True)
    ap.add_argument("--level", type=int, required=True, choices=sorted(LEVELS))
    ap.add_argument("--fraction", type=float, default=0.49,
                    help="storeys to strip of IfcSpace (real-corpus rate)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    agg = {}
    n = 0
    for ifc in sorted(glob.glob(os.path.join(args.src, "*.ifc"))):
        name = os.path.basename(ifc)
        log = damage(ifc, os.path.join(args.out, name), args.level,
                     args.fraction, args.seed + n)
        # Ground truth is invariant under damage: the building did not change,
        # only the file's description of it.
        gt = ifc.replace(".ifc", ".gt.json")
        if os.path.exists(gt):
            shutil.copyfile(gt, os.path.join(args.out, os.path.basename(gt)))
        for k, v in log.items():
            if k != "level":
                agg[k] = agg.get(k, 0) + v
        n += 1
    print(f"L{args.level}: damaged {n} models -> {args.out}")
    for k, v in sorted(agg.items()):
        print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
