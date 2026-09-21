"""Semantic injection: naming the rooms IFC never named.

344 of the 1,025 rooms in the corpus carry no label, and all of them are rooms
we recovered geometrically -- IFC cannot name what it did not model. Language
goals make this load-bearing rather than cosmetic: "go to the kitchen" fails if
the kitchen is `room_7`.

Signal used is the room's graph context, not pixels: area and shape, the named
elements inside it, windows, what it connects to, and the labels of its
labelled neighbours. This matches the evidence -- vision-language models are
measurably weakest at exactly the architectural configuration reasoning a floor
plan would demand -- and the published work on this problem (GNNs over element
context, language models over IFC text) is graph- and text-based, not visual.

Three backends share one interface:

  RuleBackend   hand-written priors over the features. No model, always
                available, the number a learned approach must beat -- and also
                the deterministic prior handed to the LLM, not a competitor
                run alongside it.
  LLMBackend    GLM-4-9B-0414 reading a system-framed prompt: taxonomy,
                deterministic evidence, the rule's own guess as a hypothesis to
                confirm or override.
  VLMBackend    GLM-4.1V-9B-Thinking, same backbone, reading a rendered plan.
                Included because it was asked for; expected to trail the other
                two per the evidence above, and measured rather than assumed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter

log = logging.getLogger(__name__)

# This module reaches for the top-level `evaluation` package below. Every
# caller before today only needed `bimsg` itself, so entry points such as
# main/eval_semantics.py, main/eval_goal_resolution.py, main/label_corpus.py,
# main/validate.py and main/demo.py add only their own directory (main/) to
# sys.path, not the project root that `evaluation/` lives beside. Rather than
# patch five call sites (and the next one someone adds), the module that
# introduced the dependency takes responsibility for satisfying it.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# --------------------------------------------------------------------------
# Canonical taxonomy
#
# The corpus uses 153 distinct label strings for what are perhaps a dozen real
# room functions -- "wc", "toilet", "rest room" and "bathroom" all name the same
# thing. Scoring raw strings would measure an exporter's vocabulary rather than
# a method's understanding, so both prediction and ground truth are normalised.
# --------------------------------------------------------------------------

# The room-function vocabulary lives in ONE place. This module used to carry its
# own copy plus its own `canonical()`, and that copy had three measured defects:
# "Store Front" resolved to `storage` because matching took the first hit in dict
# order, "W.C. (Women)" matched nothing because punctuation was never stripped,
# and "Lab" -- one of the twelve room types main/generate_building.py emits --
# had no keyword at all, so 166 of 2049 rooms in the primary corpus were
# silently unscorable. Worse, `canonical()` returned None both for a name with
# no function ("Room") and for a real function outside the taxonomy ("Gym"), so
# a taxonomy gap was indistinguishable from an unlabelled room.
#
# `evaluation/labels.py` fixes all of that and is versioned. These names are
# re-exported so existing callers keep working; prefer `labels.classify()` in
# new code, because it returns the distinction this signature cannot express.
from evaluation.labels import (          # noqa: F401
    CLASSES as _CLASSES,
    KEYWORDS as TAXONOMY,
    canonical,
)

CANON = list(_CLASSES)


# --------------------------------------------------------------------------
# Furniture/fitting name -> what it implies about room function.
#
# Revit furniture family names are noisy ("M_Bed-Standard:1372 x 1905mm -
# 1905mm", "Furniture_Cabinet_File_Lateral") but the first token is almost
# always the real category. Matched as substrings against a cleaned lowercase
# name; several keywords may fire per room and are returned as evidence, not
# collapsed to one guess, so the caller (rule or LLM) makes the final call.
# --------------------------------------------------------------------------

FURNITURE_HINTS = {
    "bedroom": ("bed", "nightstand", "wardrobe", "dresser"),
    # "dining" is not itself a taxonomy class (canonical() folds it into
    # "living"); a prior version kept it as its own hint key, so any room
    # whose strongest evidence was a dining table returned "dining" -- a
    # label that can never match a canonical truth value, guaranteed wrong
    # by construction rather than by weak evidence.
    "living": ("sofa", "couch", "tv", "entertainment", "coffee table",
              "armchair", "lounge", "dining table", "dining chair"),
    "office": ("desk", "task chair", "office chair", "cabinet-file",
              "filing", "workstation", "monitor"),
    # "küchenzeile" (German: fitted kitchen unit) covers real family names in
    # this corpus that the English-only keywords below never matched.
    "kitchen": ("range", "stove", "refrigerator", "sink-kitchen", "hob",
               "oven", "cooktop", "cabinet-kitchen", "küchenzeile", "kitchen"),
    "meeting": ("conference", "boardroom"),
    "bathroom": ("toilet", "wc-", "bathtub", "shower", "vanity", "sink",
                "urinal", "bidet"),
}


def furniture_hints(names: list) -> Counter:
    """Category evidence from furniture/fitting names in one room."""
    hits = Counter()
    for raw in names:
        t = re.sub(r"[:_]", " ", str(raw)).lower()
        for cat, keys in FURNITURE_HINTS.items():
            if any(k in t for k in keys):
                hits[cat] += 1
    return hits


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def room_features(g, node) -> dict:
    """Everything known about a room from its place in the graph."""
    d = g.nodes[node]
    contents = Counter()
    names = []
    n_windows = 0
    for _, v, e in g.out_edges(node, data=True):
        if e.get("relation") != "contains":
            continue
        vd = g.nodes[v]
        t = vd.get("ifc_type")
        if not t:
            continue
        contents[t.replace("Ifc", "")] += 1
        if t == "IfcWindow":
            n_windows += 1
        lab = vd.get("label")
        # Curtain-wall members etc. carry no useful name; keep the list to
        # furniture-like elements, which is where names actually discriminate.
        if lab and t in ("IfcFurniture", "IfcSanitaryTerminal"):
            names.append(lab)

    neighbours, rel_counts = [], Counter()
    for _, v, e in g.out_edges(node, data=True):
        rel = e.get("relation")
        if rel in ("connected_by_door", "open_passage", "vertically_connected"):
            rel_counts[rel] += 1
            c = canonical(g.nodes[v].get("label"))
            if c:
                neighbours.append(c)

    storey = None
    for u, _, e in g.in_edges(node, data=True):
        if e.get("relation") == "contains" and g.nodes[u].get("layer") == "storey":
            storey = g.nodes[u].get("label")

    return {
        "area_m2": d.get("area"),
        "height_m": d.get("height"),
        "storey": storey,
        "elevation_m": d.get("elevation"),
        "is_ground_floor": (d.get("elevation") or 1) < 0.5,
        "n_windows": n_windows,
        "contains": dict(contents.most_common(8)),
        "element_names": names[:8],
        "furniture_hints": dict(furniture_hints(names)),
        "connects_to": dict(Counter(neighbours).most_common(6)),
        "n_doors": rel_counts.get("connected_by_door", 0),
        "n_open_passages": rel_counts.get("open_passage", 0),
        "n_vertical": rel_counts.get("vertically_connected", 0),
        "source": d.get("source"),
    }


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class RuleBackend:
    """Hand-written priors over the features.

    Doubles as the deterministic prior handed to the LLM: its guess and the
    evidence for it are given to the model as a hypothesis to confirm or
    override, not computed independently and compared after the fact. A
    learned model that cannot beat this on its own is not earning its
    dependency, and one that ignores it when it is confidently right is
    throwing away a free signal.
    """

    name = "rule"

    def predict(self, feats: dict) -> str:
        return self.predict_with_evidence(feats)[0]

    def predict_with_evidence(self, feats: dict) -> tuple[str, str]:
        a = feats.get("area_m2") or 0.0
        c = feats.get("contains") or {}
        nb = feats.get("connects_to") or {}
        fh = feats.get("furniture_hints") or {}
        deg = feats["n_doors"] + feats["n_open_passages"]

        if fh:
            top, n = Counter(fh).most_common(1)[0]
            if n >= 1:
                return top, f"furniture named as {top} ({n} item(s))"
        if feats["n_vertical"] > 0 or "Stair" in c or "StairFlight" in c:
            return "stair", "vertical connection or stair element present"
        if "SanitaryTerminal" in c:
            return "bathroom", "contains a sanitary fitting"
        if deg >= 4 and a >= 15:
            return "circulation", f"{deg} connections is high for its {a:.0f} m2"
        if a < 6:
            kind = "bathroom" if deg <= 1 else "storage"
            return kind, f"very small ({a:.1f} m2)"
        if c.get("Furniture", 0) >= 6 and a >= 20:
            return "living", "heavily furnished, large"
        if a >= 40 and deg >= 2:
            return "office", f"large ({a:.0f} m2) with multiple connections"
        if nb.get("bedroom") and a < 12:
            return "bathroom", "small room off a bedroom"
        if a >= 12:
            return "office", "no stronger signal; moderate area"
        return "storage", "no stronger signal; small area"


SYSTEM_PROMPT = """You are a building-information assistant that assigns \
functional labels to rooms recovered from an IFC (Industry Foundation \
Classes) building model.

Context you must understand about this task:
- Some rooms in the source file already have a name given by the architect \
who designed the building; those are never sent to you.
- The rooms you are asked about were found by measuring free floor space, \
because the file itself never named them. Your job is to infer, from what \
surrounds and fills the room, what a person would call it.
- The strongest evidence is usually the named furniture or fittings inside \
the room (a bed means bedroom, a sanitary fitting means bathroom) and what \
kind of rooms it connects to. Room area alone is weak evidence and should \
only break ties.
- A deterministic geometric heuristic has already produced a candidate label \
with its reasoning. Treat it as a hypothesis: adopt it when the evidence \
supports it, override it when the room's contents clearly indicate something \
else.

You must answer with exactly one word, chosen from the fixed list you are \
given, and nothing else -- no punctuation, no explanation."""

USER_PROMPT = """Allowed labels: {taxonomy}

Deterministic hypothesis: "{hyp}"  (reason: {reason})

Room facts:
{facts}

Neighbouring rooms it opens onto: {neighbours}

Respond with exactly one word from the allowed labels."""


class LLMBackend:
    """GLM-4-9B-0414 reading a system-framed prompt with a deterministic prior."""

    def __init__(self, model_id="zai-org/GLM-4-9B-0414", device="cuda",
                 max_new_tokens=16):
        # 16, not 6. The taxonomy words are 1-3 tokens, but a model that
        # prefixes anything at all ("The answer is bathroom") needs headroom,
        # and at 6 tokens such a reply was cut before the class word and then
        # silently scored as the rule prior -- see `predict`.
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch
        self.name = f"llm:{model_id.split('/')[-1]}"
        self.tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, device_map=device,
            trust_remote_code=True)
        self.model.eval()
        self.max_new_tokens = max_new_tokens
        self._torch = torch
        self.prior = RuleBackend()

    def predict(self, feats: dict):
        """Returns (label_or_None, status), same contract as VLMBackend.

        This backend had the identical scoring defect: it ended with
        `_coerce(ans, fallback=hyp)`, so any reply that did not contain a
        taxonomy word was credited with the *rule backend's* answer. The
        18 September 2026 figure of 0.556 was reported as a clean measurement
        before that was noticed; it is not known how often the fallback fired,
        because nothing counted it. It is counted now.
        """
        hyp, reason = self.prior.predict_with_evidence(feats)
        facts = {k: v for k, v in feats.items()
                 if k not in ("connects_to", "source", "furniture_hints")
                 and not k.startswith("_")
                 and v not in (None, {}, 0, [])}
        prompt = USER_PROMPT.format(
            taxonomy=", ".join(CANON), hyp=hyp, reason=reason,
            facts=json.dumps(facts, indent=1),
            neighbours=", ".join(feats.get("connects_to") or {}) or "none")
        msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
        text = self.tok.apply_chat_template(msgs, tokenize=False,
                                            add_generation_prompt=True)
        ids = self.tok(text, return_tensors="pt").to(self.model.device)
        with self._torch.no_grad():
            out = self.model.generate(**ids, max_new_tokens=self.max_new_tokens,
                                      do_sample=False,
                                      pad_token_id=self.tok.eos_token_id)
        gen = out[0][ids["input_ids"].shape[1]:]
        ans = self.tok.decode(gen, skip_special_tokens=True)
        label = _coerce_strict(ans)
        if label:
            return label, "ok"
        # No taxonomy word in the reply. Distinguish "ran out of room" from
        # "answered, but not with a class" so the two are not pooled.
        hit_cap = len(gen) >= self.max_new_tokens
        return None, ("truncated" if hit_cap else "unparseable")


VLM_SYSTEM_PROMPT = """You are a building-information assistant. You are \
shown a top-down floor plan with one room highlighted. Assign it exactly one \
functional label from the given list, using the room's shape, size and \
position relative to the rest of the plan. Answer with exactly one word and \
nothing else."""


class VLMBackend:
    """GLM-4.1V-9B-Thinking reading a rendered plan of the room in context.

    Same backbone as LLMBackend, which makes the text-vs-vision comparison
    fair rather than confounded by model family. Included because it was
    requested; expected to trail the other two per our own measurement that
    VLMs are weakest at architectural configuration reasoning, and per the
    published semantic-enrichment literature being graph/text-based rather
    than visual. Measured, not assumed.
    """

    def __init__(self, model_id="zai-org/GLM-4.1V-9B-Thinking", device="cuda"):
        from transformers import AutoProcessor, AutoModelForImageTextToText
        import torch
        self.name = f"vlm:{model_id.split('/')[-1]}"
        self.proc = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, device_map=device,
            trust_remote_code=True)
        self.model.eval()
        self._torch = torch
        self.prior = RuleBackend()

    def predict_image(self, image, feats: dict, max_new_tokens: int = 1536):
        """Returns (label_or_None, status) where status is one of:

            "ok"           the model closed its trace and named a class
            "truncated"    hit the token cap mid-thought; no answer exists
            "unparseable"  closed the trace but said nothing in the taxonomy

        In the two failure cases the label is **None**. The caller must count
        that as a non-answer; nothing is substituted for it.

        Three defects were fixed here on 21 September 2026, all of which
        inflated the reported score by silently crediting the model with the
        deterministic prior's answer:

        1. `max_new_tokens` was 300. GLM-4.1V-9B-Thinking emits an unconditional
           `<think>...</think>` trace (there is no way to disable it -- the
           chat template has no toggle), and on a measured full run **95 of 592
           rooms (16.0%) never closed the trace inside 300 tokens**. Measured
           over 24 rooms at a 1200-token cap, the trace closes at a median of
           85 tokens but with a long right tail: p90 245, max 1004. A cap of
           300 therefore sits inside the tail, which is exactly the 16% seen.
           The default is now 1536 -- above the observed maximum with headroom
           -- and it is a parameter, because the right value is a property of
           the model and should not be buried. Raising it is nearly free: the
           generation stops at EOS, so the 90% of rooms that finish in under
           250 tokens cost the same as before.

        2. On truncation the old code set `after = ans`, i.e. the *entire
           partial reasoning trace*, and ran `_coerce` over it. A trace reading
           "this could be a bathroom, though the fixtures suggest..." would
           yield `bathroom` -- scoring the model's thinking-aloud as its
           answer. Only text after `</think>` is read now.

        3. `_coerce(after, fallback=hyp)` fell back to the rule backend's own
           hypothesis. That is what made an earlier "VLM accuracy" equal the
           rule baseline's to three decimals on the same sample. Worse, it also
           fired when the trace *did* close but the answer was unparseable --
           a case the old `was_truncated` flag did not even report, so it was
           invisible in the results. Both cases now return None and are
           counted separately.
        """
        hyp, reason = self.prior.predict_with_evidence(feats)
        prompt = (f"Allowed labels: {', '.join(CANON)}. "
                  f"Deterministic hypothesis: {hyp} ({reason}). "
                  f"Room area: {feats.get('area_m2')} m2. "
                  f"Think briefly, then answer with exactly one word.")
        msgs = [{"role": "system", "content": VLM_SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "image"},
                                             {"type": "text", "text": prompt}]}]
        text = self.proc.apply_chat_template(msgs, add_generation_prompt=True)
        inp = self.proc(text=[text], images=[image],
                        return_tensors="pt").to(self.model.device)
        with self._torch.no_grad():
            out = self.model.generate(**inp, max_new_tokens=max_new_tokens,
                                      do_sample=False,
                                      pad_token_id=self.proc.tokenizer.eos_token_id)
        gen = out[:, inp["input_ids"].shape[1]:]
        hit_cap = gen.shape[1] >= max_new_tokens
        ans = self.proc.batch_decode(gen, skip_special_tokens=True)[0]

        if "</think>" not in ans:
            # No closing tag: the trace never finished, so there is no answer
            # to read. Whether or not the cap was the cause, nothing was said.
            return None, ("truncated" if hit_cap else "unparseable")
        after = ans.split("</think>")[-1]
        label = _coerce_strict(after)
        return (label, "ok") if label else (None, "unparseable")


def _coerce(text: str, fallback: str = "unknown") -> str:
    """Force a free-form answer onto the taxonomy, or return `fallback`.

    DO NOT pass the deterministic hypothesis as `fallback` when scoring a
    model. Doing so was how a "VLM accuracy" over 200 rooms came to match the
    rule backend's accuracy on the same sample to three decimal places: it was
    the rule backend, relabelled. A model that does not answer must be charged
    for not answering, not credited with the prior's answer.

    `_coerce_strict` is the version to use in an evaluation path.
    """
    t = (text or "").strip().lower()
    t = re.split(r"[\n,.;:]", t)[0].strip()
    if t in CANON:
        return t
    for c in CANON:
        if c in t:
            return c
    return canonical(t) or fallback


def _coerce_strict(text: str):
    """Map a model's answer onto the taxonomy, or None if it did not give one.

    No fallback, by design. The caller is expected to count None as a
    non-answer rather than substitute anything for it.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    t = re.split(r"[\n,.;:]", t)[0].strip()
    if t in CANON:
        return t
    for c in CANON:
        if c in t:
            return c
    return canonical(t)


# --------------------------------------------------------------------------

def predict_label(backend, feats):
    """(label_or_None, status) from any backend, normalising the two shapes.

    `RuleBackend.predict` returns a bare string -- it is deterministic priors
    and always answers. `LLMBackend.predict` returns `(label_or_None, status)`
    because a model can decline or emit junk. Callers go through this so
    neither shape can be written into a graph or a scorer by mistake: an
    earlier version of this change had `label_rooms` storing the literal
    tuple `("bathroom", "ok")` as a room's predicted label.
    """
    res = backend.predict(feats)
    if isinstance(res, tuple):
        return res
    return (res, "ok")


def label_rooms(g, backend, only_unlabelled: bool = True) -> dict:
    """Assign a functional label to rooms that lack one.

    Predictions are written to `predicted_label` and the provenance recorded,
    so a consumer can always tell an inferred name from a stated one.
    """
    out = {}
    for n, d in g.nodes(data=True):
        if d.get("layer") != "space":
            continue
        if only_unlabelled and canonical(d.get("label")) is not None:
            continue
        feats = room_features(g, n)
        try:
            pred, status = predict_label(backend, feats)
        except Exception as e:
            log.warning("labelling failed for %s: %s", n, e)
            continue
        if pred is None:
            # The model declined or emitted nothing usable. Leave the room
            # unlabelled rather than recording the deterministic prior's guess
            # under the model's name.
            log.info("no usable label for %s (%s)", n, status)
            continue
        out[n] = pred
        d["predicted_label"] = pred
        d["label_source"] = getattr(backend, "name", "unknown")
    log.info("semantics: labelled %d rooms via %s", len(out),
             getattr(backend, "name", "?"))
    return out
