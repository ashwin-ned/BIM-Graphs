"""Score a model on recovering a storey's graph, in the same scorer as the method.

The per-room harness (`main/eval_semantics.py`) asks a model for one word about
one room. This asks for the structure: every room on a storey, what each is
for, and which pairs open onto each other. That is the task the paper is about.

Nothing here defines a metric. The model's answer is parsed into a graph in the
pipeline's own shape (`bimsg.graph_extract.to_graph`) and handed to
`evaluation.scoring.score_connectivity` and `score_labels` -- the identical code
that scores the deterministic method. That was the point of building the
evaluation contract: a model and a hand-written heuristic are compared by one
definition of precision, and there is no second metric to drift out of step.

Scope, stated rather than discovered:

* **One storey per call.** A plan image is one storey, so per-storey text keeps
  the modalities comparable instead of asking the text condition to do more.
* **Vertical links are out of scope**, and are removed from the *reference*
  too, by `GroundTruth.restrict`, which counts what it removed. The
  deterministic path already recovers them at precision 1.000, so there is
  nothing for a model to win and a great deal for it to lose by accident.
* **A non-answer is a non-answer.** Every parse failure is counted and named
  (`bimsg.graph_extract.Extraction.status`), and a failed extraction scores as
  an empty prediction rather than a partial one.

Seven modalities. `text` serialises the storey; `plan` and `occ` show a bare
render and ask for pixel coordinates; `plan_som`, `occ_som` and
`text+plan_som` show the same render with a numbered disc on each declared
room, so the model names rooms by number and never has to localise anything.
The set-of-mark conditions are the primary vision ones -- asking for bare
coordinates measures coordinate regression, not plan reading. See
`render_blind.render_som_png` for what the marks give away.

Four backend kinds, two of which exist only to keep the harness honest:

    oracle   emits the reference itself, through the real parser and the real
             scorer. It must score 1.000. When it does not, the harness is
             broken, not the model -- and every model number taken through a
             broken harness would have been unfalsifiable. Run it first, and
             with `--oracle-anchor pixels`: the default `identity` anchor
             touches no geometry and passed against a sign-flipped lift.
    replay   re-scores raw responses from an earlier run's provenance log, so a
             scorer fix does not cost the GPU hours again.
    llm      a text model (default GLM-4-9B-0414)
    vlm      a vision model (default GLM-4.1V-9B-Thinking). InternVL3 is
             routed to `InternVLBackend`, which is the one model of the six
             that does not speak the generic image-text-to-text API.

Use `--max-new-tokens 4096` or more for any vision cell: a whole storey's
JSON after a thinking model's trace does not fit in 2048, where two thirds of
a probe truncated.

    # verify the harness before spending anything on it
    python main/extract_graph_llm.py --corpus dataset/synthetic_data/synth150 \\
        --backend oracle --oracle-anchor pixels --modality plan_som

    python main/extract_graph_llm.py --corpus dataset/synthetic_data/synth150 \\
        --backend vlm --model-id glm-9b-v --modality plan_som \\
        --max-new-tokens 4096 --out results/extract/
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bimsg import graph_extract as X
from evaluation import matching as MATCH
from evaluation import schema as SCHEMA
from evaluation import scoring as SCORE
from evaluation.manifest import RunManifest
from render_blind import declared_marks, occupancy_transform, storey_slug

MODALITIES = X.ALL_MODALITIES

# The grid's model list, declared in one place so a cell is identified by a
# short name rather than by a repo path spelled slightly differently in four
# scripts. `kind` selects the backend; `pairs_with` records where a text and a
# vision model share a language backbone, which is what makes a text-vs-vision
# comparison a modality result rather than a model-family result.
# `max_new_tokens` is per model because the requirement differs by an order of
# magnitude, and the default that suits one truncates another. Measured
# 21 Sep 2026 on the set-of-mark task: GLM-4.1V-9B-Thinking spends 92-100% of
# its output on a `<think>` trace (traces 8-15k characters against answers of
# 395-693), so it needs ~6k while a non-thinking model needs a fraction of
# that. Prefilling a closed `</think>` does NOT suppress the trace -- the
# model reasons in prose instead and emits nothing parseable -- so the cost is
# intrinsic and budgeted for rather than worked around.
MODELS = {
    # -- text --------------------------------------------------------------
    "glm-9b":     {"id": "zai-org/GLM-4-9B-0414", "kind": "text",
                   "pairs_with": "glm-9b-v", "max_new_tokens": 3072,
                   "thinking": False},
    "qwen25-7b":  {"id": "Qwen/Qwen2.5-7B-Instruct", "kind": "text",
                   "pairs_with": "qwen25-vl-7b", "max_new_tokens": 3072,
                   "thinking": False},
    # Third text model. Llama-3.1-8B-Instruct and gemma-2-9b-it were the
    # obvious choices and are both gated on the Hub (HTTP 401 without a
    # token), so this is the strongest ungated model in the same weight class.
    # It also gives a within-family generation comparison against
    # Qwen2.5-7B on the same task.
    # Qwen3 reasons by default and its trace does not fit any cap worth paying
    # for: at 6144 every unit of a 4-unit probe truncated and the cell scored
    # F1 0.000, which is not a result but an absence of one. Unlike
    # GLM-4.1V-Thinking it exposes a real toggle -- its chat template takes
    # `enable_thinking=False`, which prefills a closed empty trace the model is
    # trained to respect -- so it is run in that mode and the paper says so.
    "qwen3-8b":   {"id": "Qwen/Qwen3-8B", "kind": "text", "pairs_with": None,
                   "max_new_tokens": 3072, "thinking": False,
                   "no_think_template": True},
    # -- vision ------------------------------------------------------------
    "glm-9b-v":   {"id": "zai-org/GLM-4.1V-9B-Thinking", "kind": "vision",
                   "pairs_with": "glm-9b", "max_new_tokens": 6144,
                   "thinking": True},
    "qwen25-vl-7b": {"id": "Qwen/Qwen2.5-VL-7B-Instruct", "kind": "vision",
                     "pairs_with": "qwen25-7b", "max_new_tokens": 3072,
                     "thinking": False},
    # InternVL3-8B's language tower is Qwen2.5-7B, so its text counterpart is
    # already in the list rather than being a fourth download.
    "internvl3-8b": {"id": "OpenGVLab/InternVL3-8B", "kind": "vision",
                     "pairs_with": "qwen25-7b", "max_new_tokens": 3072,
                     "thinking": False},
}


def cap_for(name: str, fallback: int) -> int:
    """The model's own generation cap, or the caller's if it is not in the registry."""
    return MODELS.get(name, {}).get("max_new_tokens") or fallback


def resolve_model(name_or_id: str, kind: str) -> str:
    """A registry short name, or a bare repo id passed straight through."""
    if name_or_id in MODELS:
        entry = MODELS[name_or_id]
        if entry["kind"] != kind:
            raise ValueError(
                f"{name_or_id!r} is a {entry['kind']} model; --backend "
                f"{'llm' if kind == 'text' else 'vlm'} expects a {kind} one")
        return entry["id"]
    return name_or_id


def output_tag(corpus: str, backend: str, model: str | None, modality: str,
               mask: bool, oracle_anchor: str | None = None) -> str:
    """The stem every output file of one cell shares. ONE definition.

    There were briefly two: this module named its files
    `{corpus}_{backend}_{modality}` while `run_grid.cell_tag` expected
    `{corpus}_{backend}-{model}_{modality}`. Two consequences, both silent.
    Every text model wrote to the same three filenames and overwrote the
    previous model's results -- 8 files where there should have been 24 -- and
    `run_grid`'s resume check looked for a name that never existed, so it
    would have re-run finished cells forever. Caught by a 2-unit smoke pass
    over all 56 cells; it would otherwise have destroyed two thirds of the
    grid quietly.
    """
    parts = [os.path.basename(corpus.rstrip("/")), backend]
    if backend == "oracle" and oracle_anchor:
        parts[-1] = f"{backend}-{oracle_anchor}"
    elif model:
        parts[-1] = f"{backend}-{model}"
    parts.append(modality.replace("+", "-"))
    tag = "_".join(parts)
    return tag + ("_masked" if mask else "")


def transform_for(modality: str, storey: dict) -> dict | None:
    """The pixel grid of the image this modality actually shows the model.

    `plan` and `text+plan` show the 1800 px plan render, whose transform the
    sidecar stores. `occ` shows the metric occupancy raster, which has a
    completely different grid (~30 px/m against the plan's ~89.6, and its own
    origin and size). Lifting an occupancy answer with the plan transform is a
    factor-of-three error that still produces coordinates, so it reads as a
    legitimate 0.000 rather than as a fault -- `matching._diagnose` cannot see
    it, because the rooms do carry centroids. Hence one function, used by both
    the scorer path and the oracle.
    """
    if modality in ("occ", "occ_som"):
        return occupancy_transform(storey)
    if modality in ("plan", "text+plan", "plan_som", "text+plan_som"):
        return storey.get("transform")
    return None


def marks_for(unit: dict) -> list:
    """The numbered marks drawn on this unit's image.

    Preferred from the sidecar the renderer wrote, so the numbers the harness
    resolves are provably the numbers on the image. Recomputed from the storey
    only as a fallback -- both paths call `render_blind.declared_marks`, but a
    stale image beside a fresh sidecar would otherwise go unnoticed, which is
    the same drift the transform/image check exists to stop.
    """
    side = (unit.get("marks_path") or "")
    if side and os.path.exists(side):
        with open(side) as fh:
            return json.load(fh)["marks"]
    return declared_marks(unit["storey"])


def marks_to_gt(marks: list, gt) -> dict:
    """{mark index -> reference room id}, by identity where possible.

    Two dialects, two joins. The deriver and the annotator key rooms by IFC
    GlobalId, which is exactly what a declared space carries, so that is an
    exact join. The generator keys rooms by its own synthetic rid, which
    appears nowhere in the sidecar, so the mark's centroid is placed inside a
    reference room's extent instead. A mark that resolves to nothing is left
    out and the caller sees a shorter mapping -- it is never guessed into the
    nearest room.
    """
    out = {}
    taken = set()
    for mk in marks:
        gid = mk.get("gid")
        if gid in gt.rooms and gid not in taken:
            out[mk["index"]] = gid
            taken.add(gid)
            continue
        cx, cy = mk["centroid"]
        for rid, room in gt.rooms.items():
            if rid in taken or not room.bbox:
                continue
            x0, y0, x1, y1 = room.bbox
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                out[mk["index"]] = rid
                taken.add(rid)
                break
    return out


# --------------------------------------------------------------------------
# Units of work
# --------------------------------------------------------------------------

def find_units(corpus: str, modality: str, images: str | None,
               gt_glob: str | None = None, plans: str | None = None) -> list:
    """Every (building, storey) the corpus can serve for this modality.

    A unit that cannot be served -- no sidecar, no ground truth, no image where
    one is needed -- is returned with `skip` set rather than omitted, so it
    lands in the manifest as `missing` instead of shrinking the denominator.
    That is the failure `evaluation/manifest.py` exists to prevent.

    `gt_glob` and `plans` exist because the real corpus is laid out differently
    from the synthetic ones: its references live in `results/real_gt/` rather
    than beside the IFC, and its sidecars under
    `dataset/2025_BIMfundamentals/floorplans/`.
    """
    units = []
    gts = sorted(glob.glob(gt_glob or os.path.join(corpus, "*.gt.json")))
    # `_summary.json` and similar roll-ups are not buildings.
    gts = [p for p in gts if not os.path.basename(p).startswith("_")]
    img_dir = images or os.path.join(corpus, "vision")
    plan_dir = plans or os.path.join(corpus, "floorplans")
    for gt_path in gts:
        name = os.path.basename(gt_path).split(".")[0]
        side = os.path.join(plan_dir, name, f"{name}_floorplans.json")
        if not os.path.exists(side):
            units.append({"unit": f"{name}/?", "building": name,
                          "skip": f"no sidecar at {side}"})
            continue
        try:
            with open(side) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            units.append({"unit": f"{name}/?", "building": name,
                          "skip": f"unreadable sidecar: {e}"})
            continue
        for i, st in enumerate(doc.get("storeys") or []):
            slug = storey_slug(st, i)
            u = {"unit": f"{name}/{slug}", "building": name, "storey_index": i,
                 "storey": st, "gt_path": gt_path, "sidecar": side,
                 "slug": slug, "image": None, "skip": None}
            if modality in X.SOM_MODALITIES:
                kind = "occ_som" if modality == "occ_som" else "plan_som"
                p = os.path.join(img_dir, f"{name}_{slug}_{kind}.png")
                u["marks_path"] = os.path.join(
                    img_dir, f"{name}_{slug}_{kind}.marks.json")
                if not os.path.exists(p):
                    u["skip"] = (f"no {kind} render at {p} -- run "
                                 f"main/render_blind.py --mode som")
                else:
                    u["image"] = p
            elif modality in ("plan", "occ", "text+plan"):
                kind = "occ" if modality == "occ" else "plan"
                p = os.path.join(img_dir, f"{name}_{slug}_{kind}.png")
                if not os.path.exists(p):
                    u["skip"] = f"no {kind} render at {p}"
                else:
                    u["image"] = p
            if u["image"] and not u["skip"] and not transform_for(modality, st):
                # Without the transform a pixel answer cannot be lifted into
                # metres, so it could never match. Refusing the unit is right;
                # running it would produce a 0.000 that means nothing.
                u["skip"] = "storey has no stored transform to lift pixels with"
            units.append(u)
    return units


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class OracleBackend:
    """Emits the reference as if a model had. Must score 1.000.

    Not a baseline and not a result -- a harness test. It exercises the real
    prompt path, the real parser and the real scorer, so anything that breaks
    matching, scope restriction or unit conversion shows up here as a score
    below 1.000 instead of showing up as a model that mysteriously failed.

    `anchor` decides which matching path is exercised, and the default does
    **not** exercise the risky one:

      identity  room ids are the reference's own, so `match_rooms` joins by id
                and no geometry is involved. This passed with the spatial
                anchor deliberately removed entirely, which is exactly why the
                other two modes exist.
      metres    ids the reference does not have, plus a world-metre centroid,
                so matching must go through `_bbox_score`.
      pixels    the same, but the centroid is pushed through the *forward*
                transform into image pixels first, so the harness has to lift
                it back with `graph_extract.px_to_m`. This is the only oracle
                mode that tests what the vision conditions depend on, and a
                sign error or a swapped axis in that lift fails it.
    """

    def __init__(self, anchor: str = "identity"):
        self.anchor = anchor
        self.name = f"oracle:{anchor}"

    def generate(self, system, user, image=None, unit=None, gt=None,
                 max_new_tokens=0, transform=None, marks=None):
        if marks is not None:
            return self._as_marks(gt, marks)
        rooms, edges = [], []
        alias = {}
        for k, (rid, r) in enumerate(gt.rooms.items() if gt else []):
            # A synthetic id the reference has never heard of, so identity
            # matching cannot rescue a broken geometric path.
            mid = rid if self.anchor == "identity" else f"M{k:03d}"
            alias[rid] = mid
            room = {"id": mid, "label": r.label}
            if self.anchor != "identity" and r.bbox:
                cx = (r.bbox[0] + r.bbox[2]) / 2
                cy = (r.bbox[1] + r.bbox[3]) / 2
                if self.anchor == "pixels" and transform:
                    s = transform["px_per_m"]
                    room["centroid_px"] = [round(s * cx + transform["tx"], 2),
                                           round(-s * cy + transform["ty"], 2)]
                else:
                    # `metres` -- and also `pixels` on a modality that shows no
                    # image, where there is no pixel frame to answer in.
                    room["centroid_m"] = [round(cx, 3), round(cy, 3)]
            rooms.append(room)
        inv = {"connected_by_door": "door", "open_passage": "open"}
        for p, declared in (gt.positives.items() if gt else []):
            a, b = tuple(p)
            rel = SCORE.TYPE_MAP.get(declared)
            if rel is None and declared is None:
                # The derived reference records *that* two rooms connect and
                # not how: `schema.load_gt` sets every derived positive's type
                # to None. Emitting nothing for those made the oracle score
                # recall 0.000 on the real corpus while matching 583 of 583
                # rooms -- the oracle working exactly as intended, catching a
                # dialect difference rather than a scorer fault.
                #
                # Any connective relation is correct here, because
                # `score_connectivity` skips the type check when the reference
                # declares no type. `door` is the commonest and the choice is
                # not scored.
                rel = "connected_by_door"
            if rel == "vertically_connected":
                continue                   # out of scope by design
            if rel in X.EXTRACT_RELATIONS:
                edges.append({"a": alias.get(a, a), "b": alias.get(b, b),
                              "type": inv[rel]})
        return (json.dumps({"rooms": rooms, "edges": edges}), False,
                {"oracle": self.anchor})

    def _as_marks(self, gt, marks):
        """The reference answered the way a set-of-mark condition asks for it.

        Verifies the whole mark path end to end: the numbers the renderer drew,
        their resolution back to a room, and the scoring that follows. If the
        numbering and the reference ever disagree this returns below 1.000,
        which is the only way that mismatch is visible -- a model scoring badly
        on marks would otherwise look like a model that cannot read a plan.
        """
        m2g = marks_to_gt(marks, gt)
        g2m = {rid: idx for idx, rid in m2g.items()}
        rooms = [{"id": str(idx), "label": gt.rooms[rid].label}
                 for idx, rid in sorted(m2g.items())]
        inv = {"connected_by_door": "door", "open_passage": "open"}
        edges = []
        for p, declared in gt.positives.items():
            a, b = tuple(p)
            if a not in g2m or b not in g2m:
                continue
            rel = SCORE.TYPE_MAP.get(declared)
            if rel is None and declared is None:
                rel = "connected_by_door"
            if rel in X.EXTRACT_RELATIONS:
                edges.append({"a": str(g2m[a]), "b": str(g2m[b]),
                              "type": inv[rel]})
        return (json.dumps({"rooms": rooms, "edges": edges}), False,
                {"oracle": "marks", "marks": len(marks)})


class ReplayBackend:
    """Re-scores raw responses recorded by an earlier run."""

    def __init__(self, log_path: str):
        self.name = "replay"
        with open(log_path) as fh:
            doc = json.load(fh)
        recs = doc.get("predictions", doc if isinstance(doc, list) else [])
        self.by_unit = {r["unit"]: r for r in recs}
        self.source = log_path
        self.source_backend = (doc.get("config", {}) or {}).get("backend")

    def generate(self, system, user, image=None, unit=None, gt=None,
                 max_new_tokens=0):
        r = self.by_unit.get(unit)
        if r is None or r.get("raw") is None:
            return None, False, {"replay_miss": True}
        return r["raw"], bool(r.get("hit_cap")), {"replayed_from": self.source}


class TextBackend:
    """A causal LM reading the storey as text."""

    def __init__(self, model_id="zai-org/GLM-4-9B-0414", device="cuda",
                 no_think: bool = False):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch
        self.name = f"llm:{model_id.split('/')[-1]}"
        self.model_id = model_id
        # Only where the model's own template supports it -- see MODELS.
        self.no_think = no_think
        self.tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=torch.bfloat16, device_map=device,
            trust_remote_code=True)
        self.model.eval()
        self._torch = torch
        self.revision = getattr(self.model.config, "_commit_hash", None)

    def generate(self, system, user, image=None, unit=None, gt=None,
                 max_new_tokens=2048):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        kw = {"enable_thinking": False} if self.no_think else {}
        text = self.tok.apply_chat_template(msgs, tokenize=False,
                                            add_generation_prompt=True, **kw)
        ids = self.tok(text, return_tensors="pt").to(self.model.device)
        t0 = time.time()
        with self._torch.no_grad():
            out = self.model.generate(**ids, max_new_tokens=max_new_tokens,
                                      do_sample=False,
                                      pad_token_id=self.tok.eos_token_id)
        gen = out[0][ids["input_ids"].shape[1]:]
        return (self.tok.decode(gen, skip_special_tokens=True),
                len(gen) >= max_new_tokens,
                {"prompt_tokens": int(ids["input_ids"].shape[1]),
                 "generated_tokens": int(len(gen)),
                 "seconds": round(time.time() - t0, 2)})


class VisionBackend:
    """A vision-language model reading the rendered storey."""

    def __init__(self, model_id="zai-org/GLM-4.1V-9B-Thinking", device="cuda"):
        from transformers import AutoProcessor, AutoModelForImageTextToText
        import torch
        self.name = f"vlm:{model_id.split('/')[-1]}"
        self.model_id = model_id
        self.proc = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, dtype=torch.bfloat16, device_map=device,
            trust_remote_code=True)
        self.model.eval()
        self._torch = torch
        self.revision = getattr(self.model.config, "_commit_hash", None)

    def generate(self, system, user, image=None, unit=None, gt=None,
                 max_new_tokens=2048):
        from PIL import Image
        img = Image.open(image).convert("RGB")
        msgs = [{"role": "system", "content": [{"type": "text", "text": system}]},
                {"role": "user", "content": [{"type": "image"},
                                             {"type": "text", "text": user}]}]
        text = self.proc.apply_chat_template(msgs, tokenize=False,
                                             add_generation_prompt=True)
        inp = self.proc(text=text, images=img, return_tensors="pt").to(
            self.model.device)
        t0 = time.time()
        with self._torch.no_grad():
            out = self.model.generate(**inp, max_new_tokens=max_new_tokens,
                                      do_sample=False,
                                      pad_token_id=self.proc.tokenizer.eos_token_id)
        plen = inp["input_ids"].shape[1]
        gen = out[:, plen:]
        n_gen = int(gen.shape[1])
        return (self.proc.batch_decode(gen, skip_special_tokens=True)[0],
                n_gen >= max_new_tokens,
                {"prompt_tokens": int(plen), "generated_tokens": n_gen,
                 "seconds": round(time.time() - t0, 2)})


class InternVLBackend:
    """InternVL3, which does not speak the `AutoModelForImageTextToText` API.

    Checked before planning around it, per the rule this project earned the
    hard way, and it is the one model of the six that needed its own path:
    `AutoProcessor.from_pretrained` hands back a bare `Qwen2Tokenizer` with no
    image handling at all, the architecture is the custom `InternVLChatModel`,
    and the chat template rejects the list-of-parts content the other two
    vision models take ("can only concatenate str (not list) to str").

    So images go through InternVL's own documented preprocessing -- a dynamic
    tiling into 448 px patches whose aspect ratio is chosen per image -- and
    generation goes through `model.chat`.

    Its remote code also needs two packages the rest of this project does not:
    `einops` and `timm` (for `timm.layers.DropPath`). Install `timm` with
    `--no-deps`: a plain install resolves its own torch pin and can silently
    move this environment off torch 2.5.1+cu121, which everything else here is
    built against.

    One accounting compromise, stated because it affects a reported number:
    `model.chat` returns a decoded string and not token ids, so the generated
    length is recovered by re-tokenising the reply. That is exact for the
    truncation test's purpose (it asks whether the model ran out of budget),
    but it is a re-count rather than the generator's own, and it can read one
    or two tokens low where the reply ends mid-multi-byte-character.
    """

    IMAGENET_MEAN = (0.485, 0.456, 0.406)
    IMAGENET_STD = (0.229, 0.224, 0.225)

    def __init__(self, model_id="OpenGVLab/InternVL3-8B", device="cuda",
                 max_tiles: int = 12, tile: int = 448):
        from transformers import AutoModel, AutoTokenizer
        from transformers.modeling_utils import PreTrainedModel
        import torch

        # transformers 5.16.1 sets `all_tied_weights_keys` as an *instance*
        # attribute inside `post_init()`, and reads it back during weight
        # loading. InternVL3's vendored modelling code predates that API and
        # declares only the older `_tied_weights_keys`, so loading dies with
        # "'InternVLChatModel' object has no attribute
        # 'all_tied_weights_keys'". This is the transformers-vs-remote-code
        # gap the project flagged as a risk and that did NOT reproduce for the
        # GLM pair; it is real for this one model.
        #
        # A class-level default is the contained fix: every current model sets
        # its own instance attribute in post_init, which shadows this, so the
        # shim only ever applies to a class that never set it at all.
        if not hasattr(PreTrainedModel, "all_tied_weights_keys"):
            PreTrainedModel.all_tied_weights_keys = {}

        self.name = f"vlm:{model_id.split('/')[-1]}"
        self.model_id = model_id
        self.tile = tile
        self.max_tiles = max_tiles
        self.tok = AutoTokenizer.from_pretrained(
            model_id, trust_remote_code=True, use_fast=False)
        self.model = AutoModel.from_pretrained(
            model_id, dtype=torch.bfloat16, trust_remote_code=True,
            low_cpu_mem_usage=True).eval().to(device)
        self._torch = torch
        self.revision = getattr(self.model.config, "_commit_hash", None)

    def _transform(self):
        import torchvision.transforms as T
        from torchvision.transforms.functional import InterpolationMode
        return T.Compose([
            T.Lambda(lambda im: im.convert("RGB")),
            T.Resize((self.tile, self.tile),
                     interpolation=InterpolationMode.BICUBIC),
            T.ToTensor(),
            T.Normalize(mean=self.IMAGENET_MEAN, std=self.IMAGENET_STD)])

    def _tiles(self, image):
        """InternVL's dynamic tiling: pick the aspect ratio, then cut."""
        w, h = image.size
        ar = w / h
        best, best_diff = (1, 1), float("inf")
        for n in range(1, self.max_tiles + 1):
            for i in range(1, n + 1):
                if n % i:
                    continue
                j = n // i
                diff = abs(ar - i / j)
                if diff < best_diff:
                    best_diff, best = diff, (i, j)
        cols, rows = best
        resized = image.resize((self.tile * cols, self.tile * rows))
        out = []
        for k in range(cols * rows):
            box = ((k % cols) * self.tile, (k // cols) * self.tile,
                   ((k % cols) + 1) * self.tile, ((k // cols) + 1) * self.tile)
            out.append(resized.crop(box))
        if len(out) != 1:
            out.append(image.resize((self.tile, self.tile)))   # thumbnail
        return out

    def generate(self, system, user, image=None, unit=None, gt=None,
                 max_new_tokens=2048):
        from PIL import Image
        img = Image.open(image).convert("RGB")
        tf = self._transform()
        pv = self._torch.stack([tf(t) for t in self._tiles(img)])
        pv = pv.to(self._torch.bfloat16).to(self.model.device)
        # The system prompt is folded into the question: `model.chat` takes a
        # single string, and setting `model.system_message` would mutate shared
        # state across units.
        question = f"<image>\n{system}\n\n{user}"
        t0 = time.time()
        with self._torch.no_grad():
            reply = self.model.chat(
                self.tok, pv, question,
                dict(max_new_tokens=max_new_tokens, do_sample=False))
        n_gen = len(self.tok(reply).input_ids)
        return (reply, n_gen >= max_new_tokens - 2,
                {"generated_tokens": n_gen, "tiles": int(pv.shape[0]),
                 "seconds": round(time.time() - t0, 2),
                 "token_count": "re-tokenised from the reply; model.chat "
                                "returns no ids"})


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run(args) -> int:
    units = find_units(args.corpus, args.modality, args.images,
                       gt_glob=args.gt_glob, plans=args.plans)
    if args.limit:
        step = max(1, len(units) // args.limit)
        units = units[::step][:args.limit]
    if not units:
        print(f"no units under {args.corpus}")
        return 1

    cfg = {"corpus": args.corpus, "gt_glob": args.gt_glob,
           "plans": args.plans, "modality": args.modality,
           "backend": args.backend, "model_id": args.model_id,
           "oracle_anchor": args.oracle_anchor if args.backend == "oracle" else None,
           "mask_labels": bool(args.mask_labels),
           "no_think_template": bool(
               MODELS.get(args.model_id or "", {}).get("no_think_template")),
           "max_new_tokens": args.max_new_tokens,
           "relations_scored": list(X.EXTRACT_RELATIONS),
           "scope": "one storey per call; vertical links out of scope on both "
                    "the prediction and the reference"}
    man = RunManifest(f"extract:{args.backend}:{args.modality}",
                      expected=[u["unit"] for u in units], config=cfg)

    if args.backend == "oracle":
        backend = OracleBackend(args.oracle_anchor)
    elif args.backend == "replay":
        if not args.replay:
            print("--backend replay needs --replay <provenance json>")
            return 1
        backend = ReplayBackend(args.replay)
    elif args.backend == "llm":
        name = args.model_id or "glm-9b"
        backend = TextBackend(
            resolve_model(name, "text"),
            no_think=bool(MODELS.get(name, {}).get("no_think_template")))
    else:
        vid = resolve_model(args.model_id or "glm-9b-v", "vision")
        # InternVL3 needs its own path -- see InternVLBackend for why the
        # generic AutoModelForImageTextToText route does not work for it.
        backend = (InternVLBackend(vid) if "InternVL" in vid
                   else VisionBackend(vid))

    rows, provenance = [], []
    status_n, t_start = {}, time.time()
    for i, u in enumerate(units, 1):
        if u.get("skip"):
            man.missing(u["unit"], reason=u["skip"])
            continue
        gt_full = SCHEMA.load_gt(u["gt_path"])
        if not gt_full.usable:
            man.excluded(u["unit"],
                         reason=gt_full.excluded_because or "reference unusable")
            continue
        # The reference is narrowed to the same storey the model was shown, and
        # records what that cost -- see GroundTruth.restrict.
        #
        # Which key names a storey is dialect-dependent: the generator writes an
        # integer index, the deriver and the annotator a storey GlobalId. Both
        # are tried rather than assumed, because assuming one silently yields an
        # empty restriction -- every room missing, precision undefined -- on the
        # other corpus.
        key = u["storey_index"]
        rids = gt_full.rooms_on_storey(key)
        if not rids and u["storey"].get("global_id"):
            key = u["storey"]["global_id"]
            rids = gt_full.rooms_on_storey(key)
        if not rids:
            man.missing(u["unit"],
                        reason=f"reference states no rooms on storey "
                               f"{u['storey_index']} (tried index and "
                               f"global_id {u['storey'].get('global_id')!r})")
            continue
        gt = gt_full.restrict(rids, reason=f"storey {key}")

        marks = None
        if args.modality in X.SOM_MODALITIES:
            marks = marks_for(u)
            m2g = marks_to_gt(marks, gt)
            if not m2g:
                man.missing(u["unit"],
                            reason=f"none of the {len(marks)} marks resolved "
                                   f"to a reference room")
                continue
            # The model was shown these rooms and no others, so the reference
            # is narrowed to them. Charging it for a room that carried no mark
            # would measure room recovery, which this condition deliberately
            # hands over -- see render_blind.render_som_png on what the marks
            # give away.
            gt = gt.restrict(set(m2g.values()),
                             reason=f"storey {key}; {len(m2g)} marked rooms")
            if len(m2g) < len(marks):
                marks = [mk for mk in marks if mk["index"] in m2g]

        tr = transform_for(args.modality, u["storey"])
        if u["image"] and tr:
            # The transform must describe THIS image. If it does not, every
            # pixel answer is lifted on the wrong grid and the cell reads
            # 0.000 while looking perfectly scorable -- the occupancy raster
            # (~30 px/m, 579x396) against the plan render (~89.6 px/m,
            # 1800x1262) is exactly that mistake, and an oracle cannot catch
            # it because it projects and lifts through the same function.
            from PIL import Image as _Im
            with _Im.open(u["image"]) as _im:
                real = _im.size
            if real != (tr["width"], tr["height"]):
                man.failed(u["unit"],
                           reason=f"transform/image mismatch: transform "
                                  f"describes {tr['width']}x{tr['height']} but "
                                  f"{os.path.basename(u['image'])} is "
                                  f"{real[0]}x{real[1]}; a pixel answer would "
                                  f"be lifted on the wrong grid")
                continue
        system, user, wants_img = X.build_prompt(
            args.modality, u["storey"], mask_labels=args.mask_labels,
            image_size=((tr["width"], tr["height"]) if tr else None))
        phash = hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:16]

        try:
            kw = {}
            if args.backend == "oracle":
                kw["transform"] = transform_for(args.modality, u["storey"])
                kw["marks"] = marks
            raw, hit_cap, meta = backend.generate(
                system, user, image=u["image"], unit=u["unit"], gt=gt,
                max_new_tokens=args.max_new_tokens, **kw)
        except Exception as e:                       # noqa: BLE001
            man.failed(u["unit"], reason=f"{type(e).__name__}: {e}")
            continue
        if raw is None:
            man.missing(u["unit"], reason="backend produced no response")
            continue

        ex = X.parse_response(raw, hit_cap=hit_cap)
        status_n[ex.status] = status_n.get(ex.status, 0) + 1
        g = X.to_graph(ex, transform=transform_for(args.modality, u["storey"]),
                       elevation=u["storey"].get("elevation_m"),
                       storey_gid=f"ST-{u['storey_index']}", marks=marks)
        m = MATCH.match_rooms(g, gt)
        if m.unscorable:
            # Loud, and out of the average. A score computed here would
            # describe the harness or the reference, not the model.
            man.failed(u["unit"], reason=f"unscorable: {m.unscorable}")
            provenance.append({"unit": u["unit"], "prompt_sha256": phash,
                               "extraction": ex.summary(), "raw": raw,
                               "hit_cap": hit_cap, "unscorable": m.unscorable,
                               **meta})
            continue

        conn = SCORE.score_connectivity(g, gt, m, relations=X.EXTRACT_RELATIONS)
        lab = SCORE.score_labels(g, gt, m)
        conn["unit"] = u["unit"]
        conn["extraction_status"] = ex.status
        conn["label_accuracy"] = lab["accuracy"]
        conn["label_accuracy_strict"] = lab["accuracy_strict"]
        conn["label_scored"] = lab["scored"]
        conn["label_scored_matched"] = lab["scored_matched"]
        conn["label_correct"] = lab["correct"]
        conn["label_pred_missing"] = lab["pred_missing"]
        conn["label_pred_unmapped"] = lab["pred_unmapped"]
        rows.append(conn)
        man.ok(u["unit"], extraction_status=ex.status,
               rooms_pred=len(ex.rooms), rooms_ref=len(gt.rooms))
        provenance.append({
            "unit": u["unit"], "prompt_sha256": phash,
            "extraction": ex.summary(), "raw": raw, "hit_cap": hit_cap,
            "connectivity": {k: conn[k] for k in
                             ("tp", "fn", "fp_on_reference_negative",
                              "precision", "recall", "f1")},
            "labels": {k: lab[k] for k in ("scored", "correct", "accuracy")},
            "gt_scope": gt.scope, **meta})

        if i % max(1, args.progress_every) == 0 or i == len(units):
            el = time.time() - t_start
            print(f"  [{i}/{len(units)}] {el/60:.1f} min, "
                  f"{el/i:.1f} s/unit, ~{el/i*(len(units)-i)/60:.0f} min left "
                  f"  statuses={status_n}", flush=True)

    # ---- report ----------------------------------------------------------
    print(f"\nbackend: {getattr(backend, 'name', args.backend)}   "
          f"modality: {args.modality}   "
          f"labels: {'masked' if args.mask_labels else 'given'}")
    print(man.report())
    print(f"\n  extraction statuses: {status_n}")

    if rows:
        agg = SCORE.pool(rows)
        n_lab = sum(r["label_scored"] for r in rows)
        n_lab_ok = sum(r["label_correct"] for r in rows)
        print(f"\n  connectivity, micro-averaged over {agg['buildings']} storeys")
        print(f"    precision        {agg['precision']:.3f}")
        print(f"    recall           {agg['recall']:.3f}")
        print(f"    F1               {agg['f1']:.3f}")
        print(f"    precision_strict {agg['precision_strict']:.3f}")
        # Specificity is first-class in this project, not an afterthought: a
        # method that joins everything reaches high recall and is worthless,
        # and the annotated real reference cannot measure it at all (all 53 of
        # its negatives are pipeline proposals). The synthetic corpus states
        # explicit adjacent-not-connected negatives, so here it is computable
        # and must be printed beside recall rather than left in the JSON.
        neg = agg["gt_negatives"]
        if neg:
            spec = (neg - agg["fp_on_reference_negative"]) / neg
            print(f"    specificity      {spec:.3f}  "
                  f"({neg - agg['fp_on_reference_negative']}/{neg} reference "
                  f"negatives left unjoined)")
        else:
            print("    specificity      n/a (this reference states no negatives)")
        print(f"    tp {agg['tp']}  fn {agg['fn']}  "
              f"fp_on_negative {agg['fp_on_reference_negative']}  "
              f"out_of_scope {agg['pred_out_of_scope']}  "
              f"unmatched_endpoint {agg['pred_unmatched_endpoint']}")
        print(f"    rooms: matched {agg['rooms_matched']}, "
              f"missed {agg['rooms_unmatched_gt']}, "
              f"invented {agg['rooms_unmatched_pred']}")
        if n_lab:
            n_match = sum(r["label_scored_matched"] for r in rows)
            print(f"\n  labels, over every labelled reference room "
                  f"(comparable):      {n_lab_ok}/{n_lab} = {n_lab_ok/n_lab:.3f}")
            print(f"  labels, over rooms the prediction reached "
                  f"(NOT comparable):  {n_lab_ok}/{n_match} = "
                  f"{(n_lab_ok/n_match if n_match else 0):.3f}")
            if n_match < n_lab:
                print(f"    {n_lab - n_match} labelled reference room(s) had no "
                      f"matching prediction at all")
        else:
            print("\n  labels: nothing scorable")
    else:
        print("\n  no unit produced a scorable prediction")

    out_dir = args.out or "results/extract"
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, output_tag(
        args.corpus, args.backend, args.model_id, args.modality,
        bool(args.mask_labels), args.oracle_anchor))
    man.write(f"{base}.manifest.json")
    with open(f"{base}.provenance.json", "w") as fh:
        json.dump({"config": {**cfg,
                              "backend_name": getattr(backend, "name",
                                                       args.backend),
                              "model_revision": getattr(backend, "revision",
                                                         None)},
                   "predictions": provenance}, fh, indent=2)
    pooled = SCORE.pool(rows) if rows else None
    if pooled and pooled.get("gt_negatives"):
        n = pooled["gt_negatives"]
        pooled["specificity"] = round(
            (n - pooled["fp_on_reference_negative"]) / n, 5)
    with open(f"{base}.scores.json", "w") as fh:
        json.dump({"config": cfg,
                   "pooled": pooled,
                   "extraction_statuses": status_n,
                   "per_unit": rows}, fh, indent=2)
    print(f"\nwrote {base}.{{scores,provenance,manifest}}.json")

    if args.backend == "oracle" and rows:
        agg = SCORE.pool(rows)
        perfect = (agg["precision"] == 1.0 and agg["recall"] == 1.0
                   and agg["rooms_unmatched_gt"] == 0
                   and agg["rooms_unmatched_pred"] == 0)
        print("\n" + ("ORACLE OK: the harness scores a perfect prediction as "
                      "perfect." if perfect else
                      "ORACLE FAILED: the reference scored against itself is "
                      "not 1.000, so the harness is wrong and no model number "
                      "taken through it means anything. Fix this first."))
        return 0 if perfect else 2
    return 0 if man.complete else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True,
                    help="corpus directory holding *.gt.json, floorplans/ and "
                         "vision/ (e.g. dataset/synthetic_data/synth150)")
    ap.add_argument("--modality", default="text", choices=MODALITIES)
    ap.add_argument("--backend", default="oracle",
                    choices=("oracle", "replay", "llm", "vlm"))
    ap.add_argument("--model-id", default=None,
                    help="a registry short name (" + ", ".join(MODELS)
                         + ") or a bare HuggingFace repo id")
    ap.add_argument("--oracle-anchor", default="identity",
                    choices=("identity", "metres", "pixels"),
                    help="which matching path the oracle exercises. 'identity' "
                         "joins by room id and tests no geometry at all; "
                         "'pixels' is the only mode that tests the pixel->metre "
                         "lift the vision conditions depend on")
    ap.add_argument("--images", default=None,
                    help="directory of renders; defaults to <corpus>/vision")
    ap.add_argument("--gt-glob", default=None,
                    help="glob for reference files; defaults to "
                         "<corpus>/*.gt.json. The real corpus keeps its "
                         "derived references in results/real_gt/*.realgt.json")
    ap.add_argument("--plans", default=None,
                    help="root holding <building>/<building>_floorplans.json; "
                         "defaults to <corpus>/floorplans")
    ap.add_argument("--replay", default=None,
                    help="a previous run's *.provenance.json, for --backend replay")
    ap.add_argument("--mask-labels", action="store_true",
                    help="withhold the architect's room names from the text "
                         "condition, so labelling is inferred rather than copied")
    ap.add_argument("--max-new-tokens", type=int, default=2048,
                    help="a whole storey's JSON is far longer than the one word "
                         "the per-room task needed, and a thinking model emits "
                         "its trace first")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--progress-every", type=int, default=10)
    ap.add_argument("--out", default=None)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
