# Provenance of the 30 fps hands clip-clock goldens

`tests/test_hands_frame_rate.py::test_thirty_fps_is_byte_identical_to_the_pre_fix_code` compares `hands_golden_text()` against `tests/fixtures/hands_clip_clock/golden_30fps_fda7b51.txt` (sha256 `03f35cebd802bb6b31fec92bdfb50f704d6c5fcba7438c60687fbaa86ed42778`).

`hands_golden_text()` runs `python -m qor_receipt.hands.sidecar`'s `main()` on 4 synthetic segment sets (regular, jitter, short, flat) with 8 clips each, all without `source.frame_rate`. Video decoding and `build()` are stubbed. It records:
- the `capture_clock` fit as a full float repr;
- every clip's `start_mono_ms` (32 in total);
- the printed document.

The fixture was produced on Nov 12 2026 (CST) by running the same function against the **pre-fix code at qortime main `fda7b51`** (`* 30.0` in `hands/sidecar.py`, `10 * 30` and the 30 fps nominal in `review/clock.py`):

```
git worktree add --detach /tmp/qt-prefix fda7b51
cp tests/test_hands_frame_rate.py /tmp/qt-prefix/tests/
cd /tmp/qt-prefix
python -c "import sys; sys.path[:0] = ['tests', '.']; import test_hands_frame_rate as t; sys.stdout.write(t.hands_golden_text())" > golden.txt
sha256sum golden.txt   # 03f35cebd802bb6b31fec92bdfb50f704d6c5fcba7438c60687fbaa86ed42778
```

On the pre-fix code, the golden test passes and the other 19 tests in the file fail.
