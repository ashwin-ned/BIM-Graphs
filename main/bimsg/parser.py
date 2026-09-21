"""Stage 1 -- deterministic parsing of IFC into geometry-carrying records.

Reads the spatial decomposition tree where it exists and tessellates every
physical product once, in world coordinates, normalised to metres. Nothing here
infers anything: this layer reports only what the file actually states, so that
later stages can be explicit about what they had to recover.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.unit
import ifcopenshell.util.placement
from shapely import affinity
from shapely.geometry import Polygon, MultiPoint
from shapely.ops import unary_union

log = logging.getLogger(__name__)

# Products that physically obstruct a robot and therefore bound rooms.
OCCLUDING_TYPES = {
    "IfcWall", "IfcWallStandardCase", "IfcColumn", "IfcCurtainWall",
    "IfcPlate", "IfcMember", "IfcRailing", "IfcStair", "IfcStairFlight",
    "IfcSlab", "IfcRoof", "IfcBeam",
}

# Products that are navigation-relevant transitions between spaces.
TRANSITION_TYPES = {"IfcDoor", "IfcStair", "IfcStairFlight", "IfcTransportElement"}

# Products treated as furniture/clutter: they occupy space but never bound it.
CLUTTER_TYPES = {"IfcFurniture", "IfcFurnishingElement", "IfcSanitaryTerminal"}

# Never worth a node of their own -- thousands of curtain-wall mullions etc.
SKIP_TYPES = {"IfcOpeningElement", "IfcSpace", "IfcAnnotation", "IfcGrid"}


@dataclass
class Product:
    """A tessellated physical element in world coordinates (metres)."""

    gid: str
    ifc_type: str
    name: str
    long_name: str
    zmin: float
    zmax: float
    centroid: tuple            # (x, y, z)
    footprint: Optional[Polygon]   # XY projection
    matrix: Optional[np.ndarray] = None   # 4x4 placement, for door normals
    storey_gid: Optional[str] = None
    space_gid: Optional[str] = None       # only if the file states it
    properties: dict = field(default_factory=dict)
    # Retained only when parse(with_meshes=True); the renderer needs them and
    # nothing else does, so by default they are dropped to keep memory sane.
    verts: Optional[np.ndarray] = None
    faces: Optional[np.ndarray] = None
    colour: Optional[tuple] = None

    @property
    def is_occluding(self) -> bool:
        return self.ifc_type in OCCLUDING_TYPES

    @property
    def is_transition(self) -> bool:
        return self.ifc_type in TRANSITION_TYPES


@dataclass
class Space:
    """An IfcSpace as stated by the file (not an inferred room)."""

    gid: str
    name: str
    long_name: str
    zmin: float
    zmax: float
    footprint: Optional[Polygon]
    storey_gid: Optional[str] = None
    source: str = "ifc"        # "ifc" | "inferred"
    # For recovered rooms: the free-space connected component they came from.
    # Two rooms from *different* components are separated by a barrier, by
    # construction -- the flood fill would have merged them otherwise.
    component: Optional[str] = None


@dataclass
class Storey:
    gid: str
    name: str
    elevation: float
    space_gids: list = field(default_factory=list)


@dataclass
class ParsedModel:
    path: str
    schema: str
    project_name: str
    building_name: str
    unit_scale: float
    storeys: list = field(default_factory=list)
    spaces: list = field(default_factory=list)
    products: list = field(default_factory=list)
    # The two factors behind `unit_scale`, kept apart. `unit_scale` is the
    # declared unit times the empirical repair; tessellated geometry already
    # arrives in metres and needs only the repair, while raw schema attributes
    # need both. Anything that cannot tell them apart scales twice. Appended
    # rather than inserted: this record is also built positionally.
    scale_correction: float = 1.0
    declared_unit_scale: float = 1.0

    def storey_by_gid(self, gid):
        return next((s for s in self.storeys if s.gid == gid), None)


def _footprint_from_mesh(verts: np.ndarray, faces: np.ndarray,
                         accurate: bool) -> Optional[Polygon]:
    """XY footprint of a mesh.

    `accurate` unions the projected triangles, preserving L-shaped rooms.
    Otherwise the 2D convex hull is used, which is exact for the box-like
    elements that dominate a building model and far cheaper on dense meshes.
    """
    if len(verts) < 3:
        return None

    if not accurate:
        try:
            hull = MultiPoint(verts[:, :2]).convex_hull
            return hull if isinstance(hull, Polygon) and hull.area > 1e-6 else None
        except Exception:
            return None

    tris = []
    for a, b, c in faces:
        p = verts[[a, b, c]][:, :2]
        # Skip near-degenerate (vertical wall) triangles.
        area = abs(np.cross(p[1] - p[0], p[2] - p[0])) / 2.0
        if area > 1e-7:
            tris.append(Polygon(p))
    if not tris:
        return None
    try:
        u = unary_union(tris).buffer(0)
    except Exception:
        return None
    if u.is_empty:
        return None
    if u.geom_type == "MultiPolygon":
        u = max(u.geoms, key=lambda g: g.area)
    return u if isinstance(u, Polygon) and u.area > 1e-6 else None


def detect_scale_correction(model) -> float:
    """Empirically recover the length unit when the file misdeclares it.

    model_2 of the test set carries no IfcUnitAssignment on its IfcProject at
    all, so IfcOpenShell defaults to metres while the coordinates are actually
    centimetres -- every room comes out 10,000x too large. Declared units cannot
    be trusted, but physics can: a storey is between 2 and 6 metres tall, so the
    observed floor-to-floor distance identifies the true unit to within a
    decimal power.
    """
    heights = [s.zmax - s.zmin for s in model.spaces if s.zmax > s.zmin]
    if not heights:
        elevs = sorted({round(s.elevation, 4) for s in model.storeys})
        heights = [b - a for a, b in zip(elevs, elevs[1:]) if b > a]
    if not heights:
        return 1.0

    h = float(np.median(heights))
    if h <= 0:
        return 1.0
    for factor in (1.0, 0.01, 0.001, 100.0, 1000.0):
        if 2.0 <= h * factor <= 6.5:
            if factor != 1.0:
                log.warning("unit misdeclared: median storey height %.1f -> "
                            "applying x%g correction", h, factor)
            return factor
    log.warning("could not validate units (median storey height %.2f m)", h)
    return 1.0


def _apply_scale(model, k: float) -> None:
    if k == 1.0:
        return
    from shapely import affinity
    for st in model.storeys:
        st.elevation *= k
    for coll in (model.spaces, model.products):
        for o in coll:
            o.zmin *= k
            o.zmax *= k
            if o.footprint is not None:
                o.footprint = affinity.scale(o.footprint, k, k, origin=(0, 0))
            if isinstance(o, Product):
                o.centroid = tuple(c * k for c in o.centroid)
                if o.matrix is not None:
                    o.matrix = o.matrix.copy()
                    o.matrix[:3, 3] *= k
                if o.verts is not None:
                    o.verts = o.verts * k


def _placement_matrix(f, gid: str, scale: float) -> Optional[np.ndarray]:
    """World 4x4 placement of a product, in metres.

    Needed because USE_WORLD_COORDS bakes the transform into the vertices and
    then reports the shape transformation as identity.
    """
    try:
        el = f.by_guid(gid)
        if el is None or el.ObjectPlacement is None:
            return None
        m = np.array(ifcopenshell.util.placement.get_local_placement(
            el.ObjectPlacement), dtype=float)
        m[:3, 3] *= scale
        return m
    except Exception:
        return None


def _spatial_containment(f):
    """Map product GlobalId -> (storey_gid, space_gid) as stated by the file."""
    in_storey, in_space = {}, {}
    for rel in f.by_type("IfcRelContainedInSpatialStructure"):
        host = rel.RelatingStructure
        if host is None:
            continue
        for el in rel.RelatedElements:
            if host.is_a("IfcBuildingStorey"):
                in_storey[el.GlobalId] = host.GlobalId
            elif host.is_a("IfcSpace"):
                in_space[el.GlobalId] = host.GlobalId
    return in_storey, in_space


def _rep_key(el):
    """Identity of a product's shape, shared across mapped instances."""
    try:
        for rep in el.Representation.Representations:
            for item in rep.Items:
                if item.is_a("IfcMappedItem"):
                    return ("map", item.MappingSource.id())
        return ("own", el.Representation.id())
    except Exception:
        return None


def _parse_clutter(f, clutter, settings, scale, in_storey, in_space) -> list:
    """Tessellate each distinct furniture shape once, then place the copies.

    Furniture only needs a position and a rough footprint -- for room
    containment and as a navmesh obstacle -- so a per-instance mesh buys
    nothing that a transformed prototype does not already give.
    """
    if not clutter:
        return []

    proto = {}          # rep key -> (local footprint, dz, zoff)
    out = []
    for el in clutter:
        key = _rep_key(el)
        mat = _placement_matrix(f, el.GlobalId, scale)
        if key is not None and key in proto:
            fp_local, zmin_l, zmax_l = proto[key]
        else:
            try:
                sh = ifcopenshell.geom.create_shape(settings, el)
            except Exception:
                continue
            v = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3)
            if len(v) == 0:
                continue
            # World-coordinate verts: undo this instance's placement so the
            # prototype can be re-placed for every other copy.
            if mat is not None:
                inv = np.linalg.inv(mat)
                vh = np.c_[v, np.ones(len(v))] @ inv.T
                vl = vh[:, :3]
            else:
                vl = v
            fcs = np.array(sh.geometry.faces, dtype=int).reshape(-1, 3)
            fp_local = _footprint_from_mesh(vl, fcs, accurate=False)
            if fp_local is None:
                continue
            zmin_l, zmax_l = float(vl[:, 2].min()), float(vl[:, 2].max())
            if key is not None:
                proto[key] = (fp_local, zmin_l, zmax_l)

        fp, z0 = fp_local, 0.0
        if mat is not None:
            a, b = mat[0, 0], mat[0, 1]
            d, e = mat[1, 0], mat[1, 1]
            fp = affinity.affine_transform(
                fp_local, [a, b, d, e, mat[0, 3], mat[1, 3]])
            z0 = mat[2, 3]
        c = fp.centroid
        out.append(Product(
            gid=el.GlobalId, ifc_type=el.is_a(), name=el.Name or "",
            long_name="",
            zmin=zmin_l + z0, zmax=zmax_l + z0,
            centroid=(float(c.x), float(c.y), float(z0 + (zmin_l + zmax_l) / 2)),
            footprint=fp, matrix=None,
            storey_gid=in_storey.get(el.GlobalId),
            space_gid=in_space.get(el.GlobalId),
        ))

    log.info("clutter: %d instances from %d distinct shapes", len(out), len(proto))
    return out


def _shape_colour(sh):
    """Mean diffuse colour of a tessellated shape, for renderer realism."""
    try:
        mats = sh.geometry.materials
        if not mats:
            return None
        d = mats[0].diffuse
        return (float(d[0]), float(d[1]), float(d[2]))
    except Exception:
        return None


def parse(path: str, accurate_space_footprints: bool = True,
          with_meshes: bool = False) -> ParsedModel:
    f = ifcopenshell.open(path)
    scale = ifcopenshell.util.unit.calculate_unit_scale(f)

    proj = f.by_type("IfcProject")
    bldg = f.by_type("IfcBuilding")
    model = ParsedModel(
        path=path,
        schema=f.schema,
        project_name=(proj[0].Name if proj else "") or "",
        building_name=(bldg[0].Name if bldg else "") or "",
        unit_scale=scale,
    )

    # --- storeys (always present, and always ordered by elevation) ---
    for st in f.by_type("IfcBuildingStorey"):
        elev = 0.0
        try:
            elev = float(st.Elevation or 0.0) * scale
        except (TypeError, ValueError):
            pass
        model.storeys.append(Storey(gid=st.GlobalId, name=st.Name or "", elevation=elev))
    model.storeys.sort(key=lambda s: s.elevation)

    # Stated space->storey nesting via IfcRelAggregates.
    space_storey = {}
    for st in f.by_type("IfcBuildingStorey"):
        for rel in getattr(st, "IsDecomposedBy", []) or []:
            for o in rel.RelatedObjects:
                if o.is_a("IfcSpace"):
                    space_storey[o.GlobalId] = st.GlobalId

    in_storey, in_space = _spatial_containment(f)

    settings = ifcopenshell.geom.settings()
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except Exception:
        settings.set("use-world-coords", True)

    # --- spaces ---
    for sp in f.by_type("IfcSpace"):
        try:
            shape = ifcopenshell.geom.create_shape(settings, sp)
        except Exception:
            continue
        # NB: IfcOpenShell already emits metres regardless of the file's length
        # unit, so vertices must NOT be multiplied by the unit scale. Raw
        # schema attributes (Elevation, placements) still need it.
        v = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3)
        fc = np.array(shape.geometry.faces, dtype=int).reshape(-1, 3)
        if len(v) == 0:
            continue
        model.spaces.append(Space(
            gid=sp.GlobalId,
            name=sp.Name or "",
            long_name=sp.LongName or "",
            zmin=float(v[:, 2].min()),
            zmax=float(v[:, 2].max()),
            footprint=_footprint_from_mesh(v, fc, accurate_space_footprints),
            storey_gid=space_storey.get(sp.GlobalId),
            source="ifc",
        ))

    # --- physical products, via the threaded iterator ---
    #
    # Clutter is handled separately and never tessellated per instance. Revit
    # exports furniture as IfcMappedItem: 302 copies of one 14k-vertex chair in
    # model_16, each costing ~4.5 s to tessellate, which alone accounted for
    # 456 s of a 470 s parse. The geometry is identical by construction, so it
    # is tessellated once per representation and re-placed per instance.
    all_products = [e for e in f.by_type("IfcProduct")
                    if not any(e.is_a(t) for t in SKIP_TYPES)
                    and e.Representation is not None]
    clutter = [e for e in all_products if e.is_a() in CLUTTER_TYPES]
    wanted = [e for e in all_products if e.is_a() not in CLUTTER_TYPES]
    wanted_ids = {e.GlobalId for e in wanted}

    # `exclude` matters for speed, not tidiness: without it the iterator
    # tessellates clutter anyway and the prototype cache saves nothing.
    it = ifcopenshell.geom.iterator(
        settings, f, max(1, os.cpu_count() // 2),
        exclude=sorted(CLUTTER_TYPES) or None)
    ok = it.initialize()
    seen = 0
    if ok:
        while True:
            sh = it.get()
            gid = sh.guid
            if gid in wanted_ids:
                v = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3)
                if len(v):
                    fcs = np.array(sh.geometry.faces, dtype=int).reshape(-1, 3)
                    ifc_type = sh.type
                    # With USE_WORLD_COORDS the shape transform is baked into
                    # the vertices and reported as identity, so orientation
                    # (needed for door normals) is read from the placement.
                    mat = None
                    if ifc_type in TRANSITION_TYPES:
                        mat = _placement_matrix(f, gid, scale)
                    model.products.append(Product(
                        gid=gid,
                        ifc_type=ifc_type,
                        name=sh.name or "",
                        long_name="",
                        zmin=float(v[:, 2].min()),
                        zmax=float(v[:, 2].max()),
                        centroid=(float(v[:, 0].mean()), float(v[:, 1].mean()),
                                  float(v[:, 2].mean())),
                        footprint=_footprint_from_mesh(v, fcs, accurate=False),
                        matrix=mat,
                        storey_gid=in_storey.get(gid),
                        space_gid=in_space.get(gid),
                        verts=v if with_meshes else None,
                        faces=fcs if with_meshes else None,
                        colour=_shape_colour(sh) if with_meshes else None,
                    ))
                    seen += 1
            if not it.next():
                break
    model.products.extend(
        _parse_clutter(f, clutter, settings, scale, in_storey, in_space))

    k = detect_scale_correction(model)
    _apply_scale(model, k)
    model.declared_unit_scale = scale
    model.scale_correction = k
    model.unit_scale = scale * k

    log.info("%s: %d storeys, %d spaces, %d products (unit correction x%g)",
             path, len(model.storeys), len(model.spaces), seen, k)
    return model
