"""Known-good ``estimator_sha256()`` values for the pan estimator.

A fixed frozenset of exact 64-character lowercase hex digests. It is read only by
``pan_estimator.estimator_known()``, which recomputes the hash in-process; no
function accepts a hash from a caller.

Recorded with the pinned environment (tests/constraints-pan-estimator.txt and the
``dev`` extra): ``opencv-python-headless==5.0.0.93``, ``numpy==2.4.6``, and
``cv2.useOptimized()`` True. The hash does not include the Python version.
numpy 2.4.6 ships wheels for both CPython 3.11 and 3.13, so both CI interpreters
produce the same hash when numpy and OpenCV match. Without the numpy pin, CI
resolved 2.4.6 on 3.11 and 2.5.4 on 3.13 and the hashes differed.

This file is separate from ``pan_estimator.py`` because the estimator hash covers
that module's source. Any edit to ``pan_estimator.py``, its parameters, OpenCV,
or numpy needs the entry re-recorded; ``tests/test_pan_estimator.py`` prints the
running hash when it is missing. Being on this list unlocks nothing: the mismatch
gate still has no allow path.
"""
from __future__ import annotations

# Must match tests/constraints-pan-estimator.txt and the `dev` extra (a test checks).
PINNED_NUMPY_REQUIREMENT = "numpy==2.4.6"

KNOWN_ESTIMATOR_SHA256: frozenset[str] = frozenset({
    # CPython 3.11 and 3.13, numpy 2.4.6, opencv-python-headless 5.0.0.93.
    # The digest does not include the Python version.
    # Re-recorded after the hashed source said so.
    "51e3a92b826719ed7b919cf18dc5344efd9f3d5decf5976ad15b87203f648b69",
})
