# Capture agent (`qor_receipt/agent/`)

Feeds real data to the referee from a Windows PC: PS5 -> HDMI -> "USB3.0 Video" capture card
(DirectShow, read at 640x360) and the DualSense Edge on USB to the PC (it stays on Bluetooth to the PS5).

| module | role |
| --- | --- |
| `capture.py` | DirectShow device by name (pygrabber), CAP_DSHOW, 1-frame buffer, resize to 640x360, frame-size reset guard, per-frame sha256 |
| `hid.py` | hidapi DualSense/Edge (054C:0CE6/0DF2) USB 0x01 parsing; every button edge kept, sticks decimated to `QOR_HID_ANALOG_HZ` (default 125, same as Qoresence); monotonic-derived timestamps |
| `pause_detect.py` | pause-menu heuristic (dim outside menu region + bright menu panel) or template NCC; thresholds via `--pause-config JSON`. **NEEDS TUNING** |
| `runner.py` | match start, kickoff/halftime(Q3)/end(FINAL) challenges, 4 s rolling checkpoints, scorebug reads ~5/s with agreement, timeline + motion bins, finalize, co-sign, receipt |

Live (on the Windows laptop):

    scripts\qor-agent.cmd http://REFEREE:8000 --window
    # = qortime agent --device "USB3.0 Video" --referee http://REFEREE:8000 --window

Each live run writes `qor-session-*/hid.jsonl` (button/stick events, challenge nonces) and
`qor-session-*/frames/<t_ms>.jpg` (all frames around challenges, ~5/s otherwise) so the match can be replayed:

    qortime agent --replay qor-session-X/frames --hid-log qor-session-X/hid.jsonl

Replay runs the same core against an in-process referee on a virtual clock, re-using the recorded
nonces (proves the pipeline, not freshness). `qortime agent-fixture DIR` writes a synthetic session.

End of game: the reader now reads the red final-seconds clock (luma, contrast-stretched: 4:2:0 chroma blurs the
red channel) under the same glyph gates and flags `clock_red`; a red 0:00 needs 5 agreed reads. If the clock
never reads 0:00 the agent falls back to `scorebug_gone` (strip absent ≥30 s after a Q4 ≤0:05 read) or
`final_screen` (hook `AgentCore.final_screen_detector`; no detector ships yet, no real final-screen frames). A Jev 1.14 shadow detector logs `jev_final_screen` records through `AgentCore.final_screen_shadow` and never sets the basis (see README).
The FINAL log line / session-log `final` record carry `final_basis`.

Known live unknowns: pause-menu template; a real Q4 end-of-game capture (no 0:00 red frame or final
screen exists yet; eval set: `eval/labels/`). The 640x360 caps stay full <= 16 / lower <= 8.
Challenge rule v2 uses the measured USB/HDMI caps (full <= 26, lower <= 16). Fresh challenges are
drawn so CROSS and CIRCLE, which select or leave the pause menu, sit only in the last slot.
