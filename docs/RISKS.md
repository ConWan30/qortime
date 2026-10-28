# Residual risks (v0)

This is what a VERIFIED receipt does **not** prove. Platforms should price these in.

1. **Account sharing / pro-for-hire.** A receipt does not name the human who held the controller. The PSN id is
   self-reported by the agent, or read off the screen. Later mitigations, still not built: PSN profile cross-check,
   cross-match behavioral input fingerprints, platform KYC.

   What the controller evidence is, as the code computes it:
   - The agent key signs a log of HID reports from a pad QorCap saw on USB. Pad identity is enumeration metadata:
     `serial:<USB serial>`, otherwise `instance:<hidapi path>`, otherwise `hidraw:<node>`. That string is recorded
     on each connect. It is not a console pairing and not a signature from the pad. QorCap's local input proof can
     flag a pad swap mid-match (`pad_identity_changed`) or a missing id. A different pad used from the start is not
     flagged. QorTime folds that proof into `input_advisory`. Advisory notes do not change the verdict.
   - Official challenges show that presses in the observed log lined up with pause menus on the captured video,
     inside that rule's window. Rule v2 is 0–2500 ms, and the successful latencies must sit within ±300 ms of their
     median. That is coincidence of those presses with the console pausing. It is not a binding for every other
     input the console obeyed.
   - Motion rule v3 correlates video frame-difference energy with the per-second count of logged `"in"` rows.
     The count is report rate. It is not stick values and it is not per-axis. The floor is 300 per mille, which is
     Pearson r ≥ 0.30. Below the floor the receipt is PARTIAL (`motion_correlation_low`). A missing score is
     PARTIAL (`motion_signal_missing`). Neither case is FAIL. Buttons, triggers, or the left stick can keep the
     row count in step with play while the right stick is idle, and the check can still clear 300.
   - Haptic counts are IMU bursts on the observed pad. They are advisory and do not change the verdict. The
     verifier calls them haptic events. It does not claim each burst is a console rumble command. The laptop
     cannot see the Bluetooth rumble stream.
2. **Both players collude** (e.g. to launder money or farm promos). Dual attestation only proves the two agents agree.
   Two colluding players can play a real but staged game, and every check passes. Out of scope for a result receipt.
   Platform-side risk controls are needed.
3. **Single-attested matches.** One agent and the referee. The opponent's side is unobserved, so a cheating
   single-attester controls both the video and the input evidence. That's why it's a lower tier.
4. **Compromised player PC (phase 1 software keys).** Malware or a skilled cheater with the key can *live*-forge
   checkpoints if they also fake capture and HID in real time. Referee timestamps stop *post-hoc* fabrication, not a
   real-time simulator with a nonce-responding bot. Raising that cost is phase 2 (TPM key attestation).
5. **Video injection in real time.** An HDMI or capture-card MITM that composites a fake scorebug while the real game runs
   would pass the pause challenge (the pause is real). Cross-checks: dual-mode timelines; motion correlation; later, C2PA
   on clips.
6. **Scorebug misread.** The vendored reader refuses unless frames agree, but a new Madden UI patch can break the
   profile. The fail mode is "no final", not "wrong final". Watch out for profile drift after title updates.
7. **Disconnects / mercy rules / quits.** v0 detects only a normal 4th-quarter or OT final. Quits produce no receipt,
   and the platform's dispute flow applies.
8. **Referee trust.** The referee is a trusted third party. A compromised referee key can sign anything. Pin its key,
   rotate it, and publish receipts to a transparency log (phase 2).
9. **Clock and latency windows.** The 600 ms pause-latency and 3 s skew windows are first guesses. Tune them on real rigs.
   Loose windows weaken relay detection; tight windows cause false FAILs.
10. **Privacy.** Receipts carry hashes only, not frames or raw input. HID logs submitted for challenges are hashed into
    the receipt. The referee sees them in memory.
11. **Passthrough adapter / right-stick-only injection.** Open. An inline adapter between the real pad and the PS5
    can forward the human's buttons, triggers, and left stick, and inject its own right-stick input. A
    computer-vision aimbot is the case. QorCap still logs the real pad on USB. Forwarded challenge presses can
    still meet the pause window. If the adapter forwards vibration, the observed pad can still shake, and the
    haptic count still does not change the verdict. Motion rule v3 counts input rows, so play that moves the
    left stick or the buttons can still clear 300. Nothing in the current checks binds the observed pad to the
    input the console obeyed. No red-team row covers this case.

    Planned mitigations, not built. They stay advisory until a later rule says otherwise, and a heuristic must
    not FAIL a receipt on its own:
    - Per-axis stick-versus-camera-pan correlation, as an advisory signal first. Compare right-stick `rx`/`ry`
      with camera pan on still-to-motion transitions.
    - Binding the observed pad to the console is out of scope for now. The PS5 link is Bluetooth and is left alone.
