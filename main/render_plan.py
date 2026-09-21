"""Render a top-down floor-plan crop with one room highlighted, for the VLM.

The serialized graphs (`results/*.graph.json`) keep only room centroids, not
footprint polygons -- geometry is intentionally not duplicated into the graph
export. Producing an image therefore means re-parsing the source IFC. Each
model is parsed once and cached; per-room crops are cheap after that, since
they reuse the parsed wall and room geometry.

    from render_plan import PlanCache
    cache = PlanCache()
    img = cache.crop("dataset/test_set/model_0.ifc", room_number="107")
"""

from __future__ import annotations

import io
import logging
import os
import sys

os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPoly
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bimsg import parser as P
from bimsg import topology


def _draw(ax, poly, **kw):
    if poly is None or poly.is_empty:
        return
    geoms = [poly] if poly.geom_type == "Polygon" else list(getattr(poly, "geoms", []))
    for g in geoms:
        if g.geom_type == "Polygon" and not g.is_empty:
            ax.add_patch(MplPoly(list(g.exterior.coords), **kw))


log = logging.getLogger(__name__)


class PlanCache:
    """Parses each IFC once; serves highlighted room crops from the cache."""

    def __init__(self):
        self._models = {}

    def _get(self, ifc_path: str):
        if ifc_path not in self._models:
            model = P.parse(ifc_path)
            topology.assign_storeys(model)
            self._models[ifc_path] = model
        return self._models[ifc_path]

    def crop(self, ifc_path: str, room_number: str, size_px: int = 448,
            margin_m: float = 6.0, gid: str = None, storey_gid: str = None):
        """A plan crop centred on one room, with that room highlighted.

        Shows enough surrounding context (a fixed metre margin, not a fixed
        room count) for a vision model to judge scale and neighbours, which is
        the information the text backend gets for free from graph edges.

        `gid` is the room's IFC GlobalId and identifies it exactly. Selecting by
        room number alone took the first space with that name, and room numbers
        repeat in this corpus -- `model_6` and `model_17` both have duplicates --
        so a room could be shown a picture of a different room on another floor
        and scored on it. A caller that knows the GlobalId must pass it; a
        mismatch returns None rather than a plausible wrong crop, because a
        missing image is visible in the results and a wrong one is not.
        """
        model = self._get(ifc_path)
        if gid is not None:
            target = next((s for s in model.spaces
                           if s.gid == gid and s.footprint is not None), None)
            if target is None:
                log.warning("no space %s with geometry in %s", gid, ifc_path)
                return None
            if storey_gid is not None and target.storey_gid != storey_gid:
                log.warning("space %s is on storey %s, not %s -- refusing to "
                            "render a crop of a different floor",
                            gid, target.storey_gid, storey_gid)
                return None
        else:
            matches = [s for s in model.spaces
                       if s.name == room_number and s.footprint is not None]
            if not matches:
                return None
            if len(matches) > 1:
                # Ambiguous by name. Guessing here is how a room is scored on
                # another room's picture.
                log.warning("room number %r matches %d spaces in %s; pass gid=",
                            room_number, len(matches), ifc_path)
                return None
            target = matches[0]

        st = model.storey_by_gid(target.storey_gid)
        z = (st.elevation if st else target.zmin) + 1.2
        cands = [p for p in model.products
                 if p.storey_gid == target.storey_gid and p.footprint is not None]

        cx, cy = target.footprint.centroid.coords[0]
        fig, ax = plt.subplots(figsize=(size_px / 100, size_px / 100), dpi=100)

        for p in cands:
            if p.is_occluding and p.zmin <= z <= p.zmax:
                _draw(ax, p.footprint, facecolor="0.55", edgecolor="0.3",
                     lw=0.4, zorder=2)
        for sp in model.spaces:
            if sp.storey_gid != target.storey_gid or sp.footprint is None:
                continue
            hi = sp.gid == target.gid
            _draw(ax, sp.footprint,
                 facecolor="#ff5533" if hi else "#3388cc",
                 alpha=0.75 if hi else 0.25,
                 edgecolor="#aa2200" if hi else "#225588",
                 lw=1.6 if hi else 0.6, zorder=3 if hi else 1)

        ax.set_xlim(cx - margin_m, cx + margin_m)
        ax.set_ylim(cy - margin_m, cy + margin_m)
        ax.set_aspect("equal")
        ax.axis("off")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        buf.seek(0)
        return Image.open(buf).convert("RGB")
