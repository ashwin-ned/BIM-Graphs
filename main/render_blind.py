"""Render what a model is allowed to see: geometry, and nothing that answers the task.

The 984 floorplan PNGs already in `dataset/2025_BIMfundamentals/floorplans/` cannot
be used as a vision input. Every one of them states the answer in legible type --
`MASTER BEDROOM 24.8 m2`, `KITCHEN 11.0 m2` -- and three further leaks are not
suppressed by `render_floorplan.py --no-labels`:

  * room *fills* are the room-segmentation ground truth, drawn on top of the walls;
  * the fill tint and the dashed outline encode whether the IFC stated a room or the
    pipeline recovered it, which is part of the graph partition;
  * a `?` is appended to the name of every recovered room;
  * the storey name and elevation are drawn as a title unconditionally.

A model given those needs OCR, not spatial reasoning. So this is a separate module
rather than another flag on a 552-line renderer, and it holds one auditable
invariant: **it never calls `dr.text`**. `tests/test_blind_render.py` asserts that by
reading this source, and asserts it again by OCR-ing the output.

Two conditions are produced:

  plan  walls, columns and structure, with doors and windows drawn *differently*.
        `render_floorplan.py` puts both in one style bucket at the same colour and
        weight, so a model literally cannot tell a door from a window -- which makes
        door-mediated connectivity unrecoverable from vision and the comparison
        against the text modality unfair. Door positions are an input, not an
        answer: the IFC states them and the text modality gets them too.

  occ   a binary wall/free-space raster. Leak-free by construction, and derived from
        wall geometry alone -- never from `IfcSpace`. That matters: the damage levels
        remove `IfcSpace` from half the storeys while leaving `IfcWall` untouched, so
        an occupancy map built from walls is invariant to the damage and an occupancy
        map built from spaces would degrade with it, silently destroying the
        experiment.

Both read the `*_floorplans.json` vector sidecars, so no IFC is re-parsed: the whole
208-model corpus re-renders in minutes, against roughly 50 minutes from source.

    python main/render_blind.py --from-json dataset/2025_BIMfundamentals/floorplans \
        --out dataset/2025_BIMfundamentals/vision --mode both
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

try:
    from PIL import Image, ImageDraw
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# --------------------------------------------------------------------------
# styling -- geometry only, and doors distinguishable from windows
# --------------------------------------------------------------------------

# (colour, line width in px). Doors and windows are deliberately separated;
# everything else keeps render_floorplan.py's weights so the two renderers stay
# visually comparable.
BLIND_STYLES = {
    "structure": ((20, 20, 20), 2.6),      # walls, columns, curtain wall, plates
    "door":      ((198, 62, 38), 2.4),     # IfcDoor -- its own colour and weight
    "window":    ((70, 140, 200), 1.4),    # IfcWindow -- distinct from a door
    "light":     ((150, 150, 155), 1.0),   # stairs, railings, slabs, ramps, beams
}

STRUCTURE_CLASSES = {
    "IfcWall", "IfcWallStandardCase", "IfcColumn", "IfcCurtainWall",
    "IfcPlate", "IfcMember",
}

BG = (255, 255, 255)

# Occupancy raster: metres per cell. Matches main/bimsg/spaces.py RESOLUTION so
# the two rasterisations are directly comparable.
OCC_RES = 0.10
OCC_MAX_CELLS = 40_000_000
# Most vision encoders want a side of at least ~336 px; a small storey at
# 0.10 m/cell comes out well under that.
OCC_MIN_SIDE = 384


def bucket_for(ifc_class: str) -> str:
    if ifc_class == "IfcDoor":
        return "door"
    if ifc_class == "IfcWindow":
        return "window"
    if ifc_class in STRUCTURE_CLASSES:
        return "structure"
    return "light"


# --------------------------------------------------------------------------
# plan condition
# --------------------------------------------------------------------------

def render_plan_png(storey: dict, path: str, tr: dict, ss: int = 2) -> str:
    """Geometry only. No spaces, no text, no title, no scale bar.

    `tr` is the storey's stored `transform`, so the output shares a pixel grid
    with the existing corpus and the recorded px_per_m/tx/ty still lift a pixel
    coordinate back into metres.
    """
    if not HAVE_PIL:
        raise RuntimeError("Pillow is required (pip install Pillow)")

    W, H = tr["width"] * ss, tr["height"] * ss
    s, tx, ty = tr["px_per_m"] * ss, tr["tx"] * ss, tr["ty"] * ss
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    def P(pt):
        return (pt[0] * s + tx, -pt[1] * s + ty)

    # Deliberately NOT drawn: storey["spaces"]. Room extents are the answer to
    # the room-recovery half of the task.
    #
    # Draw order light -> structure -> door/window so apertures stay visible on
    # top of the wall they sit in.
    for bucket in ("light", "structure", "window", "door"):
        col, w = BLIND_STYLES[bucket]
        width = max(1, int(w * ss))
        for el in storey.get("elements") or []:
            if bucket_for(el.get("ifc_class", "")) != bucket:
                continue
            for pl in el.get("polylines") or []:
                if len(pl) >= 2:
                    dr.line([P(p) for p in pl], fill=col, width=width, joint="curve")

    img = img.resize((tr["width"], tr["height"]), Image.LANCZOS)
    img.save(path)
    return path


# --------------------------------------------------------------------------
# occupancy condition
# --------------------------------------------------------------------------

def occupancy_transform(storey: dict, res: float = OCC_RES,
                        min_side: int = OCC_MIN_SIDE) -> dict | None:
    """World metres -> occupancy-raster pixels: px = s*x + tx, py = -s*y + ty.

    The occupancy raster does **not** share the plan render's pixel grid. The
    plan fits a fixed 1800 px width to the storey (`render_floorplan.
    view_transform`, ~89.6 px/m on a typical synthetic storey); this raster is
    metric, at `1/res` = 10 px/m, with its own origin, and is then enlarged by
    an integer NEAREST factor to reach `min_side`.

    This function exists because those two transforms were briefly conflated:
    the extraction harness lifted a model's occupancy-image pixel answer with
    the *plan* transform, which is a factor of ~9 out. Nothing would have
    caught it -- the rooms still land somewhere, so the prediction looks
    scorable and simply reads 0.000. Both the renderer and the harness now take
    the grid from here, so there is one definition and no drift.
    """
    b = storey.get("bounds") or {}
    lo, hi = b.get("min"), b.get("max")
    if not lo or not hi or len(lo) < 2 or len(hi) < 2:
        return None
    x0, y0 = float(lo[0]), float(lo[1])
    x1, y1 = float(hi[0]), float(hi[1])
    if not (x1 > x0 and y1 > y0):
        return None
    pad = 0.5
    x0 -= pad; x1 += pad; y0 -= pad; y1 += pad
    nx = int(np.ceil((x1 - x0) / res))
    ny = int(np.ceil((y1 - y0) / res))
    if nx < 2 or ny < 2 or nx * ny > OCC_MAX_CELLS:
        return None

    s = 1.0 / res
    tx = -x0 * s
    ty = y1 * s          # image row 0 is the top, i.e. max y
    k = 1
    if min_side and min(nx, ny) < min_side:
        k = int(np.ceil(min_side / min(nx, ny)))
    return {"px_per_m": s * k, "tx": tx * k, "ty": ty * k,
            "width": nx * k, "height": ny * k,
            "base_px_per_m": s, "base_tx": tx, "base_ty": ty,
            "base_width": nx, "base_height": ny, "upscale": k}


def render_occupancy_png(storey: dict, path: str, res: float = OCC_RES,
                         min_side: int = OCC_MIN_SIDE) -> str | None:
    """Binary raster: black where a solid sits at the cut plane, white elsewhere.

    Every polyline in the sidecar is a CLOSED loop -- a section cross-section of
    one solid -- so each is filled, not stroked. Stroking them instead seals the
    doorways: a wall's two faces are separate loops and a brush wide enough to
    look like a wall spills across the 0.9 m opening between them. Measured on
    model_0's ground floor, stroking left only 6% of door geometry on free space.

    Doors are left as GAPS; everything else, windows included, is filled.

    This differs from main/bimsg/spaces.py on purpose. That module fills doors as
    barriers (`spaces.py:76-78`, "Doors are barriers too: they separate rooms so
    components come out clean") because it wants flood-fill components to equal
    individual rooms, and restores connectivity afterwards from the door products
    themselves. Here the image is the model's only evidence, so an aperture it
    cannot see is connectivity it cannot recover.

    Windows are filled even though IfcWindow is absent from
    parser.OCCLUDING_TYPES, because these are section cuts rather than XY
    projections: a window at the 1.2 m cut plane leaves a real hole in its wall,
    and an unfilled hole lets free space escape through every window.

    Known limitation, stated rather than papered over: the raster is NOT bounded
    to the storey's floor plate. spaces.py bounds its flood fill with
    `_slab_domain()` (`:103-104`, "rooms must stand on a floor"), but no slab
    survives a 1.2 m section -- model_0's ground floor carries zero IfcSlab
    elements -- so no floor-plate geometry is available here. Free space
    therefore continues past the building envelope wherever a real opening
    exists, including through external doors. That is correct as a picture and
    wrong as a domain for flood-fill, so treat this condition as an image for a
    model to read, not as a segmentation mask.
    """
    if not HAVE_PIL:
        raise RuntimeError("Pillow is required (pip install Pillow)")

    tr = occupancy_transform(storey, res=res, min_side=min_side)
    if tr is None:
        return None
    # Before any upscale: the polygons are drawn on the base grid and the
    # result is enlarged afterwards, so drawing uses the base scale.
    s, tx, ty = tr["base_px_per_m"], tr["base_tx"], tr["base_ty"]
    nx, ny = tr["base_width"], tr["base_height"]

    # "L" mode with polygon fill: Pillow does not antialias, so the result stays
    # strictly two-valued. A grey edge pixel in an occupancy map would be a lie
    # about whether a cell is passable.
    img = Image.new("L", (nx, ny), 255)
    dr = ImageDraw.Draw(img)
    for el in storey.get("elements") or []:
        if el.get("ifc_class") == "IfcDoor":
            continue
        for pl in el.get("polylines") or []:
            if len(pl) >= 3:
                dr.polygon([(p[0] * s + tx, -p[1] * s + ty) for p in pl], fill=0)

    if min_side and min(img.size) < min_side:
        # NEAREST, never LANCZOS, for the same reason.
        k = int(np.ceil(min_side / min(img.size)))
        img = img.resize((img.width * k, img.height * k), Image.NEAREST)
    img.save(path)
    return path


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def storey_slug(st: dict, i: int) -> str:
    name = (st.get("name") or f"level{i}").lower()
    keep = "".join(c if c.isalnum() else "_" for c in name).strip("_")
    return f"{i:02d}_{keep or 'level'}"


def render_model(sidecar: str, out_dir: str, mode: str, res: float,
                 min_side: int = OCC_MIN_SIDE) -> dict:
    """Render every storey of one model. Returns a per-model record."""
    with open(sidecar) as fh:
        doc = json.load(fh)
    model = os.path.basename(sidecar).replace("_floorplans.json", "")
    os.makedirs(out_dir, exist_ok=True)

    rec = {"model": model, "storeys": 0, "plan": 0, "occ": 0, "skipped": []}
    for i, st in enumerate(doc.get("storeys") or []):
        rec["storeys"] += 1
        tr = st.get("transform")
        slug = storey_slug(st, i)
        if mode in ("plan", "both"):
            if not tr or not st.get("elements"):
                rec["skipped"].append(f"{slug}:plan:no-geometry")
            else:
                render_plan_png(st, os.path.join(out_dir, f"{model}_{slug}_plan.png"), tr)
                rec["plan"] += 1
        if mode in ("occ", "both"):
            p = render_occupancy_png(
                st, os.path.join(out_dir, f"{model}_{slug}_occ.png"),
                res=res, min_side=min_side)
            if p is None:
                rec["skipped"].append(f"{slug}:occ:no-bounds")
            else:
                rec["occ"] += 1
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-json", required=True,
                    help="directory of model_*/model_*_floorplans.json sidecars, "
                         "or a single sidecar file")
    ap.add_argument("--out", required=True, help="directory to write images into")
    ap.add_argument("--mode", choices=("plan", "occ", "both"), default="both")
    ap.add_argument("--res", type=float, default=OCC_RES,
                    help="metres per cell for the occupancy raster")
    ap.add_argument("--min-side", type=int, default=OCC_MIN_SIDE,
                    help="upscale the occupancy raster to at least this many px "
                         "on its short side (nearest-neighbour, stays binary)")
    ap.add_argument("--models", nargs="*", help="only these model names")
    a = ap.parse_args(argv)

    if os.path.isfile(a.from_json):
        sidecars = [a.from_json]
    else:
        sidecars = sorted(glob.glob(os.path.join(a.from_json, "*", "*_floorplans.json")))
        sidecars += sorted(glob.glob(os.path.join(a.from_json, "*_floorplans.json")))
    if a.models:
        want = set(a.models)
        sidecars = [p for p in sidecars
                    if os.path.basename(p).replace("_floorplans.json", "") in want]
    if not sidecars:
        print(f"no *_floorplans.json under {a.from_json}")
        return 1

    print(f"rendering {len(sidecars)} model(s), mode={a.mode} -> {a.out}")
    tot = {"storeys": 0, "plan": 0, "occ": 0}
    skipped = []
    for n, p in enumerate(sidecars, 1):
        try:
            rec = render_model(p, a.out, a.mode, a.res, a.min_side)
        except Exception as e:
            model = os.path.basename(p).replace("_floorplans.json", "")
            print(f"  !! {model}: {type(e).__name__}: {e}")
            skipped.append(f"{model}:failed")
            continue
        for k in tot:
            tot[k] += rec[k]
        skipped += [f"{rec['model']}:{s}" for s in rec["skipped"]]
        if n % 25 == 0:
            print(f"  {n}/{len(sidecars)}")

    print(f"\n{tot['storeys']} storey(s): {tot['plan']} plan, {tot['occ']} occupancy")
    if skipped:
        # Named, not just counted: a storey with no image is a storey no model
        # can be asked about, and it would otherwise vanish from the denominator.
        print(f"{len(skipped)} skipped:")
        for s in skipped[:20]:
            print(f"  {s}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
