"""The annotation vocabulary, and what each word means to a scorer.

`evaluate.py` recognised `correct` / `spurious`. The annotator writes `real` /
`not_a_room` for rooms and `passable` / `not_passable` for links, and has done
for the whole of the current annotation effort. Nothing raised an error: every
verdict simply failed to match, so two real rooms and a passable edge produced
zero judged items and a blank score. A vocabulary mismatch that scores zero
instead of failing is the worst kind, because the output looks like a result.

There is one vocabulary. `annotator/compose.py` carries its own literal copy so
that it stays dependency-free for its browser twin; `test_m0_regressions.py`
asserts the three copies agree, which is what makes "one vocabulary" true
rather than merely intended.

Five outcomes, and the difference between the last two is the one that matters:

    POSITIVE  the reviewer confirmed it
    NEGATIVE  the reviewer rejected it -- evidence, and the only thing that
              makes precision measurable
    PARTIAL   real, but wrongly delimited (a merge or split request). Neither a
              hit nor a miss; it needs geometry the annotator cannot edit.
    UNKNOWN   the reviewer looked and could not say
    UNJUDGED  nobody looked. Not a negative. Silence is not evidence.
"""

from __future__ import annotations

POSITIVE, NEGATIVE, PARTIAL, UNKNOWN, UNJUDGED = (
    "positive", "negative", "partial", "unknown", "unjudged")

ROOM_VERDICTS = ("real", "not_a_room", "unsure", "merge", "split")
LINK_VERDICTS = ("passable", "not_passable", "unsure")

ROOM_OUTCOME = {
    "real": POSITIVE,
    "not_a_room": NEGATIVE,
    "merge": PARTIAL,
    "split": PARTIAL,
    "unsure": UNKNOWN,
}

LINK_OUTCOME = {
    "passable": POSITIVE,
    "not_passable": NEGATIVE,
    "unsure": UNKNOWN,
}

# Verdict words this project has used and then stopped using. Reading one is a
# sign of an annotation file older than the current vocabulary, and it must be
# migrated rather than silently scored as nothing.
RETIRED = {"correct": "real", "spurious": "not_a_room"}


class RetiredVerdict(ValueError):
    """An annotation still uses a word the tool no longer writes."""


def room_outcome(entry: dict, strict: bool = True) -> str:
    return _outcome(entry, ROOM_OUTCOME, "room", strict)


def link_outcome(entry: dict, strict: bool = True) -> str:
    return _outcome(entry, LINK_OUTCOME, "link", strict)


def _outcome(entry: dict, table: dict, kind: str, strict: bool) -> str:
    v = (entry or {}).get("verdict")
    if v is None:
        return UNJUDGED
    if v in table:
        return table[v]
    if v in RETIRED:
        if strict:
            raise RetiredVerdict(
                f"{kind} verdict {v!r} is from the retired vocabulary; "
                f"the current word is {RETIRED[v]!r}. Migrate the file with "
                f"annotator/migrate_verdicts.py rather than scoring it as if "
                f"it said nothing.")
        return table.get(RETIRED[v], UNJUDGED)
    # A word from neither vocabulary. Treated as unjudged, which is what the
    # composer does, so the two cannot disagree about what was reviewed.
    return UNJUDGED
