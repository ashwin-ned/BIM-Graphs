"""Check the vision modality does not hand the model its answer.

The whole multimodal comparison rests on one property: an image given to a model
must show the building's geometry and nothing that states the graph. The 984
PNGs in `dataset/2025_BIMfundamentals/floorplans/` fail that badly -- room names
and areas in legible type, a `?` on every recovered room, and a fill tint
encoding whether the IFC stated a room or the pipeline guessed it. A model given
those needs OCR, not spatial reasoning, and the text-vs-vision comparison would
be meaningless.

There is no OCR in this environment (no pytesseract, no tesseract binary) and
the GPU that would let a VLM read the images is occupied, so the leak is caught
by colour instead. That works because the leaking renderer draws with known
constants: `SPACE_FILL_INFERRED` covers 949,601 pixels of model_0's ground floor
alone. Every check below is therefore run against BOTH the old leaking image and
the new blind one -- the old one must trip it and the new one must not. A gate
that has not been shown to fire is not a gate.

    python tests/test_blind_render.py
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "main"))

from PIL import Image                      # noqa: E402
import render_blind as RB                   # noqa: E402

# Colours the *leaking* renderer uses for things a model must not be shown.
# From main/render_floorplan.py:65-66 and the label/title draw calls at :400-406.
FORBIDDEN = {
    "space fill (IFC-stated room)":      (243, 246, 249),
    "space fill (recovered room)":       (247, 240, 227),
    "space outline":                     (175, 190, 205),
    "room-name text":                    (60, 70, 85),
    "area text":                         (130, 140, 155),
    "title text":                        (30, 30, 35),
}

# Above this many pixels of a forbidden colour, someone painted a region; below
# it, LANCZOS blending coincided with the constant. The leaking reference sits
# four orders of magnitude above it.
FILL_NOISE = 50

LEAKY = os.path.join(ROOT, "dataset", "2025_BIMfundamentals", "floorplans",
                     "model_0", "model_0_00_ground_floor.png")
VISION = os.path.join(ROOT, "dataset", "2025_BIMfundamentals", "vision")

failures = 0


def ok(msg: str, cond: bool) -> None:
    global failures
    if not cond:
        failures += 1
    print(("  PASS " if cond else "  FAIL ") + msg)


def forbidden_counts(path: str) -> dict:
    a = np.array(Image.open(path).convert("RGB"))
    return {name: int(np.all(a == np.array(c), axis=-1).sum())
            for name, c in FORBIDDEN.items()}


def main() -> int:
    # ---- the source-level invariant -------------------------------------
    src = open(os.path.join(ROOT, "main", "render_blind.py")).read()
    body = src.split('"""', 2)[-1]        # skip the module docstring
    # Comments are stripped, because this module documents what it deliberately
    # does NOT draw -- and an earlier version of this test failed on the comment
    # that says so.
    code = "\n".join(
        ln.split("#", 1)[0] for ln in body.splitlines()
        if not ln.strip().startswith("#"))
    ok("render_blind.py never draws text (no .text( call in its code)",
       ".text(" not in code)
    ok("render_blind.py never reads storey['spaces'] in code",
       'storey["spaces"]' not in code and "storey['spaces']" not in code
       and '"spaces"' not in code)

    # ---- doors must be distinguishable from windows ---------------------
    # Without this the door-mediated half of the task is unrecoverable from
    # vision, and render_floorplan.py puts both in one bucket at one colour.
    d_col, d_w = RB.BLIND_STYLES["door"]
    w_col, w_w = RB.BLIND_STYLES["window"]
    ok("doors and windows render in different colours", d_col != w_col)
    ok("doors and windows render at different weights", d_w != w_w)
    ok("a door is not the structure colour", d_col != RB.BLIND_STYLES["structure"][0])

    # ---- the gate must fire on the known-bad image ----------------------
    if not os.path.exists(LEAKY):
        print(f"  SKIP the leaking reference image is gone ({LEAKY})")
    else:
        leak = forbidden_counts(LEAKY)
        tripped = {k: v for k, v in leak.items() if v > 0}
        ok(f"the gate FIRES on the old leaking render ({len(tripped)} forbidden "
           f"colour(s) present, worst {max(leak.values())} px)",
           max(leak.values()) > 1000)
        for k, v in sorted(tripped.items(), key=lambda kv: -kv[1]):
            print(f"         leaking image contains {v:>7d} px of {k}")

    # ---- and must not fire on the blind renders -------------------------
    if not os.path.isdir(VISION):
        print(f"  FAIL no blind renders at {VISION} -- run main/render_blind.py")
        return failures + 1

    plans = sorted(p for p in os.listdir(VISION) if p.endswith("_plan.png"))
    occs = sorted(p for p in os.listdir(VISION) if p.endswith("_occ.png"))
    ok(f"blind plan renders exist ({len(plans)})", len(plans) > 0)
    ok(f"blind occupancy renders exist ({len(occs)})", len(occs) > 0)

    # A sample, spread across the corpus rather than the first N, so a
    # systematic failure later in the run cannot hide behind an early pass.
    step = max(1, len(plans) // 40)
    sample = plans[::step][:40]
    bad = {}
    for name in sample:
        counts = forbidden_counts(os.path.join(VISION, name))
        # A threshold, not zero. These images are supersampled and then
        # LANCZOS-downsampled, so a blend between a dark line and the white
        # ground lands on an arbitrary colour -- including, a few pixels at a
        # time, on a near-white constant like (243,246,249). A real filled room
        # is not a few pixels: the leaking model_0 render carries 949,601 of
        # them. Anything under FILL_NOISE is blending; anything over it is a
        # region someone painted on purpose.
        hit = {k: v for k, v in counts.items() if v > FILL_NOISE}
        if hit:
            bad[name] = hit
    ok(f"no painted-on region in {len(sample)} sampled plan renders "
       f"(tolerating <={FILL_NOISE} px of blend noise)", not bad)
    for name, hit in list(bad.items())[:5]:
        print(f"         {name}: {hit}")
    worst = max((max(forbidden_counts(os.path.join(VISION, n)).values())
                 for n in sample), default=0)
    ok(f"the worst blend noise across the sample is small ({worst} px)",
       worst <= FILL_NOISE)

    # ---- occupancy must be strictly binary ------------------------------
    step = max(1, len(occs) // 40)
    nonbinary = {}
    for name in occs[::step][:40]:
        a = np.array(Image.open(os.path.join(VISION, name)))
        vals = set(np.unique(a).tolist())
        if not vals <= {0, 255}:
            nonbinary[name] = sorted(vals)[:6]
    ok("sampled occupancy rasters are strictly two-valued", not nonbinary)
    for name, vals in list(nonbinary.items())[:5]:
        print(f"         {name}: {vals}")

    # ---- doorways must be traversable in the occupancy raster -----------
    # An occupancy map whose doorways are sealed says the building has no way
    # through it. Stroking the section outlines instead of filling them did
    # exactly that: only 6% of door geometry survived as free space.
    side = os.path.join(ROOT, "dataset", "2025_BIMfundamentals", "floorplans",
                        "model_0", "model_0_floorplans.json")
    plan = os.path.join(ROOT, "dataset", "2025_BIMfundamentals", "plans",
                        "model_0.plan.json")
    occ_img = os.path.join(VISION, "model_0_00_ground_floor_occ.png")
    if all(os.path.exists(p) for p in (side, plan, occ_img)):
        st = json.load(open(side))["storeys"][0]
        doors = json.load(open(plan))["storeys"][0]["doors"]
        a = np.array(Image.open(occ_img))
        res, pad = RB.OCC_RES, 0.5
        (x0, y0), (x1, y1) = st["bounds"]["min"], st["bounds"]["max"]
        x0 -= pad; x1 += pad; y0 -= pad; y1 += pad
        nx = int(np.ceil((x1 - x0) / res))
        k = a.shape[1] // nx
        s, tx, ty = 1.0 / res, -x0 / res, y1 / res
        opened = 0
        for dd in doors:
            ix = int((dd["x"] * s + tx) * k)
            iy = int((-dd["y"] * s + ty) * k)
            r = 2 * k
            win = a[max(0, iy - r):iy + r + 1, max(0, ix - r):ix + r + 1]
            if win.size and (win == 255).mean() > 0.15:
                opened += 1
        ok(f"every doorway is open in the occupancy raster ({opened}/{len(doors)})",
           doors and opened == len(doors))
    else:
        print("  SKIP doorway check needs model_0's sidecar, plan and occ render")

    # ---- the experimental control ---------------------------------------
    # The whole multimodal claim rests on this: the damage levels degrade the
    # IFC text (IfcSpace 2049 -> 994 from L1 to L2) while leaving the geometry
    # alone (IfcWall 8912 at every level). If the rendered images are identical
    # across levels then any change in a model's score across levels cannot be
    # attributed to the picture, and the comparison is genuinely controlled.
    # Asserted at the pixel level rather than argued from damage_ifc.py's intent.
    import hashlib
    syn = os.path.join(ROOT, "dataset", "synthetic_data")
    levels = ["synth150", "synth150_L2", "synth150_L3", "synth150_L4"]
    present = [l for l in levels if os.path.isdir(os.path.join(syn, l, "vision"))]
    if len(present) < 2:
        print(f"  SKIP need >=2 rendered synthetic levels, have {present}")
    else:
        def digest(path):
            return hashlib.sha256(open(path, "rb").read()).hexdigest()
        for kind in ("plan", "occ"):
            ref_dir = os.path.join(syn, present[0], "vision")
            names = sorted(n for n in os.listdir(ref_dir) if n.endswith(f"_{kind}.png"))
            differing = []
            for n in names:
                seen = set()
                for l in present:
                    q = os.path.join(syn, l, "vision", n)
                    if os.path.exists(q):
                        seen.add(digest(q))
                if len(seen) > 1:
                    differing.append(n)
            ok(f"{kind} renders are byte-identical across {len(present)} damage "
               f"levels ({len(names) - len(differing)}/{len(names)})",
               names and not differing)
            for n in differing[:5]:
                print(f"         differs across levels: {n}")

    print()
    return failures


if __name__ == "__main__":
    n = main()
    print(f"FAIL the vision modality leaks ({n} problem(s))" if n else
          "PASS the vision modality shows geometry and states no answer")
    sys.exit(1 if n else 0)
