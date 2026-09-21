"""Check every source IFC is intact before anything is built from it.

`dataset/test_set/model_2.ifc` was a half-finished copy: 12,845,056 bytes, which
is exactly 3136 x 4096, ending mid-number with no `END-ISO-10303-21;`. It was a
byte-exact prefix of the real file and was missing 8 of its 19 doors.

Nothing noticed. IfcOpenShell opened it without raising, reported a plausible
6 storeys and 35 spaces, the pipeline built a graph from it, and a human spent
an afternoon annotating a building with a third of its doors missing -- which
became one of the ten headline ground-truth buildings. The failure mode is the
one this project keeps meeting: it scores rather than errors.

So the corpus gets a manifest, and the manifest is checked before a run. A
truncated file is a hard failure, not a smaller building.

    python main/verify_corpus.py --write dataset/test_set        # once, to record
    python main/verify_corpus.py dataset/test_set                # before a run
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys

MANIFEST = "corpus.manifest.json"

# A STEP file must end with this. Trailing whitespace is tolerated; anything
# else means the file was cut off mid-write.
TAIL = b"END-ISO-10303-21;"
HEAD = b"ISO-10303-21;"


def digest(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def structural_check(path: str):
    """Is this a complete STEP file? Returns None if fine, else why not.

    Checked without parsing, because the parser is exactly what fails to
    complain: this has to catch the file before IfcOpenShell is asked to
    interpret it.
    """
    size = os.path.getsize(path)
    if size == 0:
        return "the file is empty"
    with open(path, "rb") as fh:
        head = fh.read(len(HEAD))
        if head != HEAD:
            return f"does not start with {HEAD.decode()} (got {head[:13]!r})"
        # The footer is short; read the last 4 KiB and look for it there.
        fh.seek(max(0, size - 4096))
        tail = fh.read()
    if TAIL not in tail:
        return (f"does not end with {TAIL.decode()} — the file is truncated "
                f"({size} bytes"
                + (f", exactly {size // 4096} x 4096, so a partial copy"
                   if size % 4096 == 0 else "") + ")")
    return None


def scan(directory: str) -> dict:
    out = {}
    for p in sorted(glob.glob(os.path.join(directory, "*.ifc"))):
        name = os.path.basename(p)
        out[name] = {"bytes": os.path.getsize(p), "sha256": digest(p)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory")
    ap.add_argument("--write", action="store_true",
                    help="record the current state as the manifest, after "
                         "checking every file is structurally complete")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.directory, "*.ifc")))
    if not files:
        print(f"no *.ifc in {args.directory}")
        return 1

    # Structural completeness first, and for every file, whether or not a
    # manifest exists. A recorded hash of a truncated file is still truncated.
    broken = []
    for p in files:
        why = structural_check(p)
        if why:
            broken.append((os.path.basename(p), why))

    mpath = os.path.join(args.directory, MANIFEST)

    if args.write:
        if broken:
            print(f"refusing to record a manifest: {len(broken)} file(s) are "
                  f"not complete IFC files")
            for name, why in broken:
                print(f"  {name}: {why}")
            return 1
        with open(mpath, "w") as fh:
            json.dump({"directory": os.path.basename(args.directory.rstrip("/")),
                       "files": scan(args.directory)}, fh, indent=1, sort_keys=True)
        print(f"recorded {len(files)} file(s) -> {mpath}")
        return 0

    problems = list(broken)

    if os.path.exists(mpath):
        with open(mpath) as fh:
            recorded = (json.load(fh) or {}).get("files") or {}
        seen = set()
        for p in files:
            name = os.path.basename(p)
            seen.add(name)
            want = recorded.get(name)
            if want is None:
                problems.append((name, "is not in the manifest"))
                continue
            if os.path.getsize(p) != want["bytes"]:
                problems.append((name, f"is {os.path.getsize(p)} bytes, "
                                       f"manifest says {want['bytes']}"))
                continue
            if digest(p) != want["sha256"]:
                problems.append((name, "has the recorded size but different "
                                       "contents"))
        for name in sorted(set(recorded) - seen):
            problems.append((name, "is in the manifest but missing from disk"))
    else:
        print(f"note: no {MANIFEST} here, so only structural checks ran. "
              f"Record one with --write.")

    if problems:
        print(f"FAIL {len(problems)} problem(s) in {args.directory}")
        for name, why in sorted(problems):
            print(f"  {name}: {why}")
        return 1

    print(f"PASS {len(files)} IFC file(s) in {args.directory} are complete"
          f"{' and match the manifest' if os.path.exists(mpath) else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
