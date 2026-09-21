"""Stage 3 -- room recovery for storeys the IFC never described.

Only 74 of 146 storeys in the 25-model test corpus carry any IfcSpace, and two
models contain none at all, so a pipeline that trusts the spatial tree produces
an empty graph for much of a building. This module reconstructs rooms
geometrically for the remainder.

Method: take a horizontal slice through the storey at door height, rasterise
every occluding solid into an occupancy grid, and label the connected components
of free space. Because IFC models doors as solids filling their openings, closed
doors act as barriers and the components fall out as rooms directly -- the
door-mediated connections are restored later, in topology.py. This is the
occupancy-grid room segmentation of Bormann et al. applied to an exact
rasterisation rather than a noisy sensor map, so the usual failure mode of the
technique (ragged, uncertain walls) does not arise.
"""

from __future__ import annotations

import logging

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import box
from shapely.ops import unary_union

from .parser import Space

log = logging.getLogger(__name__)

RESOLUTION = 0.10          # metres per cell
MIN_ROOM_AREA = 2.0        # m^2; below this it is a cupboard or a modelling artefact
SLICE_OFFSET = 1.20        # metres above storey elevation
MAX_GRID_CELLS = 12_000_000


def _rle_polygon(mask: np.ndarray, x0: float, y0: float, res: float):
    """Polygonise a boolean mask via per-row run-length encoding.

    Unioning one box per horizontal run is dramatically cheaper than one box
    per cell, which matters because a large room is ~10^4 cells.
    """
    boxes = []
    for j in range(mask.shape[0]):
        row = mask[j]
        if not row.any():
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        for s, e in zip(idx[::2], idx[1::2]):
            boxes.append(box(x0 + s * res, y0 + j * res,
                             x0 + e * res, y0 + (j + 1) * res))
    if not boxes:
        return None
    poly = unary_union(boxes).buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    # Straighten the stair-stepped raster edges without losing room shape.
    return poly.simplify(res * 1.5, preserve_topology=True)


def infer_rooms_for_storey(products: list, elevation: float, ceiling: float,
                           storey_gid: str, resolution: float = RESOLUTION,
                           with_exterior: bool = True):
    """Return inferred Space objects for one storey."""
    z = elevation + SLICE_OFFSET
    if z >= ceiling:                       # very shallow storey (plant/roof)
        z = elevation + (ceiling - elevation) * 0.5

    # Solids that actually block movement at slice height.
    blockers = [p for p in products
                if p.is_occluding and p.footprint is not None
                and p.zmin <= z <= p.zmax]
    # Doors are barriers too: they separate rooms so components come out clean.
    blockers += [p for p in products
                 if p.ifc_type == "IfcDoor" and p.footprint is not None
                 and p.zmin <= z <= p.zmax]
    if not blockers:
        return []

    bounds = [p.footprint.bounds for p in blockers]
    x0 = min(b[0] for b in bounds) - 1.0
    y0 = min(b[1] for b in bounds) - 1.0
    x1 = max(b[2] for b in bounds) + 1.0
    y1 = max(b[3] for b in bounds) + 1.0

    nx = int(np.ceil((x1 - x0) / resolution))
    ny = int(np.ceil((y1 - y0) / resolution))
    if nx <= 2 or ny <= 2 or nx * ny > MAX_GRID_CELLS:
        log.warning("storey %s: grid %dx%d rejected", storey_gid, nx, ny)
        return []

    occ = np.zeros((ny, nx), dtype=bool)
    for p in blockers:
        _burn(occ, p.footprint, x0, y0, resolution, nx, ny)

    # Seal hairline gaps where wall solids abut but do not quite meet. Without
    # this a single 10 cm crack lets the exterior flood the whole floor plate.
    occ = ndimage.binary_closing(occ, structure=np.ones((3, 3)), iterations=2)

    # Bound the search to the storey's own floor slab. This is the strongest
    # constraint available: rooms must stand on a floor. Without it, any gap in
    # the facade (common when the envelope is curtain wall rather than IfcWall)
    # lets the border fill leak inward and swallow the entire storey -- in
    # model_11 that turned a 696 m^2 floor plate into "exterior" and left only
    # a handful of closets as rooms.
    inside = _slab_domain(products, elevation, x0, y0, resolution, nx, ny)

    free = ~occ
    if inside is not None:
        free &= inside

    labels, n = ndimage.label(free)
    if n == 0:
        return []

    if inside is not None:
        # Exterior = free space that reaches the edge of the floor plate
        # (balconies, decks, open walkways).
        rim = ndimage.binary_dilation(~inside, structure=np.ones((3, 3)))
        border = set(np.unique(labels[rim & free]))
    else:
        # No slab: fall back to the grid border as the outside.
        border = set(labels[0, :]) | set(labels[-1, :]) | \
            set(labels[:, 0]) | set(labels[:, -1])
    border.discard(0)

    cell_area = resolution ** 2
    rooms = []
    for lab in range(1, n + 1):
        if lab in border:
            continue
        mask = labels == lab
        area = mask.sum() * cell_area
        if area < MIN_ROOM_AREA:
            continue
        poly = _rle_polygon(mask, x0, y0, resolution)
        if poly is None or poly.area < MIN_ROOM_AREA:
            continue
        rooms.append(Space(
            gid=f"INF-{storey_gid}-{lab:04d}",
            name=f"room_{len(rooms) + 1}",
            long_name="",              # unknown: no semantic label available
            zmin=elevation,
            zmax=ceiling,
            footprint=poly,
            storey_gid=storey_gid,
            source="inferred",
            component=f"{storey_gid}#{lab}",
        ))

    log.info("storey %s: inferred %d rooms from %d blockers (grid %dx%d)",
             storey_gid, len(rooms), len(blockers), nx, ny)

    if with_exterior:
        ext = _exterior_circulation(labels, border, blockers, x0, y0,
                                    resolution, elevation, ceiling, storey_gid)
        if ext is not None:
            # A rim-touching region means something different depending on
            # context. Alongside enclosed rooms it is an outdoor deck or
            # courtyard (the open-corridor apartment blocks). With no enclosed
            # rooms at all it is simply an unpartitioned floor plate -- a
            # podium or open-plan level -- which is interior, not exterior.
            if not rooms:
                ext.name = "open_floor_plate"
                ext.long_name = "Open Floor Plate"
            rooms.append(ext)
    return rooms


def _largest_polygon(g):
    """Largest Polygon in any geometry, or None.

    Intersections routinely return GeometryCollections mixing polygons with
    stray lines and points; those must be discarded, not indexed into.
    """
    if g is None or g.is_empty:
        return None
    if g.geom_type == "Polygon":
        return g
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys = [p for p in g.geoms if p.geom_type == "Polygon" and not p.is_empty]
        return max(polys, key=lambda p: p.area) if polys else None
    return None


def _slab_domain(products, elevation, x0, y0, res, nx, ny):
    """Raster mask of the storey's floor plate, from its slabs.

    Returns None when the storey has no identifiable floor slab, in which case
    the caller falls back to treating the grid border as the outside.
    """
    slabs = [p for p in products
             if p.ifc_type in ("IfcSlab", "IfcRoof") and p.footprint is not None
             and abs(p.zmax - elevation) < 1.2]
    if not slabs:
        return None
    total = sum(p.footprint.area for p in slabs)
    if total < 4 * MIN_ROOM_AREA:          # a stair landing, not a floor
        return None

    inside = np.zeros((ny, nx), dtype=bool)
    for p in slabs:
        _burn(inside, p.footprint, x0, y0, res, nx, ny)
    # Grow slightly so rooms are not clipped where the slab stops at the
    # inner face of the facade.
    return ndimage.binary_dilation(inside, structure=np.ones((3, 3)), iterations=3)


def _exterior_circulation(labels, border, blockers, x0, y0, res,
                          elevation, ceiling, storey_gid):
    """The open walkway or courtyard a building wraps around.

    Several models in the test set are open-corridor apartment blocks: every
    flat's door opens onto an outdoor deck rather than an internal corridor.
    Treating that deck as "outside" leaves each flat an isolated node, which is
    wrong for navigation -- the walkway is how a robot actually gets between
    them. We recover it as the border-connected free space clipped to the
    building's own envelope, so genuine open landscape is excluded.
    """
    if not border:
        return None
    mask = np.isin(labels, list(border))
    if not mask.any():
        return None

    envelope = unary_union([p.footprint for p in blockers]).convex_hull
    poly = _rle_polygon(mask, x0, y0, res)
    if poly is None:
        return None
    try:
        poly = poly.intersection(envelope)
    except Exception:
        return None
    poly = _largest_polygon(poly)
    if poly is None or poly.area < MIN_ROOM_AREA:
        return None

    return Space(
        gid=f"EXT-{storey_gid}",
        name="exterior_circulation",
        long_name="Exterior Circulation",
        zmin=elevation,
        zmax=ceiling,
        footprint=poly,
        storey_gid=storey_gid,
        source="inferred",
        component=f"{storey_gid}#ext",
    )


def _burn(occ: np.ndarray, poly, x0: float, y0: float,
          res: float, nx: int, ny: int) -> None:
    """Rasterise a polygon into the occupancy grid.

    Scanline fill over the polygon's own bounding box; exact enough at 10 cm
    for elements that are, in practice, axis-aligned boxes.
    """
    minx, miny, maxx, maxy = poly.bounds
    i0 = max(0, int((minx - x0) / res))
    i1 = min(nx, int(np.ceil((maxx - x0) / res)))
    j0 = max(0, int((miny - y0) / res))
    j1 = min(ny, int(np.ceil((maxy - y0) / res)))
    if i1 <= i0 or j1 <= j0:
        return

    # Cheap path: a rectangle-ish footprint fills its whole bbox.
    bbox_area = (maxx - minx) * (maxy - miny)
    if bbox_area > 0 and poly.area / bbox_area > 0.92:
        occ[j0:j1, i0:i1] = True
        return

    xs = x0 + (np.arange(i0, i1) + 0.5) * res
    ys = y0 + (np.arange(j0, j1) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    try:
        occ[j0:j1, i0:i1] |= shapely.contains_xy(poly, gx, gy)
    except Exception:
        occ[j0:j1, i0:i1] = True
