"""Show Your Hands (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026; hackathon work, see HACKATHON.md).

A verified DualSense input overlay for highlight clips, plus a press-to-pixel check that flags on-screen actions with
no press behind them, sub-human reactions, or machine-regular timing.

Advisory and LOWER-ONLY: the most it can do is lower VERIFIED to PARTIAL. It never FAILs a match, never raises a
status, and is not wired into ``verify_receipt`` or ``verify.js``. Inputs that don't hash to the receipt's
checkpoints are refused outright (no overlay, no flags). Sidecars never carry the pad's device instance path,
VID/PID strings, serials or gamertags.
"""
