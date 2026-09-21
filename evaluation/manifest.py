"""A run's expected outputs, so that a missing one is a failure.

Every scorer in this project globbed for whatever files happened to exist and
averaged over those. A building that crashed during graph construction then left
the corpus silently: the denominator shrank, and the reported score improved.
The convenience runner made this easy to do by accident -- it suppressed stage
failures and printed DONE regardless.

A manifest fixes the denominator before the work starts. Every expected unit is
declared up front and must end in exactly one state:

    ok       -- produced and scored
    missing  -- expected, never produced. Stays in the denominator.
    failed   -- produced an error, with the reason kept
    excluded -- deliberately out of scope, with a stated reason

`missing` and `failed` are not the same as `excluded`, and only `excluded` may
leave a denominator -- which is why it has to carry a reason someone can argue
with.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time

MANIFEST_VERSION = 1
STATES = ("ok", "missing", "failed", "excluded")


def _git_revision(path: str = ".") -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


class RunManifest:
    """Declare the expected units, record what happened to each, then report."""

    def __init__(self, name: str, expected=(), config: dict | None = None):
        self.name = name
        self.started = time.time()
        self.config = dict(config or {})
        self.expected = list(dict.fromkeys(expected))
        self.rows = {}

    # -- recording --------------------------------------------------------
    def record(self, unit: str, state: str, reason: str | None = None,
               **extra) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; expected one of {STATES}")
        if state == "excluded" and not reason:
            raise ValueError("an excluded unit must say why it was excluded")
        if unit not in self.expected:
            self.expected.append(unit)
        self.rows[unit] = {"unit": unit, "state": state, "reason": reason, **extra}

    def ok(self, unit: str, **extra):
        self.record(unit, "ok", **extra)

    def missing(self, unit: str, reason: str = "no output produced", **extra):
        self.record(unit, "missing", reason, **extra)

    def failed(self, unit: str, reason: str, **extra):
        self.record(unit, "failed", reason, **extra)

    def excluded(self, unit: str, reason: str, **extra):
        self.record(unit, "excluded", reason, **extra)

    # -- reporting --------------------------------------------------------
    def finalise(self) -> None:
        """Anything declared but never recorded is missing, not absent."""
        for unit in self.expected:
            if unit not in self.rows:
                self.rows[unit] = {"unit": unit, "state": "missing",
                                   "reason": "declared but never recorded"}

    def counts(self) -> dict:
        self.finalise()
        c = {s: 0 for s in STATES}
        for r in self.rows.values():
            c[r["state"]] += 1
        return c

    @property
    def complete(self) -> bool:
        c = self.counts()
        return c["missing"] == 0 and c["failed"] == 0

    def denominator(self) -> int:
        """Units a rate should be computed over: everything not excluded."""
        c = self.counts()
        return c["ok"] + c["missing"] + c["failed"]

    def to_dict(self) -> dict:
        self.finalise()
        return {
            "manifest_version": MANIFEST_VERSION,
            "run": self.name,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git_revision": _git_revision(),
            "config": self.config,
            "counts": self.counts(),
            "complete": self.complete,
            "denominator": self.denominator(),
            "units": [self.rows[u] for u in self.expected],
        }

    def write(self, path: str) -> str:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path

    def report(self) -> str:
        c = self.counts()
        lines = [f"run {self.name}: {c['ok']} ok, {c['missing']} missing, "
                 f"{c['failed']} failed, {c['excluded']} excluded "
                 f"(denominator {self.denominator()})"]
        for r in (self.rows[u] for u in self.expected):
            if r["state"] in ("missing", "failed"):
                lines.append(f"  !! {r['unit']}: {r['state']} -- {r['reason']}")
            elif r["state"] == "excluded":
                lines.append(f"  -- {r['unit']}: excluded -- {r['reason']}")
        if not self.complete:
            lines.append("  these units stay in the denominator; a score that "
                         "omits them is not a score for this corpus")
        return "\n".join(lines)
