# eval/ocr_spike: PP-OCRv6 scorebug-reader spike (research only)

**Status: a spike.** Nothing here is imported by `qor_receipt`. It does not feed `receipt.json`, the verifier, `referee_verdict.json`, any gate, or any scoring. `site/`, `verify/` and `schema/` are untouched.

**Synthetic-data rule (QorProof).** Synthetic images are used only for training, for the synthetic val split, and for the tiny cross-machine probe. They never enter any test set used to decide whether to replace the current reader. Synthetic-only scores are always labelled as such and **never count toward replacing the current reader**. That decision needs Con's blind, game-split, frozen labelled set of real frames. This directory neither looks for that set nor trains on it, and the same goes for QorProof's held-out attack set.

## What is here

| file | what it does |
|---|---|
| `roi.py` | Field ROIs at 1280x720. Score, quarter and clock are the current reader's `MADDEN27_STANDARD` boxes x2. Team, play clock and down & distance were measured on the 3 public 170123 clips. |
| `synth.py`, `gen_dataset.py`, `fonts.json`, `fetch_fonts.py` | A deterministic synthetic scorebug generator written from scratch with PIL, OpenCV and numpy. It uses no game art: team names are random letters, and the fonts are open-licence and pinned by sha256. Outputs are band PNGs, per-field crops, `labels_*.jsonl`, a `manifest.json` with hashes, and PaddleOCR rec lists. |
| `preprocess.py`, `decode.py`, `ortrun.py` | Pinned PP-OCR rec preprocessing (48x320, `INTER_LINEAR`, zero padding), CTC greedy decoding, and the pinned onnxruntime CPU session (1 intra-op and 1 inter-op thread, sequential, `ORT_ENABLE_BASIC`, `use_deterministic_compute`). |
| `eval_rec.py` | Exact match per field on a synthetic split. **Synthetic only.** |
| `slice_head.py` | Shrinks the official checkpoint's CTC head (18,710 classes) and NRTR head (18,714) to the 67-character scorebug alphabet. It keeps the pretrained rows rather than starting from a random head. |
| `paddle_config.py`, `finetune.py` | PaddleOCR CPU fine-tune, then export to a PIR inference model, then paddle2onnx 2.x, then onnxruntime static QDQ int8 (U8U8, per-channel, Conv+MatMul, calibrated on synthetic train crops). Writes `weights.json` with the sha256 of every weight file. |
| `rerun.py`, `rerun_check.py` | Re-run check: the same model is run twice in-process and once in each of two separate processes, then raw logits bytes and decoded strings are compared. **Exits 1 on any byte difference.** A pass means "re-runnable with the same pinned setup" on that machine only. |
| `probe/`, `probe.py` | 32 committed synthetic crops (156 KB) with a hash-locked preprocessed-input sha256. |
| `pinned_models.json`, `golden.json`, `golden_check.py` | Cross-machine check. It downloads the pinned weights by URL (or derives them by a pinned recipe), verifies their sha256 **before** running, runs them single-threaded on the probe, and compares against the committed golden hashes. CPU features are recorded. A mismatch is a finding; it is not re-blessed. |
| `readers_entry.py` | Emits a `qorcap-readers/v0`-shaped document following open qortime PR #70's closed schema: the ONNX file as a `pack` and `onnxruntime` as a `runtime`. It is an example only, and nothing writes it into a bundle. |
| `smoke_clips.py` | **Smoke test only.** Compares the current reader, zero-shot PP-OCRv6 and the fine-tuned model on the 3 public, hash-locked clips in `site/matches/170123/clips/`. This is not accuracy evidence. |

## Weights and licences
- Upstream `PaddlePaddle/PP-OCRv6_small_rec_onnx` (HF rev `b8f84f0b`) `inference.onnx` sha256 `5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634`. The model card's licence is **Apache-2.0**.
- Trainable checkpoint `PP-OCRv6_small_rec_pretrained.pdparams` sha256 `25c9bd54b0e5900916e8bb6ada938abeffb1eac1baedac0ca54a45b1c9310825`. It comes from the official PaddleOCR docs link (Apache-2.0 repo, the same model card).
- No weights are committed (see `.gitignore`). The fine-tuned int8 file is referenced by sha256 only. Hosting it at a pinned URL needs Con's OK.

## Reproduce (box)
```bash
python eval/ocr_spike/fetch_fonts.py --out fonts
python eval/ocr_spike/gen_dataset.py --out data/synth_v0 --fonts fonts --train 20000 --val 2000 --seed 20261024
python eval/ocr_spike/eval_rec.py --data data/synth_v0 --split val --model inference.onnx --dict ppocrv6_dict.txt
python eval/ocr_spike/rerun_check.py --model scorebug_rec_int8.onnx --dict eval/ocr_spike/scorebug_dict.txt --data data/synth_v0 --split val
python eval/ocr_spike/golden_check.py --cache .cache/ocr_spike
python eval/ocr_spike/smoke_clips.py --zeroshot inference.onnx --zeroshot-dict ppocrv6_dict.txt --finetuned scorebug_rec_int8.onnx
```
