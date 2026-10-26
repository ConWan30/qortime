"""Minimal Meta Model API client (OpenAI-compatible /chat/completions) for Muse Spark. Standard library only.

* Key from env ``META_API_KEY`` (fallback ``MODEL_API_KEY``, the name the Meta docs use). Never hard-coded, never
  logged, never written to a sidecar or put in an exception message.
* Any failure raises ``MuseError`` with a short reason; the explainer turns that into "no explanation" (fail closed).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"          # Standard mode. Never the -contributor variant (Meta may train on content).
KEY_ENVS = ("META_API_KEY", "MODEL_API_KEY")
# Muse Spark always reasons and reasoning tokens count against this cap: 64 left an empty answer in the smoke test.
DEFAULT_MAX_TOKENS = 2048


class MuseError(Exception):
    """API/network/format failure. The message never contains the key."""


@dataclass
class MuseReply:
    text: str
    model: str
    finish_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)


def api_key_from_env(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    for name in KEY_ENVS:
        v = (env.get(name) or "").strip()
        if v:
            return v
    return None


class MuseClient:
    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_MODEL, base_url: str = BASE_URL,
                 max_tokens: int = DEFAULT_MAX_TOKENS, reasoning_effort: str | None = "minimal", timeout_s: float = 60.0,
                 transport=None) -> None:
        if model.endswith("-contributor"):
            raise ValueError("contributor-mode models are not allowed here")
        self._key = api_key if api_key is not None else api_key_from_env()
        self.model, self.base_url, self.max_tokens = model, base_url.rstrip("/"), max_tokens
        self.reasoning_effort, self.timeout_s = reasoning_effort, timeout_s
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:  # never show the key
        return f"MuseClient(model={self.model!r}, key={'set' if self._key else 'missing'})"

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def chat(self, system: str, user: str) -> MuseReply:
        if not self._key:
            raise MuseError("api_key_missing")
        body: dict[str, Any] = {"model": self.model, "max_completion_tokens": self.max_tokens,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        status, data = self._transport(f"{self.base_url}/chat/completions", self._key, body, self.timeout_s)
        if status != 200 or not isinstance(data, dict) or "_error_code" in data:
            code = (data or {}).get("_error_code") if isinstance(data, dict) else None
            raise MuseError(f"http_{status}" + (f":{code}" if code else ""))
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise MuseError(f"malformed_response:{type(e).__name__}") from None
        if finish == "length":
            raise MuseError("truncated_finish_reason_length")
        u = data.get("usage") or {}
        usage = {k: int(u[k]) for k in ("prompt_tokens", "completion_tokens") if isinstance(u.get(k), int)}
        rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if isinstance(rt, int):
            usage["reasoning_tokens"] = rt
        return MuseReply(text=str(text), model=str(data.get("model") or self.model), finish_reason=finish, usage=usage)


def _urllib_transport(url: str, key: str, body: dict, timeout_s: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _error_code(e)
    except Exception as e:  # noqa: BLE001  any network failure = no answer
        raise MuseError(f"network:{type(e).__name__}") from None


def _error_code(e) -> dict | None:
    """Only the API's short error code (e.g. ``invalid_api_key``), sanitised. Never the body, never the key."""
    try:
        err = json.loads(e.read(4096) or b"{}").get("error") or {}
        code = str(err.get("code") or err.get("type") or "")
    except Exception:  # noqa: BLE001
        return None
    code = "".join(ch for ch in code.lower() if ch.isalnum() or ch == "_")[:40]
    return {"_error_code": code} if code else None


def list_models(api_key: str | None = None, *, base_url: str = BASE_URL, timeout_s: float = 15.0) -> dict:
    """Operator diagnostic: model ids visible to this key (free GET /models). Never returns the key. Never raises."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {"status": None, "error_code": "api_key_missing", "models": []}
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read())
        ids = sorted(str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict))
        return {"status": 200, "models": ids}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error_code": (_error_code(e) or {}).get("_error_code"), "models": []}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error_code": f"network_{type(e).__name__}", "models": []}
