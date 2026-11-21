"""Verdict-path import ban. This module does not import cv2, so a missing wheel cannot skip it."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.imports_guard import verdict_path_import_offenders  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_live_tree_has_no_verdict_path_import_offenders():
    assert verdict_path_import_offenders(ROOT) == []


def test_services_scripts_and_unparsable_files_are_offenders(tmp_path: Path):
    service = tmp_path / "services" / "worker"
    service.mkdir(parents=True)
    (service / "plug.py").write_text("from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator\n", encoding="utf-8")
    (service / "clean.py").write_text("import json\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "broken.py").write_bytes(b"\xff\xfe not utf-8")
    (scripts / "syntax.py").write_text("def (\n", encoding="utf-8")
    (scripts / "vision_mod.py").write_text("from qor_receipt import vision\n", encoding="utf-8")
    (scripts / "variable.py").write_text(
        "import importlib\nname = 'qor_receipt.vision.pan_estimator'\nimportlib.import_module(name)\n",
        encoding="utf-8",
    )
    (scripts / "redteam_mod.py").write_text("import redteam.optical_flow\n", encoding="utf-8")
    (scripts / "pan_estimate_clips.py").write_text(
        "from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator\n",
        encoding="utf-8",
    )
    (scripts / "clean.py").write_text("import json\n", encoding="utf-8")
    outside = tmp_path / "eval"
    outside.mkdir()
    (outside / "referee_side.py").write_text(
        "import importlib\nimportlib.import_module('qor_receipt.vision.pan_estimator')\n",
        encoding="utf-8",
    )

    offenders = verdict_path_import_offenders(tmp_path)
    assert "services/worker/plug.py" in offenders
    assert "services/worker/clean.py" not in offenders
    assert "scripts/broken.py" in offenders
    assert "scripts/syntax.py" in offenders
    assert "scripts/vision_mod.py" in offenders
    assert "scripts/variable.py" in offenders
    assert "scripts/redteam_mod.py" in offenders
    assert "scripts/clean.py" not in offenders
    # The estimator CLI may import the vision package. A non-literal import still fails.
    assert "scripts/pan_estimate_clips.py" not in offenders
    assert "eval/referee_side.py" in offenders

    (scripts / "pan_estimate_clips.py").write_text(
        "import importlib\nname = 'qor_receipt.vision'\nimportlib.import_module(name)\n",
        encoding="utf-8",
    )
    assert "scripts/pan_estimate_clips.py" in verdict_path_import_offenders(tmp_path)
