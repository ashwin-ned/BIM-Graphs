"""The room-label vocabulary: one place, versioned, and honest about its gaps.

`main/bimsg/semantics.canonical()` answered a three-way question with two
answers. It returned `None` both for a string carrying no functional content
("Room", "Space", "Area") and for a real room function outside its taxonomy
("Studio", "Gym", "Classroom") -- and every caller dropped both. Measured over
the 208-model corpus: 1298 of 6323 IFC-named rooms (20.5%) vanished from every
semantic score with no trace, and the taxonomy's own blind spots were invisible
because they looked identical to unlabelled rooms.

The distinction matters for reporting. A room called "Room" has no ground truth
and excluding it is correct. A room called "Gym" has perfectly good ground truth
that this taxonomy cannot express, and excluding it silently overstates coverage.

So `classify()` returns one of four outcomes:

    CLASS       one of the twelve canonical classes
    UNNAMED     the string carries no functional information
    UNMAPPED    a real room function this taxonomy does not cover
    AMBIGUOUS   a combined-function room ("Living room / Kitchen")

The twelve classes are deliberately NOT extended here. `semantics.CANON` is what
the rule, LLM and VLM backends are told to predict into, so adding a class
silently changes what every backend is being asked. Extending the taxonomy is a
research decision; `report_gaps()` ranks the evidence for it.

Two resolution bugs are fixed along the way, both found by measurement:

  * Punctuation was never stripped, so "W.C. (Women)" did not match the key
    "wc". Worth 14 rooms.
  * Keyword matching took the first hit in `TAXONOMY` dict order, so
    "Store Front" resolved to `storage` (via "store") rather than `retail`.
    Longest-match was tried as a fix and rejected: it makes "Lift Shaft" resolve
    to `service` (via "shaft", 5 chars) instead of `stair` (via "lift", 4), and
    a lift is vertical circulation. Genuine conflicts are resolved by an
    explicit table instead, because there are only ten of them.

    python -m evaluation.labels --report        # rank the unmapped strings
"""

from __future__ import annotations

import re

# Bump when a mapping changes, so a score can state which vocabulary produced
# it. A semantic number is not comparable across versions.
VOCAB_VERSION = "labels-1.0.0 (2026-09-18)"

CLASS = "class"
UNNAMED = "unnamed"
UNMAPPED = "unmapped"
AMBIGUOUS = "ambiguous"

# The twelve canonical classes, in the order main/bimsg/semantics.py declares
# them. Kept identical on purpose -- see the module docstring.
CLASSES = (
    "bathroom", "bedroom", "kitchen", "living", "office", "meeting",
    "circulation", "stair", "storage", "service", "retail", "outdoor",
)

# Keyword -> class. Substring matched against the normalised forms of a label.
KEYWORDS = {
    "bathroom": ("bath", "wc", "toilet", "rest room", "restroom", "washroom",
                 "shower", "powder", "sanitary", "wash basin", "lavatory",
                 "ensuite", "en-suite", "urinal"),
    "bedroom": ("bedroom", "bed room", "master bed", "guest room", "dormitory"),
    "kitchen": ("kitchen", "pantry", "kitchenette", "scullery"),
    "living": ("living", "lounge", "family room", "drawing", "sitting",
               "dining", "recreation"),
    "office": ("office", "cabin", "workstation", "study", "desk", "workspace",
               "working space", "work room", "laborator"),
    "meeting": ("meeting", "conference", "boardroom", "seminar", "training"),
    "circulation": ("hall", "corridor", "lobby", "passage", "foyer",
                    "reception", "circulation", "vestibule", "entrance",
                    "waiting"),
    "stair": ("stair", "staircase", "escalator", "lift", "elevator", "mumti"),
    "storage": ("store", "storage", "closet", "wardrobe", "pantry store",
                "cupboard", "archive"),
    "service": ("utility", "plant", "mechanical", "electrical", "server",
                "machine", "duct", "shaft", "laundry", "janitor", "technical",
                "switch", "pump", "ahu", "riser"),
    "retail": ("shop", "retail", "store front", "cafeteria", "canteen", "cafe",
               "restaurant", "bar", "kiosk"),
    "outdoor": ("balcony", "terrace", "deck", "courtyard", "exterior",
                "open floor plate", "veranda", "patio"),
}

# Strings that name no function. Excluding these is correct -- there is no
# ground truth to score against -- but it must be counted separately from a
# taxonomy gap. Includes the German equivalents actually present in the corpus
# and one recurring typo ("Public Aera").
NO_FUNCTION = {
    "", "room", "room name", "rooms", "space", "spaces", "area", "aera",
    "public aera", "public area", "floor", "level", "zone", "unnamed",
    "no name", "n a", "na", "tbd", "x", "?", "-", "--",
    # German: Raum = room, Fläche = area, Zimmer = room, Geschoss = storey
    "raum", "raume", "räume", "flache", "fläche", "zimmer", "geschoss",
}

# Exact matches on a normalised string, applied BEFORE keyword matching.
# Highest precision and no heuristic. Three kinds live here:
#   1. typos of terms the keyword table already knows
#   2. non-English names present in the corpus
#   3. the ten genuine keyword conflicts, resolved explicitly
ALIASES = {
    # -- typos, all observed in dataset/2025_BIMfundamentals ----------------
    "storrage": "storage",
    "laundary": "service",
    "confrence room": "meeting",
    "dinning area": "living",
    "dinning room": "living",
    "electricroom": "service",
    "enterance": "circulation",
    "enterance lobby": "circulation",

    # -- German -------------------------------------------------------------
    "buro": "office", "büro": "office",
    "priv buro": "office", "priv büro": "office",
    "grossraumburo": "office", "grossraumbüro": "office",
    "treppenhaus": "stair", "treppe": "stair",
    "kuche": "kitchen", "küche": "kitchen",
    "bad": "bathroom",
    "flur": "circulation",
    "abstellraum": "storage",

    # -- conflicts, resolved explicitly -------------------------------------
    # "Conference Hall": a hall here is a large room, not a corridor. The
    # keyword "hall" is a false positive; the function is a meeting room.
    "conference hall": "meeting",
    "conference hall 1": "meeting", "conference hall 2": "meeting",
    "lecture hall": "meeting",
    # "toilet cabin": a WC cubicle. "cabin" -> office is the false positive.
    "toilet cabin": "bathroom",
    # "Office Lobby": a lobby serving offices is circulation.
    "office lobby": "circulation",
    # "Store Front" is retail; dict order used to give storage.
    "store front": "retail", "shop front": "retail",
    # Vertical transitions win over the space or service that contains them:
    # for a navigation graph the useful fact is that the storeys connect here.
    "lift shaft": "stair", "elevator shaft": "stair",
    "escalator hall": "stair", "elevator hall": "stair",
    "stair hall": "stair", "stair lobby": "stair",

    # -- laboratory ----------------------------------------------------------
    # REQUIRED for the primary corpus. main/generate_building.py:112 emits
    # "Lab" as one of its twelve room types, so without this 166 of 2049
    # synthetic rooms (8.1%) are unscorable -- and the old canonical() hid that
    # by returning None indistinguishably from an unnamed room. A lab is placed
    # in `office` as the nearest existing class: a room people work in at
    # benches. This is a placement inside the taxonomy, not an extension of it.
    "lab": "office", "labs": "office", "laboratory": "office",
    "laboratories": "office", "lab room": "office",

    # -- abbreviations observed in the corpus --------------------------------
    # "Corr. 1" and "Conf. 2" normalise to "corr"/"conf" once punctuation and
    # the trailing instance number are stripped.
    "corr": "circulation", "corrs": "circulation",
    "conf": "meeting", "confs": "meeting",

    # -- teaching rooms ------------------------------------------------------
    # Placed in `meeting`: a room where a group is addressed. Same kind of call
    # as "break room" -> living, and it covers ~31 rooms.
    "classroom": "meeting", "class room": "meeting",
    "lecture room": "meeting", "seminar room": "meeting",

    # -- missing synonyms ----------------------------------------------------
    "manager room": "office",
    "working stations": "office", "work stations": "office",
    "printing room": "service",
    "dressing room": "storage",
    "quiet room": "office",
    "break room": "living",
    "w i c": "storage", "wic": "storage",      # walk-in closet
    "sink area": "service",
}

# Genuinely combined-function rooms. A single-label taxonomy cannot express
# these, and guessing one half would be inventing ground truth, so they are
# reported as ambiguous and left out of a single-label score.
AMBIGUOUS_PAIRS = (
    ("kitchen", "living"),
)

_PUNCT = re.compile(r"[.\-_/\\()\[\],;:'\"&+]+")
_WS = re.compile(r"\s+")
_TRAILING_INDEX = re.compile(r"\s+\d+$")
_BARE_ID = re.compile(r"^(?:room|space|r|s|a)[\s_]*\d+$")


def normalise(label) -> list[str]:
    """Candidate normalised forms of a raw label, most literal first.

    Two punctuation treatments are produced because neither alone is right:
    removing punctuation turns "W.C." into "wc" (which the keyword table knows)
    while replacing it with a space turns "Kitchen/Living" into two words
    (which keyword matching needs). Both are tried.
    """
    if label is None:
        return []
    t = _WS.sub(" ", str(label)).strip().lower()
    if not t:
        return []
    forms = [t]
    forms.append(_WS.sub(" ", _PUNCT.sub(" ", t)).strip())     # -> space
    forms.append(_WS.sub(" ", _PUNCT.sub("", t)).strip())      # -> removed
    # "Work Room 2", "Class Room 3" -- a trailing index is an instance number,
    # not a function.
    forms += [_TRAILING_INDEX.sub("", f) for f in list(forms)]
    out = []
    for f in forms:
        if f and f not in out:
            out.append(f)
    return out


def classify(label) -> tuple[str, str | None, str]:
    """(outcome, class or None, why).

    `why` names the rule that fired, so a disagreement can be traced to one
    table entry rather than to "the matcher".
    """
    forms = normalise(label)
    if not forms:
        return (UNNAMED, None, "empty")

    for f in forms:
        if f in NO_FUNCTION:
            return (UNNAMED, None, f"no-function term {f!r}")
        if _BARE_ID.match(f):
            return (UNNAMED, None, f"bare identifier {f!r}")

    for f in forms:
        if f in ALIASES:
            return (CLASS, ALIASES[f], f"alias {f!r}")

    hits: dict[str, str] = {}
    for f in forms:
        for cls, keys in KEYWORDS.items():
            for k in keys:
                if k in f and cls not in hits:
                    hits[cls] = k
    if not hits:
        return (UNMAPPED, None, "no keyword matched")
    if len(hits) == 1:
        cls = next(iter(hits))
        return (CLASS, cls, f"keyword {hits[cls]!r}")

    got = tuple(sorted(hits))
    for pair in AMBIGUOUS_PAIRS:
        if got == tuple(sorted(pair)):
            return (AMBIGUOUS, None,
                    f"combined function: {' + '.join(got)} via "
                    + ", ".join(f"{hits[c]!r}" for c in got))
    # An unresolved conflict is not silently broken by a heuristic -- it is
    # surfaced so an entry can be added to ALIASES.
    return (AMBIGUOUS, None,
            f"unresolved conflict {' + '.join(got)} via "
            + ", ".join(f"{hits[c]!r}" for c in got))


def canonical(label) -> str | None:
    """Backwards-compatible view: the class, or None for anything else.

    Kept so existing callers keep working, but prefer `classify()` -- this
    signature is exactly the one that lost 1298 rooms without saying so.
    """
    outcome, cls, _ = classify(label)
    return cls if outcome == CLASS else None


def tally(labels) -> dict:
    """Outcome counts over an iterable of raw labels."""
    out = {CLASS: 0, UNNAMED: 0, UNMAPPED: 0, AMBIGUOUS: 0}
    per_class: dict[str, int] = {}
    for lab in labels:
        outcome, cls, _ = classify(lab)
        out[outcome] += 1
        if cls:
            per_class[cls] = per_class.get(cls, 0) + 1
    out["per_class"] = dict(sorted(per_class.items(), key=lambda kv: -kv[1]))
    out["vocab_version"] = VOCAB_VERSION
    return out


def report_gaps(labels, top: int = 40) -> list[tuple[str, int, str]]:
    """The strings this vocabulary cannot express, ranked by room count.

    This is the evidence for whether to extend the taxonomy. It is a research
    decision, so the list is printed rather than acted on.
    """
    from collections import Counter
    gaps: Counter = Counter()
    why: dict[str, str] = {}
    for lab in labels:
        outcome, _, reason = classify(lab)
        if outcome in (UNMAPPED, AMBIGUOUS):
            key = str(lab).strip()
            gaps[key] += 1
            why[key] = reason
    return [(s, n, why[s]) for s, n in gaps.most_common(top)]


def _main() -> int:
    import argparse
    import glob
    import json
    import os

    ap = argparse.ArgumentParser(description="Report label-vocabulary coverage.")
    ap.add_argument("--plans", default="dataset/2025_BIMfundamentals/plans",
                    help="directory of *.plan.json")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    labels = []
    for p in sorted(glob.glob(os.path.join(a.plans, "*.plan.json"))):
        with open(p) as fh:
            doc = json.load(fh)
        for st in doc.get("storeys") or []:
            for r in st.get("rooms") or []:
                # Only rooms the IFC actually named. A recovered room has no
                # label and never had one, so including it in the denominator
                # understates coverage -- which is how 79.5% was once reported
                # as 66.9%.
                if r.get("source") == "ifc":
                    labels.append(r.get("label"))

    t = tally(labels)
    n = len(labels)
    print(f"{VOCAB_VERSION}")
    print(f"{n} IFC-stated rooms\n")
    for k in (CLASS, UNNAMED, UNMAPPED, AMBIGUOUS):
        print(f"  {k:10s} {t[k]:6d}  {100 * t[k] / n:5.1f}%")
    scorable = t[CLASS]
    have_gt = n - t[UNNAMED]
    print(f"\n  of the {have_gt} rooms that carry a real name, "
          f"{scorable} ({100 * scorable / have_gt:.1f}%) map to a class")
    print(f"\n  per class: {t['per_class']}")

    gaps = report_gaps(labels, a.top)
    if gaps:
        print(f"\n  taxonomy gaps, ranked (a research decision, not a bug):")
        for s, c, reason in gaps:
            print(f"    {c:5d}  {s!r:34s} {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
