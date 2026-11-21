"""Muse Spark context check for Show Your Hands action candidates (hackathon work, Oct 26 - Nov 4 2026).

A local motion onset is only a *candidate* action. Muse Spark looks at two frames (just before and just after the
onset) and says whether the change is a player-triggered start of live play (snap, kick, throw...) or something no
press causes (camera pan, replay, cutscene, celebration, menu). Only confident ``player_action`` answers keep the
candidate. Everything else, including any error, drops it, so Muse can only REMOVE events, never add them.
That direction can only remove ``no_press`` flags, never create them (it can lower the anchor count, which makes
clips unscorable, also never a penalty).

Cost-capped by ``max_calls`` per match; usage and estimated cost are recorded in the sidecar.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable

from qor_receipt.ai_ruling.muse import BASE_URL, DEFAULT_MODEL, MuseError, _urllib_transport, api_key_from_env

USD_IN, USD_OUT = 1.25 / 1e6, 4.25 / 1e6
MAX_CALLS = 12
MIN_GAP_S = 6.0      # a new play can't start within this long of the last kept start in the same clip
KINDS = ("player_action", "camera_or_cutscene", "menu_or_overlay", "unsure")
PROMPT = (
    "Two frames from an American football video game broadcast (Madden), about one second apart: first BEFORE, "
    "then AFTER. Ignore the dark 'SCOREBUG LOCK' box top-left; it is a capture overlay. Classify what changed "
    "between them:\n"
    "- player_action: ONLY the moment a play starts. BEFORE must show players set and still in a formation at "
    "the line of scrimmage (or a passer with receiver icons); AFTER shows that same play just starting (snap, "
    "kick, pass released). The field and formation must be visible in both frames;\n"
    "- camera_or_cutscene: anything else that moves: the camera following a ball already in the air, goalposts, "
    "crowd or stadium shots, replays, celebrations, players walking after the whistle, camera cuts;\n"
    "- menu_or_overlay: a menu, play-call screen, stats or presentation overlay;\n"
    "- unsure: you can't tell.\n"
    "Set confident true only if the answer is clear from the frames."
)
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "hands_context", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["kind", "confident"],
    "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "confident": {"type": "boolean"}}}}}


class MuseContext:
    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_MODEL, base_url: str = BASE_URL,
                 max_tokens: int = 2000, timeout_s: float = 60.0, transport: Callable | None = None) -> None:
        if model.endswith("-contributor"):
            raise ValueError("contributor-mode models are not allowed here")
        self._key = api_key if api_key is not None else api_key_from_env()
        self.model, self.base_url, self.max_tokens, self.timeout_s = model, base_url.rstrip("/"), max_tokens, timeout_s
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:
        return f"MuseContext(model={self.model!r}, key={'set' if self._key else 'missing'})"

    def classify(self, before_jpeg: bytes, after_jpeg: bytes) -> tuple[str, bool, dict[str, int]]:
        if not self._key:
            raise MuseError("api_key_missing")
        img = lambda b: {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}  # noqa: E731
        body = {"model": self.model, "temperature": 0, "max_completion_tokens": self.max_tokens, "reasoning_effort": "minimal",
                "response_format": RESPONSE_FORMAT,
                "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}, img(before_jpeg), img(after_jpeg)]}]}
        status, d = self._transport(f"{self.base_url}/chat/completions", self._key, body, self.timeout_s)
        if status != 200 or not isinstance(d, dict):
            raise MuseError(f"http_{status}")
        try:
            ch = d["choices"][0]
            if ch.get("finish_reason") == "length":
                raise MuseError("truncated_finish_reason_length")
            p = json.loads(ch["message"]["content"])
        except MuseError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise MuseError(f"malformed_response:{type(e).__name__}") from None
        u = d.get("usage") or {}
        usage = {k: int(u[k]) for k in ("prompt_tokens", "completion_tokens") if isinstance(u.get(k), int)}
        kind = p.get("kind") if p.get("kind") in KINDS else "unsure"
        return kind, bool(p.get("confident")), usage


def usd_micro(usage: dict[str, int]) -> int:
    return int(round((usage.get("prompt_tokens", 0) * USD_IN + usage.get("completion_tokens", 0) * USD_OUT) * 1e6))


def filter_actions(candidates: list[dict[str, Any]], frames: Callable[[dict[str, Any]], tuple[bytes, bytes]],
                   ctx: MuseContext | None, *, max_calls: int = MAX_CALLS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep only onsets Muse confidently calls player_action. ``ctx=None`` => keep nothing ("Muse unchecked").

    Returns (kept_events, report). Never raises.
    """
    report: dict[str, Any] = {"model": getattr(ctx, "model", None), "calls": 0, "cost_micro_usd": 0, "events": []}
    kept: list[dict[str, Any]] = []
    for ev in candidates:
        if ev.get("kind") != "onset":
            continue
        row = {"t_ms": round(float(ev.get("t_s", 0)) * 1000), "clip": ev.get("clip"), "muse": "unchecked"}
        if ctx is not None and report["calls"] < max_calls:
            try:
                b, a = frames(ev)
                report["calls"] += 1
                kind, conf, usage = ctx.classify(b, a)
                report["cost_micro_usd"] += usd_micro(usage)
                row["muse"] = kind if conf else "unsure"
            except Exception as e:  # noqa: BLE001 - fail closed: drop the candidate
                row["muse"] = "unchecked"
                row["error"] = type(e).__name__
        if row["muse"] == "player_action":
            last = next((k for k in reversed(kept) if k.get("clip") == ev.get("clip")), None)
            if last is not None and float(ev.get("t_s", 0)) - float(last.get("t_s", 0)) < MIN_GAP_S:
                row["muse"] = "player_action_dropped_same_play"
            else:
                kept.append(ev)
        report["events"].append(row)
    return kept, report
