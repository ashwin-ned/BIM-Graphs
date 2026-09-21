"""The evaluation contract: one definition of what ground truth is.

Scoring was previously spread across four scripts that disagreed with each
other and with the annotator. `evaluate.py` recognised a verdict vocabulary the
annotator had stopped writing years of work ago; `eval_connectivity.py` required
a `adjacent_not_connected` key the human export never emitted; room matching
allowed several recovered rooms to stand for one reference room, so an invented
room and its invented edge cost a method nothing.

This package holds the pieces those scripts now share:

    schema     -- read any of the three ground-truth dialects into one record
    matching   -- assign predicted rooms to reference rooms, one to one
    scoring    -- score connectivity in the matched space, keeping the
                  predictions that did not match anywhere
    manifest   -- a run's expected outputs, so a missing one is a failure
                  rather than a smaller denominator
"""

from .schema import GT_SCHEMA_VERSION, GroundTruth, Room, load_gt   # noqa: F401
from .matching import Matching, match_rooms                          # noqa: F401
from .scoring import f1, score_labels, pool, score_connectivity                     # noqa: F401
from .manifest import RunManifest                                    # noqa: F401
