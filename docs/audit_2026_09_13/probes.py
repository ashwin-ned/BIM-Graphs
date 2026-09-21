"""Small audit reproductions. Reads existing code; writes only to a temp folder.

Run with the project's bim-sg Python. JSON reports observed behavior, not
regression-test success. These probes intentionally exercise current defects.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "main"), str(ROOT / "annotator")]

import networkx as nx
import numpy as np
from shapely.affinity import rotate
from shapely.geometry import box

from bimsg import graph as G, navigation as N, parser as P, qa as Q, semantics as S, topology as T
from compose import compose, connectivity_gt
import eval_connectivity as EC
import evaluate as EA
import build_site
import derive_real_gt as DR
import baselines


def space(gid, poly, storey="s0", label="room", z=0):
    return P.Space(gid, gid, label, z, z + 3, poly, storey)


def product(gid, poly, kind="IfcDoor", z0=0, z1=2.1):
    c = poly.centroid
    return P.Product(gid, kind, gid, "", z0, z1, (c.x, c.y, (z0 + z1) / 2),
                     poly, np.eye(4), "s0")


def model(spaces, products, storeys=None):
    return P.ParsedModel("probe.ifc", "IFC4", "probe", "probe", 1,
                         storeys or [P.Storey("s0", "Ground", 0)], spaces, products)


def main():
    out = {}
    a = space("a", box(-3, -3, 3, -.1))
    b = space("b", box(-3, .1, 3, 3))
    wide = product("wide", box(-2, -.1, -.8, .1))
    narrow = product("narrow", box(1, -.1, 1.4, .1))
    m = model([a, b], [wide, narrow])
    g = G.build(m, infer_missing_rooms=False)
    out["parallel_doors"] = {
        "resolved_by_topology": len(T.door_connections(m, [a, b])),
        "stored_a_to_b": list(g.get_edge_data("a", "b").values()),
        "wheeled_route_exists": N.plan(g, "a", "b", profile="wheeled") is not None,
    }
    reverse = G.build(model([a, b], [narrow, wide]), infer_missing_rooms=False)
    out["parallel_doors"]["reversing_input_makes_route_exist"] = N.plan(reverse, "a", "b", profile="wheeled") is not None

    rotated = product("rotated", rotate(box(-.5, -.025, .5, .025), 45))
    out["rotation_changes_width"] = {"physical_long_side_m": 1.0,
                                     "reported_m": T._door_width(rotated)}

    narrow_room = space("narrow_room", box(-3, .1, 3, .6))
    remote = space("remote", box(-3, .8, 3, 3))
    barrier = product("solid_wall", box(-3, .6, 3, .8), "IfcWall", z1=3)
    m = model([a, narrow_room, remote], [wide, barrier])
    out["door_probe_skips_near_room"] = T.door_connections(m, m.spaces)

    stacked = [space("lo", box(0, 0, 4, 4), label="Stair"),
               space("hi", box(0, 0, 4, 4), storey="s1", label="Stair", z=3.2)]
    m = model(stacked, [], [P.Storey("s0", "Ground", 0), P.Storey("s1", "Upper", 3.2)])
    sg = G.build(m, infer_missing_rooms=False)
    out["stacked_labels_without_stair_solid"] = {
        "connections": T.vertical_connections(m, stacked),
        "legged_plan": N.plan(sg, "lo", "hi", profile="legged"),
    }
    sg.nodes["lo"]["label"] = "room"
    out["masked_stair_label_retains_label_derived_feature"] = S.room_features(sg, "lo")["n_vertical"]
    out["missing_width_passable"] = N.edge_passable({"relation": "connected_by_door", "width": None}, N.PROFILES["wheeled"])

    sg.nodes["lo"]["elevation"] = 0.0
    out["zero_elevation_is_ground_floor"] = S.room_features(sg, "lo")["is_ground_floor"]
    out["canonical_edge_cases"] = {x: S.canonical(x) for x in ["office", "meeting", "storage", "outdoor", "Open Floor Plate"]}

    qg = nx.MultiDiGraph()
    qg.add_node("k1", layer="space", label="Kitchen 1")
    qg.add_node("k2", layer="space", label="Kitchen 2")
    out["specific_room_query"] = {"query": "Kitchen 2", "candidates": Q.BuildingIndex(qg).find_rooms("Kitchen 2")}

    plan = {"model": "probe", "storeys": [{"gid": "s0", "name": "Ground", "elevation": 0,
            "rooms": [{"id": r, "label": "room", "source": "ifc", "area": 9, "centroid": [i*3, 0]}
                      for i, r in enumerate(["a", "b"])],
            "edges": [{"a": "a", "b": "b", "type": "connected_by_door"}]}], "vertical": []}
    anno = {"model": "probe", "annotator": "audit", "rooms": {"a": {"verdict": "unsure"}, "b": {"verdict": "real"}},
            "edges": {"a|b": {"verdict": "passable"}}}
    composed = compose(plan, anno)
    out["unsure_room_in_ground_truth"] = {"complete": composed["complete"], "nodes": composed["nodes"], "edges": composed["edges"], "held_out": composed["held_out"]}
    anno["rooms"]["a"]["verdict"] = "real"

    with tempfile.TemporaryDirectory(prefix="bimsg-audit-") as tmp:
        tmp = Path(tmp)
        (tmp / "probe.plan.json").write_text(json.dumps(plan))
        (tmp / "anno.json").write_text(json.dumps(anno))
        EA.PLAN_DIR = str(tmp)
        out["current_verdicts_legacy_scorer"] = EA.score_one(str(tmp / "anno.json"))

        G.save(g, str(tmp / "pred.graph.json"))
        gt = connectivity_gt(compose(plan, anno))
        (tmp / "human.gt.json").write_text(json.dumps(gt))
        try:
            out["human_gt_synthetic_scorer"] = EC.score_one(str(tmp / "human.gt.json"), str(tmp / "pred.graph.json"))
        except Exception as exc:
            out["human_gt_synthetic_scorer"] = f"{type(exc).__name__}: {exc}"

        parallel = nx.MultiDiGraph()
        parallel.add_node("a", layer="space")
        parallel.add_node("b", layer="space")
        for door in ("d1", "d2"):
            parallel.add_edge("a", "b", key=door, relation="connected_by_door", door=door)
        G.save(parallel, str(tmp / "parallel.json"))
        out["roundtrip_parallel_edges"] = {"before": parallel.number_of_edges(), "after": G.load(str(tmp / "parallel.json")).number_of_edges()}

        plans_dir = tmp / "plans"
        plans_dir.mkdir()
        (plans_dir / "probe.plan.json").write_text(json.dumps(plan))
        (plans_dir / "probe.ifc").write_text("audit placeholder: copy test only")
        build_site.build(str(tmp / "site"), quiet=True, with_ifc=True, plans_dir=str(plans_dir))
        out["static_build_drops_available_ifc"] = {
            "source_exists": (plans_dir / "probe.ifc").exists(),
            "copied": (tmp / "site/data/probe.ifc").exists(),
            "manifest": json.loads((tmp / "site/manifest.json").read_text())["models"],
        }

        import ifcopenshell
        from ifcopenshell.api import run
        from generate_building import _ctx, _body_ctx, _box
        f = ifcopenshell.file(schema="IFC4")
        run("root.create_entity", f, ifc_class="IfcProject", name="audit")
        run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
        body = _body_ctx(f, _ctx(f))
        op = run("root.create_entity", f, ifc_class="IfcOpeningElement", name="test opening")
        _box(f, body, op, 0., 0., 0., 1., .8, 2.1)
        metre_path = tmp / "metres.ifc"
        f.write(str(metre_path))
        for pt in f.by_type("IfcCartesianPoint"):
            pt.Coordinates = tuple(x * 1000 for x in pt.Coordinates)
        for solid in f.by_type("IfcExtrudedAreaSolid"):
            solid.Depth *= 1000
        for unit in f.by_type("IfcSIUnit"):
            if unit.UnitType == "LENGTHUNIT":
                unit.Prefix = "MILLI"
        mm_path = tmp / "mm.ifc"
        f.write(str(mm_path))
        out["reference_double_scales_openings"] = {
            "same_geometry_metres_voids": len(DR.doorway_voids(str(metre_path), 1.0)),
            "same_geometry_mm_voids": len(DR.doorway_voids(str(mm_path), .001)),
            "mm_without_second_scale_voids": len(DR.doorway_voids(str(mm_path), 1.0)),
            "normal_door_footprint_filtered_by_room_area_threshold": DR._clean(box(0, 0, .9, .2)) is None,
        }

        f = ifcopenshell.file(schema="IFC4")
        wall = run("root.create_entity", f, ifc_class="IfcWall", name="solid wall")
        raw_spaces = [run("root.create_entity", f, ifc_class="IfcSpace", name=x) for x in ["a", "b"]]
        for sp in raw_spaces:
            f.create_entity("IfcRelSpaceBoundary", GlobalId=ifcopenshell.guid.new(), RelatingSpace=sp,
                            RelatedBuildingElement=wall, PhysicalOrVirtualBoundary="PHYSICAL", InternalOrExternalBoundary="INTERNAL")
        path = tmp / "boundary.ifc"
        f.write(str(path))
        sps = [space(sp.GlobalId, box(i*3, 0, i*3+2, 2)) for i, sp in enumerate(raw_spaces)]
        wrong = baselines.m_schema(model(sps, []), sps, str(path))
        out["schema_baseline_treats_wall_as_door"] = [dict(d) for _, _, d in wrong.edges(data=True) if d.get("relation") == "connected_by_door"]

    import trimesh
    from render_floorplan import _section_segments
    from shapely.geometry import LineString, Point
    from shapely.ops import polygonize, unary_union
    meshes = []
    for extents, center in [((1.5, .2, 3), (-1.25, 0, 1.5)),
                            ((1.5, .2, 3), (1.25, 0, 1.5)),
                            ((1, .2, .9), (0, 0, 2.55))]:
        part = trimesh.creation.box(extents=extents)
        part.apply_translation(center)
        meshes.append(part)
    mesh = trimesh.util.concatenate(meshes)
    projection = P._footprint_from_mesh(mesh.vertices, mesh.faces, accurate=False)
    sections = list(polygonize(unary_union([LineString(s) for s in _section_segments(mesh.vertices, mesh.faces, 1.2)])))
    out["lintel_projection_seals_opening"] = {
        "doorway_center_inside_projection": projection.contains(Point(0, 0)),
        "doorway_center_inside_true_section": any(p.contains(Point(0, 0)) for p in sections),
        "section_polygons": len(sections),
    }

    scored = nx.MultiDiGraph()
    scored.add_node("a", layer="space", room_number="A", centroid=[1, 1])
    scored.add_node("b", layer="space", room_number="B", centroid=[4, 1])
    scored.add_node("fake", layer="space", room_number="fake", centroid=[100, 100])
    scored.add_edge("a", "b", relation="connected_by_door")
    scored.add_edge("a", "fake", relation="connected_by_door")
    known = [{"rid": r, "x0": x, "y0": 0, "x1": x+2, "y1": 2, "storey": 0} for r, x in [("A", 0), ("B", 3)]]
    pairs = EC.graph_edges(scored, known)
    out["unmatched_false_edge_not_scored"] = {"input_edges": scored.number_of_edges(),
        "scored_edges": sum(k != "_unmapped" for k in pairs), "unmapped_rooms": pairs["_unmapped"]}

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
