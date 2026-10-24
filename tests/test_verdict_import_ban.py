"""Verdict-path import ban. This module does not import cv2, so a missing wheel cannot skip it."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam import imports_guard  # noqa: E402
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


def test_static_import_ban_catches_aliased_and_indirect_loaders(tmp_path: Path):
    """Forms that used to miss the scan are offenders. A literal ``exec`` of safe code is not."""
    service = tmp_path / "services" / "referee"
    service.mkdir(parents=True)
    samples = {
        "dotted.py": "import tests.redteam.x\n",
        "alias.py": "import importlib as il\nil.import_module('qor_receipt.vision.pan_estimator')\n",
        "getattr_call.py": "import importlib\ngetattr(importlib, 'import_module')('qor_receipt.vision.pan_estimator')\n",
        "getattr_alias.py": "import importlib as il\ngetattr(il, 'import_module')('qor_receipt.vision')\n",
        "getattr_bound.py": "import importlib\nload = getattr(importlib, 'import_module')\nload('qor_receipt.vision')\n",
        "getattr_dynamic.py": "import importlib\nname = 'import_module'\ngetattr(importlib, name)('json')\n",
        "exec_import.py": "exec('import qor_receipt.vision')\n",
        "exec_dynamic.py": "name = 'import os'\nexec(name)\n",
        "eval_import.py": "eval(\"__import__('qor_receipt.vision.pan_estimator')\")\n",
        "runpy_mod.py": "import runpy\nrunpy.run_path('x.py')\n",
        "spec.py": "import importlib.util\nimportlib.util.spec_from_file_location('m', 'x.py')\n",
        "spec_alias.py": "from importlib.util import spec_from_file_location as load\nload('m', 'x.py')\n",
        "builtins_dunder.py": "__builtins__.__import__('qor_receipt.vision.pan_estimator')\n",
        "clean_exec.py": "exec('x = 1')\n",
        "not_a_component.py": "import notredteam\n",
    }
    for name, text in samples.items():
        (service / name).write_text(text, encoding="utf-8")

    offenders = set(verdict_path_import_offenders(tmp_path))
    for name in samples:
        if name in {"clean_exec.py", "not_a_component.py"}:
            assert f"services/referee/{name}" not in offenders
        else:
            assert f"services/referee/{name}" in offenders

    doc = imports_guard.__doc__ or ""
    assert "C extension" in doc
    assert "does not recognize" in doc
    assert "os.fork" in doc and "os.execv" in doc
