"""AI client for semantic validation, translation, and translation review.

Supports OpenAI-compatible APIs (OpenAI, DeepSeek, local LLM, etc.).
Configuration via config.json:
  "ai": {
    "enabled": true,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-...",
    "model": "deepseek-chat",
    "timeout": 30
  }

If AI is not configured or unavailable, all validators gracefully degrade
to rule-only mode (no AI calls made).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

LOG = logging.getLogger("license_trans.ai")

PROMPT_LICENSE_VALIDATOR = "license_validator_v2"
PROMPT_TRANSLATION = "translation_v2"
PROMPT_TRANSLATION_VALIDATOR = "translation_validator_v2"

_DEFAULTS = {
    "enabled": False,
    "base_url": "",
    "api_key": "",
    "model": "",
    "timeout": 30,
}

_CACHE_LOCK = threading.Lock()
_AI_CACHE: dict[str, Any] = {}
_AI_CACHE_LOADED = False


def load_ai_config(config: dict | None) -> dict:
    """Extract AI config from the main config dict, applying defaults."""
    ai = dict(_DEFAULTS)
    if config and isinstance(config.get("ai"), dict):
        ai.update(config["ai"])
    return ai


def is_ai_enabled(config: dict | None) -> bool:
    cfg = load_ai_config(config)
    return bool(cfg["enabled"] and cfg["base_url"] and cfg["api_key"] and cfg["model"])


def _cache_path() -> Path:
    return Path(__file__).resolve().parent.parent / "glossary" / "ai_response_cache.json"


def _load_ai_cache() -> None:
    global _AI_CACHE_LOADED
    if _AI_CACHE_LOADED:
        return
    with _CACHE_LOCK:
        if _AI_CACHE_LOADED:
            return
        path = _cache_path()
        try:
            if path.exists():
                _AI_CACHE.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            LOG.exception("AI response cache load failed")
        _AI_CACHE_LOADED = True


def _save_ai_cache() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK:
            path.write_text(json.dumps(_AI_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        LOG.exception("AI response cache save failed")


def cache_key(normalized_input: str, model: str, prompt_version: str) -> str:
    blob = f"{prompt_version}\n{model}\n{normalized_input}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AIClient:
    """Thin wrapper around OpenAI-compatible chat completions API."""

    def __init__(self, config: dict | None) -> None:
        self._cfg = load_ai_config(config)
        self._client = None
        if not (
            self._cfg.get("enabled")
            and self._cfg.get("base_url")
            and self._cfg.get("api_key")
            and self._cfg.get("model")
        ):
            LOG.info("[AI] Not configured — validators will run in rule-only mode")
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self._cfg["base_url"],
                api_key=self._cfg["api_key"],
                timeout=self._cfg["timeout"],
            )
            LOG.info("[AI] Client ready: %s @ %s",
                     self._cfg["model"], self._cfg["base_url"])
        except ImportError:
            LOG.warning("[AI] openai package not installed — AI disabled")
        except Exception as exc:
            LOG.warning("[AI] Client init failed: %s", exc)

    def is_available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return str(self._cfg.get("model") or "")

    def chat_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        prompt_version: str = "generic_v1",
        use_cache: bool = True,
    ) -> dict | None:
        """Send a chat completion request and parse JSON response.

        Returns parsed dict or None on any failure. Cache key is
        normalized input + model + prompt_version.
        """
        if not self.is_available():
            return None
        normalized = " ".join((user_content or "").split())
        key = cache_key(normalized, self.model, prompt_version)
        if use_cache:
            _load_ai_cache()
            cached = _AI_CACHE.get(key)
            if isinstance(cached, dict):
                return cached
        try:
            resp = self._client.chat.completions.create(
                model=self._cfg["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content or ""
            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [ln for ln in lines if not ln.strip().startswith("```")]
                text = "\n".join(lines)
            parsed = json.loads(text)
            if use_cache and isinstance(parsed, dict):
                with _CACHE_LOCK:
                    _AI_CACHE[key] = parsed
                _save_ai_cache()
            return parsed
        except json.JSONDecodeError as exc:
            LOG.warning("[AI] JSON parse failed: %s", exc)
            return None
        except Exception:
            LOG.exception("[AI] API call failed")
            return None
