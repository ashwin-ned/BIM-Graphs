"""Check a model that does not answer is charged, not credited with the prior.

This is the third time this project has had a metric that silently reported a
deterministic baseline's output as a model's. The first two shipped. The
mechanism each time was a fallback that looked defensive and was in fact a
scoring error:

    label = _coerce(after, fallback=hyp)     # hyp = the RULE backend's answer

So a VLM that emitted nothing usable scored exactly as well as the rule
baseline, and the published "VLM accuracy" over 200 rooms matched the rule
backend's accuracy on the same sample to three decimal places. A full run on
18 September 2026 measured the rate at which this fired: **95 of 592 rooms
(16.0%)**.

`VLMBackend.predict_image` now returns `(None, status)` for a non-answer.
These tests stub the processor and model so the accounting is verified without
loading 20 GB of weights, and each one is written to FAIL against the old
behaviour — the whole point is that the guard has been shown to fire.

    python tests/test_vlm_answer_accounting.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "main"))

from bimsg import semantics as S       # noqa: E402

failures = 0


def ok(msg: str, cond: bool) -> None:
    global failures
    if not cond:
        failures += 1
    print(("  PASS " if cond else "  FAIL ") + msg)


# --------------------------------------------------------------------------
# Stubs. Only the handful of calls predict_image actually makes.
# --------------------------------------------------------------------------

class _FakeTokenizer:
    eos_token_id = 0


class _FakeProc:
    """Returns a scripted generation, and reports a chosen generated length."""

    def __init__(self, reply: str, gen_len: int):
        self.reply = reply
        self.gen_len = gen_len
        self.tokenizer = _FakeTokenizer()

    def apply_chat_template(self, msgs, add_generation_prompt=True):
        return "prompt"

    def __call__(self, text=None, images=None, return_tensors=None):
        return self

    def to(self, device):
        return self

    def __getitem__(self, key):
        # inp["input_ids"].shape[1] -- the prompt length.
        return _FakeTensor(shape=(1, 5))

    def keys(self):
        return ["input_ids"]

    def batch_decode(self, gen, skip_special_tokens=True):
        return [self.reply]


class _FakeTensor:
    def __init__(self, shape):
        self.shape = shape

    def __getitem__(self, idx):
        # out[:, prompt_len:] -- hand back a tensor of the generated length.
        return self


class _FakeModel:
    device = "cpu"

    def __init__(self, gen_len: int):
        self.gen_len = gen_len

    def generate(self, **kw):
        return _FakeGen(prompt_len=5, gen_len=self.gen_len)

    def eval(self):
        return self


class _FakeGen:
    def __init__(self, prompt_len, gen_len):
        self.shape = (1, prompt_len + gen_len)
        self._gen_len = gen_len

    def __getitem__(self, idx):
        return _FakeTensor(shape=(1, self._gen_len))


class _FakeNoGrad:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


class _FakeTorch:
    @staticmethod
    def no_grad():
        return _FakeNoGrad()


def backend_returning(reply: str, gen_len: int):
    """A VLMBackend whose model emits `reply`, bypassing __init__/weights."""
    b = object.__new__(S.VLMBackend)
    b.name = "vlm:stub"
    b.proc = _FakeProc(reply, gen_len)
    b.model = _FakeModel(gen_len)
    b._torch = _FakeTorch()
    b.prior = S.RuleBackend()
    return b


FEATS = {"label": None, "area_m2": 12.0, "n_doors": 1, "n_open_passages": 0,
         "n_vertical": 0, "n_windows": 1, "neighbours": [], "contents": {},
         "furniture": [], "area": 12.0, "elongation": 1.2, "storey_index": 0}
CAP = 64


def main() -> int:
    # The rule backend's own answer for these features. If any non-answer case
    # returns this, the old defect is back.
    hyp, _ = S.RuleBackend().predict_with_evidence(FEATS)
    print(f"  (the rule prior would say {hyp!r} for this room)\n")

    # ---- the answer path -------------------------------------------------
    b = backend_returning("<think>a small room with a window</think>bathroom", 30)
    label, status = b.predict_image(object(), FEATS, max_new_tokens=CAP)
    ok(f"a closed trace with a valid class is accepted ({label!r}, {status!r})",
       label == "bathroom" and status == "ok")

    # ---- truncation: hit the cap, trace never closed ---------------------
    b = backend_returning("<think>this could be a bathroom, or perhaps a", CAP)
    label, status = b.predict_image(object(), FEATS, max_new_tokens=CAP)
    ok(f"a truncated trace yields NO answer, not the prior ({label!r}, {status!r})",
       label is None and status == "truncated")
    ok("...and specifically does not return the rule prior's label",
       label != hyp)
    # The old code ran _coerce over the whole partial trace, so the word
    # "bathroom" appearing inside the model's *reasoning* became its answer.
    ok("...and does not mine the reasoning trace for a class word",
       label != "bathroom")

    # ---- closed trace, but nothing in the taxonomy -----------------------
    # This case was invisible before: `was_truncated` was False, so it was
    # never reported, yet _coerce still substituted the prior.
    b = backend_returning("<think>hard to tell</think>I am not sure about this",
                          30)
    label, status = b.predict_image(object(), FEATS, max_new_tokens=CAP)
    ok(f"an unparseable answer yields NO answer ({label!r}, {status!r})",
       label is None and status == "unparseable")
    ok("...and is reported distinctly from truncation",
       status != "truncated")
    ok("...and does not return the rule prior's label", label != hyp)

    # ---- an empty generation ---------------------------------------------
    b = backend_returning("", 0)
    label, status = b.predict_image(object(), FEATS, max_new_tokens=CAP)
    ok(f"an empty generation yields NO answer ({label!r}, {status!r})",
       label is None)

    # ---- _coerce_strict never substitutes --------------------------------
    ok("_coerce_strict returns None on junk",
       S._coerce_strict("I cannot tell") is None)
    ok("_coerce_strict returns None on empty",
       S._coerce_strict("") is None and S._coerce_strict(None) is None)
    ok("_coerce_strict still maps a real answer",
       S._coerce_strict("bathroom") == "bathroom")
    ok("_coerce_strict maps through the shared vocabulary",
       S._coerce_strict("WC") == "bathroom")

    # ---- the old permissive helper is still available, and still dangerous
    # It is kept because non-evaluation callers use it, but it must not be
    # reachable from a scoring path. Assert the eval path does not call it.
    # Parse it rather than grepping it. The docstring of predict_image quotes
    # the old defective line verbatim in order to explain it, and an earlier
    # version of this check matched that quotation and reported a false
    # failure -- the same trap as grepping for a comment that documents the
    # absence of the thing it names.
    import ast
    src = open(os.path.join(ROOT, "main", "bimsg", "semantics.py")).read()
    fn = next(node for node in ast.walk(ast.parse(src))
              if isinstance(node, ast.FunctionDef) and node.name == "predict_image")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    ok(f"predict_image does not call the fallback-taking _coerce "
       f"(calls: {sorted(called)})",
       "_coerce" not in called)
    ok("predict_image uses _coerce_strict instead",
       "_coerce_strict" in called)

    # ---- the default cap was raised --------------------------------------
    import inspect
    sig = inspect.signature(S.VLMBackend.predict_image)
    cap = sig.parameters["max_new_tokens"].default
    ok(f"the default token cap is well above the old 300 ({cap})", cap >= 1024)

    print()
    return failures


if __name__ == "__main__":
    n = main()
    print(f"FAIL {n} problem(s) in the VLM answer accounting" if n else
          "PASS a non-answer is charged as a non-answer, never as the prior")
    sys.exit(1 if n else 0)
