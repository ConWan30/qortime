"""Lightweight, synthetic-only tests for the PP-OCRv6 scorebug spike (eval/ocr_spike).

No footage. ONNX tests skip cleanly unless onnxruntime is installed AND pinned weights are present
(OCR_SPIKE_CACHE=<dir with the files named in pinned_models.json>). Nothing here touches the receipt,
the verifier, gates or scoring.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from ocr_spike import decode, preprocess, roi  # noqa: E402

SPIKE = ROOT / "eval" / "ocr_spike"


def test_roi_boxes_are_reader_boxes_times_two():
    # Score/quarter/clock boxes derive from MADDEN27_STANDARD (640x360) x2 plus padding.
    from qor_receipt._vendor.qoresence.vision.local_scorebug.profile import MADDEN27_STANDARD as P

    assert roi.REF_LEFT_SCORE == P.left_score
    assert roi.REF_RIGHT_SCORE == P.right_score
    assert roi.REF_QUARTER == P.quarter
    assert roi.REF_CLOCK_TEXT == P.clock_text
    assert roi.BAND_Y0 == round(720 * P.band_y0)
    for f, (x1, x2, y1, y2) in roi.FIELDS.items():
        assert 0 <= x1 < x2 <= roi.FRAME_W and roi.BAND_Y0 <= y1 < y2 <= roi.FRAME_H, f


def test_crop_field_offsets_and_bounds():
    frame = np.arange(720 * 1280 * 3, dtype=np.uint32).reshape(720, 1280, 3).astype(np.uint8)
    a = roi.crop_field(frame, "score_l")
    b = roi.crop_field(frame, "score_l", 2, -2)
    x1, x2, y1, y2 = roi.FIELDS["score_l"]
    assert a.shape == (y2 - y1, x2 - x1, 3)
    assert np.array_equal(b, frame[y1 - 2:y2 - 2, x1 + 2:x2 + 2])
    band = frame[roi.BAND_Y0:]
    assert np.array_equal(roi.crop_band_field(band, "quarter"), roi.crop_field(frame, "quarter"))
    with pytest.raises(ValueError):
        roi.crop_field(frame[:360], "score_l")


def test_preprocess_shape_and_padding():
    crop = np.full((30, 60, 3), 255, np.uint8)
    u8, vw = preprocess.prepare([crop])
    assert u8.shape == (1, 48, 320, 3) and vw.tolist() == [96]
    x = preprocess.normalize_batch(u8, vw)
    assert x.shape == (1, 3, 48, 320) and x.dtype == np.float32
    assert np.all(x[0, :, :, :96] == 1.0) and np.all(x[0, :, :, 96:] == 0.0)


def test_ctc_greedy_collapse_and_alphabet():
    cs = ["<blank>", "1", "2", ":", "A", " "]
    seq = [1, 1, 0, 1, 3, 2, 2, 0, 4]
    logits = np.zeros((1, len(seq), len(cs)), np.float32)
    for t, k in enumerate(seq):
        logits[0, t, k] = 1.0
    assert decode.greedy(logits, cs)[0][0] == "11:2A"
    m = decode.allowed_mask(cs, "12:")
    assert decode.greedy(logits, cs, m)[0][0] == "11:2"  # 'A' frame falls back to the best allowed class


def test_label_sampler_and_degrade_are_seeded():
    from ocr_spike import synth

    for i in range(20):
        a = synth.sample_labels(np.random.default_rng([7, 1, i]))
        b = synth.sample_labels(np.random.default_rng([7, 1, i]))
        assert a == b
        assert 2 <= len(a["team_l"]) <= 4 and a["team_l"].isalpha() and a["team_l"].isupper()
        assert 0 <= int(a["score_l"]) <= 99
        m, s = a["game_clock"].split(":")
        assert 0 <= int(m) <= 15 and len(s) == 2 and (int(m) < 15 or s == "00")
        assert a["play_clock"].startswith(":") and 0 <= int(a["play_clock"][1:]) <= 40
    img = np.random.default_rng(0).integers(0, 256, (roi.BAND_H, roi.FRAME_W, 3), dtype=np.uint8)
    x, px = synth.degrade(np.random.default_rng([1, 2, 3]), img)
    y, py = synth.degrade(np.random.default_rng([1, 2, 3]), img)
    assert px == py and np.array_equal(x, y) and x.shape == img.shape


def _fonts():
    from ocr_spike import synth

    specs = synth.load_font_specs(SPIKE / "fonts.json")
    for d in (os.environ.get("OCR_SPIKE_FONTS"), "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation"):
        if d and synth.verify_fonts(Path(d), specs):
            return Path(d), synth.verify_fonts(Path(d), specs)
    pytest.skip("no pinned open-licence font found (set OCR_SPIKE_FONTS)")


def test_generator_is_reproducible_and_labelled():
    from ocr_spike import synth

    d, specs = _fonts()
    fb = synth.FontBank(d, specs)
    s1 = synth.make_sample(11, "val", 3, fb)
    s2 = synth.make_sample(11, "val", 3, synth.FontBank(d, specs))
    assert s1["labels"] == s2["labels"]
    assert synth.png_bytes(s1["band"]) == synth.png_bytes(s2["band"])
    assert s1["band"].shape == (roi.BAND_H, roi.FRAME_W, 3)
    assert set(s1["crops"]) == set(roi.FIELD_NAMES)
    assert synth.make_sample(11, "val", 4, fb)["labels"] != s1["labels"]


def test_probe_is_committed_and_hash_locked():
    from ocr_spike.probe import PROBE_DIR, load_probe
    from ocr_spike.rerun import input_sha256

    u8, vw, ids, meta = load_probe(PROBE_DIR)
    assert meta["synthetic"] is True and meta["counts_toward_replacement_decision"] is False
    assert len(ids) == len(meta["items"]) == u8.shape[0]
    # A change here means preprocessing (cv2 resize) differs on this machine; golden_check reports it too.
    assert input_sha256(u8, vw) == meta["preprocessed_input_sha256"]


def test_pinned_models_have_urls_or_recipes_and_no_weights_committed():
    pin = json.loads((SPIKE / "pinned_models.json").read_text())
    for name, m in pin["models"].items():
        assert m.get("url") or m.get("derive") or m.get("url", "x") is None, name
    assert not list(SPIKE.rglob("*.onnx")) and not list(SPIKE.rglob("*.pdparams")) and not list(SPIKE.rglob("*.pdiparams"))


def test_readers_entry_shape_matches_pr70_rules(tmp_path):
    from ocr_spike import readers_entry

    pytest.importorskip("onnxruntime")
    fake = tmp_path / "m.onnx"
    fake.write_bytes(b"not a model")
    doc = readers_entry.build(fake, "0.3.32")
    assert readers_entry.check_shape(doc) == []
    assert doc["readers"][0]["files"][-1] == {"kind": "pack", "path": "models/m.onnx", "sha256": hashlib.sha256(b"not a model").hexdigest()}
    assert doc["runtimes"][0]["id"] == "onnxruntime"


# ---------------------------------------------------------------- optional ONNX tests (need weights)
def _model(name: str) -> Path:
    pytest.importorskip("onnxruntime")
    cache = os.environ.get("OCR_SPIKE_CACHE")
    if not cache:
        pytest.skip("OCR_SPIKE_CACHE not set (weights are not committed)")
    pin = json.loads((SPIKE / "pinned_models.json").read_text())["models"][name]
    p = Path(cache) / pin["file"]
    if not p.exists():
        pytest.skip(f"{p} absent")
    if pin.get("sha256"):
        from ocr_spike.ortrun import sha256_file

        assert sha256_file(p) == pin["sha256"], "weights do not match the pinned sha256"
    return p


@pytest.mark.parametrize("name", ["upstream_int8_dynamic", "scorebug_ft_int8"])
def test_rerun_twice_single_thread_is_byte_identical(name):
    from ocr_spike import rerun
    from ocr_spike.probe import load_inputs

    p = _model(name)
    u8, vw, _ = load_inputs(SimpleNamespace(probe=str(SPIKE / "probe")))
    a = rerun.infer(p, u8, vw, threads=1)
    b = rerun.infer(p, u8, vw, threads=1)
    assert a.tobytes() == b.tobytes()
