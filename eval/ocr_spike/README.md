# eval/ocr_spike: PP-OCRv6 scorebug-reader spike (research only)

**Status: a spike.** Nothing here is imported by `qor_receipt`. It does not feed `receipt.json`, the verifier, `referee_verdict.json`, any gate, or any scoring. `site/`, `verify/` and `schema/` are untouched.

**Synthetic-data rule (QorProof).** Synthetic images are used only for training, for the synthetic val split, and for the tiny cross-machine probe. They never enter any test set used to decide whether to replace the current reader. Synthetic-only scores are always labelled as such and **never count toward replacing the current reader**. That decision needs Con's blind, game-split, frozen labelled set of real frames. This directory neither looks for that set nor trains on it, and the same goes for QorProof's held-out attack set.

## What is here

| file | what it does |
|---|---|
| `roi.py` | Field ROIs at 1280x720. Score, quarter and clock are the current reader's `MADDEN27_STANDARD` boxes x2. Team, play clock and down & distance were measured on the 3 public 170123 clips. |
| `synth.py`, `gen_dataset.py`, `fonts.json`, `fetch_fonts.py` | A deterministic synthetic scorebug generator written from scratch with PIL, OpenCV and numpy. It uses no game art: team names are random letters, and the fonts are open-licence and pinned by sha256. Outputs are band PNGs, per-field crops, `labels_*.jsonl`, a `manifest.json` with hashes, and PaddleOCR rec lists. |
| `preprocess.py`, `decode.py`, `ortrun.py`, `ort_session.json` | Pinned PP-OCR rec preprocessing (48x320, `INTER_LINEAR`, zero padding). **Unmasked** CTC greedy decoding: an out-of-alphabet top-1 char makes the read **invalid** (abstain-marked), never coerced, and per-read min/mean confidence and min margin are recorded. The pinned onnxruntime session is built only from `ort_session.json`: CPUExecutionProvider only, `ORT_ENABLE_BASIC`, 1 intra-op and 1 inter-op thread, sequential, `use_deterministic_compute`. That file is part of the reader identity: it is hashed into the readers entry and into `golden.json`, and the golden check fails if it changes. |
| `eval_rec.py` | Exact match per field on a synthetic split: `open` (unmasked) and `valid_exact` (unmasked and every char in the field alphabet). **Synthetic only.** |
| `slice_head.py` | Shrinks the official checkpoint's CTC head (18,710 classes) and NRTR head (18,714) to the 67-character scorebug alphabet. It keeps the pretrained rows rather than starting from a random head. |
| `paddle_config.py`, `finetune.py` | PaddleOCR CPU fine-tune, then export to a PIR inference model, then paddle2onnx 2.x, then onnxruntime static QDQ int8 (U8U8, per-channel, **MatMul only**, calibrated on synthetic train crops; quantising the convs broke accuracy, see Findings). Writes `weights.json` with the sha256 of every weight file. |
| `rerun.py`, `rerun_check.py` | Re-run check: the same model is run twice in-process and once in each of two separate processes, then raw logits bytes and decoded strings are compared. **Exits 1 on any byte difference.** A pass means "re-runnable with the same pinned setup" on that machine only. |
| `probe/`, `probe.py` | 32 committed synthetic crops (156 KB) with a hash-locked preprocessed-input sha256. |
| `pinned_models.json`, `golden.json`, `golden_check.py` | Cross-machine check. It downloads the pinned weights by URL (or derives them by a pinned recipe), verifies their sha256 **before** running, runs them single-threaded on the probe, and compares against the committed golden hashes. CPU features are recorded. A mismatch is a finding; it is not re-blessed. |
| `readers_entry.py` | Emits a `qorcap-readers/v0`-shaped document following open qortime PR #70's closed schema: the ONNX file as a `pack`, `ort_session.json` as a `code` file (so the ORT settings are part of the identity) and `onnxruntime` as a `runtime`. `readers_entry.example.json` is **spike-only, not for production bundles**. Nothing writes it into a bundle. |
| `smoke_clips.py` | **Smoke test only.** Compares the current reader, zero-shot PP-OCRv6 and the fine-tuned model on 3 of the 9 tracked public clips (the hash-locked 170123 ones in `site/matches/170123/clips/`; the 6 125430 clips are not used yet). It records confidence for every read and applies no gate. This is not accuracy evidence. |

## Findings (box, 2026-10-24). SYNTHETIC ONLY: these never count toward replacing the current reader
This is one synthetic snapshot: one generator seed, one training run and one checkpoint. It is not a general claim about real frames. Exact match per field on the synthetic val split (2,000 bands, 16,000 field crops, seed 20261024). The fine-tune's best checkpoint was chosen on `rec_val_small` (480 crops), which is a subset of this split, so these numbers are optimistic even as synthetic numbers.

| model | sha256 | all fields | worst field |
|---|---|---|---|
| upstream PP-OCRv6 small fp32, zero-shot, full dict | `5435fd74…` | 11760/16000 = 0.7350 | team_l 0.3235 (reads timeout bars as `_`) |
| fine-tuned fp32 (750 steps, CPU) | `5d6700b1…` | 15900/16000 = 0.9938 | quarter 0.9840 |
| fine-tuned int8 (QDQ U8U8, MatMul only) | `09f13fce…` | 15898/16000 = 0.9936 (valid_exact 15895 = 0.9934; 12 out-of-alphabet reads) | quarter 0.9830 |

- **int8 observation (one synthetic snapshot, not a general claim):** I measured this on a single step-200 checkpoint, using 200–400 synthetic val crops. Static Conv+MatMul scored 0.51, Conv-only 0.53 and dynamic 0.40, against 0.98 for fp32 on the same crops. MatMul-only scored 0.975. So `finetune.py quantize` defaults to `--ops MatMul` and the convs stay fp32. The model has 13/13 MatMul quantised and 0/57 Conv.
- **What runs at the pinned `ORT_ENABLE_BASIC`:** the QDQ int8 graph is **not executed with int8 kernels**. ORT runs DequantizeLinear, then an fp32 MatMul, and every conv in fp32 (MLAS sgemm/conv). The int8 weights are stored and quantisation is simulated, so no VNNI/AMX int8 path runs. EXTENDED/ALL would fuse to QLinearMatMul and give different logits bytes. That is why the opt level and EP are pinned as part of the identity.
- **Re-run check** (int8 file, ORT 1.31.0, pinned BASIC/CPU session, so the math is fp32 as above; x86-64 box with AVX-512 and AMX present but irrelevant at BASIC): over 2 in-process runs plus 2 fresh subprocesses, raw logits bytes and strings were identical on the 32-crop probe and on 300 val bands (2,400 crops). That makes it re-runnable with the same pinned setup on this machine. It says nothing yet about other CPUs.
- **Cross-runner:** golden hashes and the `ort_session.json` hash are committed. The matrix job is only a proposal (`ci/ocr-spike.yml`) until someone with the `workflow` scope moves it into `.github/workflows/`. The fine-tuned model is skipped in CI because it is not hosted.
- **Smoke test** (3 of 9 tracked public clips, 144 frames, unmasked decode, no gate, not accuracy evidence):
  - **Agreement:** where the current reader produced a score, quarter or clock, the fine-tuned model's valid read agreed 109/109, 109/109, 109/109 and 4/4. Zero-shot scored 99/109 (score_l) and 8/109 (score_r), because it reads the digit `0` as the letter `O`; that read is now invalid instead of coerced.
  - **Banner frames:** valid reads where the current reader abstained (fine-tuned): score_l 0, score_r 0, quarter 22, game_clock 126. The masked decode had reported 4, 12, 22 and 126. Every former banner score read (e.g. `04`) was a coerced out-of-alphabet read; the fine-tuned model actually reads letters such as `GOA`, `FIEL` and `TOUC` there.
  - **Unresolved:** the `1` quarter read on the no-scorebug frame (min conf 0.588) is still a "valid" read. Only the grammar/presence checks of the future gate would stop it.
  - OCR still has no refusal path.
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
