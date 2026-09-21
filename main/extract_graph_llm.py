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

Four backends. Two of them exist to keep the harness honest:

    oracle   emits the reference itself, through the real parser and the real
             scorer. It must score 1.000. When it does not, the harness is
             broken, not the model -- and every model number taken through a
             broken harness would have been unfalsifiable. Run it first.
    replay   re-scores raw responses from an earlier run's provenance log, so a
             scorer fix does not cost the GPU hours again.
    llm      a text model (default GLM-4-9B-0414)
    vlm      a vision model (default GLM-4.1V-9B-Thinking)

    # verify the harness before spending anything on it
    python main/extract_graph_llm.py --corpus dataset/synthetic_data/synth150 \\
        --backend oracle --modality text --limit 10

    python main/extract_graph_llm.py --corpus dataset/synthetic_data/synth150 \\
        --backend vlm --modality plan --limit 20 --out results/extract/
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
from render_blind import storey_slug

MODALITIES = ("text", "plan", "occ", "text+plan")


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
            if modality in ("plan", "occ", "text+plan"):
                kind = "occ" if modality == "occ" else "plan"
                p = os.path.join(img_dir, f"{name}_{slug}_{kind}.png")
                if not os.path.exists(p):
                    u["skip"] = f"no {kind} render at {p}"
                else:
                    u["image"] = p
            if modality in ("plan", "text+plan") and not st.get("transform"):
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
                 max_new_tokens=0, transform=None):
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
                if self.anchor == "pixels":
                    if not transform:
                        continue
                    s = transform["px_per_m"]
                    room["centroid_px"] = [round(s * cx + transform["tx"], 2),
                                           round(-s * cy + transform["ty"], 2)]
                else:
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

    def __init__(self, model_id="zai-org/GLM-4-9B-0414", device="cuda"):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch
        self.name = f"llm:{model_id.split('/')[-1]}"
        self.model_id = model_id
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
        text = self.tok.apply_chat_template(msgs, tokenize=False,
                                            add_generation_prompt=True)
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
        text = self.proc.apply_chat_template(msgs, add_generation_prompt=True)
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
        backend = TextBackend(args.model_id or "zai-org/GLM-4-9B-0414")
    else:
        backend = VisionBackend(args.model_id
                                or "zai-org/GLM-4.1V-9B-Thinking")

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

        system, user, wants_img = X.build_prompt(
            args.modality, u["storey"], mask_labels=args.mask_labels)
        phash = hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:16]

        try:
            kw = {}
            if args.backend == "oracle":
                kw["transform"] = u["storey"].get("transform")
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
        g = X.to_graph(ex, transform=u["storey"].get("transform"),
                       elevation=u["storey"].get("elevation_m"),
                       storey_gid=f"ST-{u['storey_index']}")
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
        conn["label_scored"] = lab["scored"]
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
        print(f"    tp {agg['tp']}  fn {agg['fn']}  "
              f"fp_on_negative {agg['fp_on_reference_negative']}  "
              f"out_of_scope {agg['pred_out_of_scope']}  "
              f"unmatched_endpoint {agg['pred_unmatched_endpoint']}")
        print(f"    rooms: matched {agg['rooms_matched']}, "
              f"missed {agg['rooms_unmatched_gt']}, "
              f"invented {agg['rooms_unmatched_pred']}")
        if n_lab:
            print(f"\n  labels: {n_lab_ok}/{n_lab} = {n_lab_ok/n_lab:.3f}")
        else:
            print("\n  labels: nothing scorable")
    else:
        print("\n  no unit produced a scorable prediction")

    out_dir = args.out or "results/extract"
    os.makedirs(out_dir, exist_ok=True)
    tag = (f"{args.backend}"
           f"{'-' + args.oracle_anchor if args.backend == 'oracle' else ''}"
           f"_{args.modality.replace('+', '-')}"
           f"{'_masked' if args.mask_labels else ''}")
    corpus_tag = os.path.basename(args.corpus.rstrip("/"))
    base = os.path.join(out_dir, f"{corpus_tag}_{tag}")
    man.write(f"{base}.manifest.json")
    with open(f"{base}.provenance.json", "w") as fh:
        json.dump({"config": {**cfg,
                              "backend_name": getattr(backend, "name",
                                                       args.backend),
                              "model_revision": getattr(backend, "revision",
                                                         None)},
                   "predictions": provenance}, fh, indent=2)
    with open(f"{base}.scores.json", "w") as fh:
        json.dump({"config": cfg,
                   "pooled": SCORE.pool(rows) if rows else None,
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
    ap.add_argument("--model-id", default=None)
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
