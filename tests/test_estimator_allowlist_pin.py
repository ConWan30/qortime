"""Pin, allowlist, and import-ban checks.

This module has no module-level skip. Dropping the numpy pin must not hide these
checks, and when ``CI=true`` an off-pin numpy fails instead of skipping.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

from qor_receipt.vision import estimator_allowlist as al

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from redteam.imports_guard import verdict_path_import_offenders  # noqa: E402


def numpy_pin_disposition(installed: str, required: str, ci: str | None) -> str:
    """``run`` on the pin, ``fail`` in CI when it differs, otherwise ``skip``."""
    if installed == required:
        return "run"
    if ci == "true":
        return "fail"
    return "skip"


def test_numpy_pin_disposition_fails_in_ci_and_skips_locally():
    required = al.PINNED_NUMPY_REQUIREMENT.split("==", 1)[1]
    assert numpy_pin_disposition(required, required, "true") == "run"
    assert numpy_pin_disposition(required, required, None) == "run"
    assert numpy_pin_disposition("9.9.9", required, "true") == "fail"
    assert numpy_pin_disposition("9.9.9", required, "false") == "skip"
    assert numpy_pin_disposition("9.9.9", required, None) == "skip"


def test_allowlist_is_a_fixed_frozenset_of_lowercase_hex():
    assert type(al.KNOWN_ESTIMATOR_SHA256) is frozenset and al.KNOWN_ESTIMATOR_SHA256
    for sha in al.KNOWN_ESTIMATOR_SHA256:
        assert type(sha) is str and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)


def test_estimator_known_takes_only_params():
    pytest.importorskip("cv2")
    from qor_receipt.vision import pan_estimator as pe

    # estimator_known takes only parameters; it never accepts a hash from the caller.
    assert list(inspect.signature(pe.estimator_known).parameters) == ["params"]


def test_pinned_wheel_matches_the_constraints_file():
    from qor_receipt.vision import pan_estimator as pe

    text = (ROOT / "tests" / "constraints-pan-estimator.txt").read_text(encoding="utf-8")
    pins = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert pe.PINNED_OPENCV_WHEEL in pins
    assert pe.PINNED_OPENCV_WHEEL.startswith("opencv-python-headless==" + pe.PINNED_OPENCV_VERSION + ".")
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert pe.PINNED_OPENCV_WHEEL in project["optional-dependencies"]["dev"]
    # The referee image installs .[referee,vision]; neither extra carries the exact pin.
    for extra in ("referee", "vision"):
        assert pe.PINNED_OPENCV_WHEEL not in project["optional-dependencies"][extra]
    assert al.PINNED_NUMPY_REQUIREMENT in pins
    assert al.PINNED_NUMPY_REQUIREMENT in project["optional-dependencies"]["dev"]
    for extra in ("referee", "vision"):
        assert al.PINNED_NUMPY_REQUIREMENT not in project["optional-dependencies"][extra]


def test_live_tree_import_ban_has_no_module_level_skip():
    assert verdict_path_import_offenders(ROOT) == []


def test_opencv_matches_the_pin():
    """CI installs the pinned wheel through the dev extra. Fails on any other OpenCV build."""
    cv2 = pytest.importorskip("cv2")
    from qor_receipt.vision import pan_estimator as pe

    assert pe.opencv_matches_pin(), (
        f"OpenCV {cv2.__version__} is not {pe.PINNED_OPENCV_WHEEL}; install with -c tests/constraints-pan-estimator.txt"
    )
    assert pe.OpticalFlowPanEstimator().identity()["opencv_pinned"] is True


def test_running_estimator_hash_is_on_the_allowlist():
    """Runs in both CI jobs (3.11 and 3.13). Off-pin numpy fails when CI=true."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from qor_receipt.vision import pan_estimator as pe

    required = al.PINNED_NUMPY_REQUIREMENT.split("==", 1)[1]
    disposition = numpy_pin_disposition(np.__version__, required, os.environ.get("CI"))
    if disposition == "fail":
        pytest.fail(
            f"numpy {np.__version__} is not {al.PINNED_NUMPY_REQUIREMENT}. "
            "CI must fail when the pin is dropped, not skip.",
            pytrace=False,
        )
    if disposition == "skip":
        pytest.skip(
            f"numpy {np.__version__} is not {al.PINNED_NUMPY_REQUIREMENT}. "
            "Install the dev extra to check the running estimator hash."
        )
    sha = pe.estimator_sha256()
    assert sha in al.KNOWN_ESTIMATOR_SHA256, (
        f"estimator sha256 {sha} on CPython {sys.version_info[0]}.{sys.version_info[1]} "
        f"(numpy {np.__version__}) is not in estimator_allowlist.py"
    )
    assert pe.estimator_known() is True
    ident = pe.OpticalFlowPanEstimator().identity()
    assert ident["estimator_known"] is True and ident["opencv_pinned"] is True
