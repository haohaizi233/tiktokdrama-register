"""Translation pipeline: local glossary → rule → verified cache → AI fragments.

AI never receives a whole name/address/scope when local tokens can resolve it.
Translation validators cannot overwrite English; web verifier only upgrades
provenance when official evidence is found.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .ai_client import AIClient, PROMPT_TRANSLATION
from .trace import PipelineTrace
from .translate import (
    GLOSSARY,
    _CJK_RE,
    translate_address,
    translate_authority,
    translate_capital,
    translate_company,
    translate_date,
    translate_fields,
    translate_person,
    translate_scope,
    translate_term,
    translate_type,
)
from .web_verifier import WebTranslationVerifier

LOG = logging.getLogger("license_trans.translate")

_CJK = _CJK_RE
_LOCK = threading.Lock()
_VERIFIED: dict[str, dict] = {}
_AI_CACHE: dict[str, dict] = {}
_VERIFIED_LOADED = False
_AI_LOADED = False

VERIFIED_CACHE_FILE = GLOSSARY / "verified_translation_cache.json"
AI_CACHE_FILE = GLOSSARY / "ai_translation_cache.json"


@dataclass
class TranslationEntry:
    source: str
    translated: str
    provider: str
    confidence: float | None
    verified: bool
    source_url: str | None = None
    source_domain: str | None = None
    source_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json_cache(path: Path, bucket: dict, loaded_flag: str) -> None:
    global _VERIFIED_LOADED, _AI_LOADED
    loaded = _VERIFIED_LOADED if loaded_flag == "verified" else _AI_LOADED
    if loaded:
        return
    with _LOCK:
        loaded = _VERIFIED_LOADED if loaded_flag == "verified" else _AI_LOADED
        if loaded:
            return
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    bucket.update(data)
        except Exception:
            LOG.exception("Failed to load translation cache %s", path)
        if loaded_flag == "verified":
            _VERIFIED_LOADED = True
        else:
            _AI_LOADED = True


def _save_json_cache(path: Path, bucket: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            path.write_text(json.dumps(bucket, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        LOG.exception("Failed to save translation cache %s", path)


def load_verified_cache() -> dict:
    _load_json_cache(VERIFIED_CACHE_FILE, _VERIFIED, "verified")
    return _VERIFIED


def load_ai_cache() -> dict:
    _load_json_cache(AI_CACHE_FILE, _AI_CACHE, "ai")
    return _AI_CACHE


def put_verified(source: str, entry: dict) -> None:
    load_verified_cache()
    with _LOCK:
        _VERIFIED[source] = entry
    _save_json_cache(VERIFIED_CACHE_FILE, _VERIFIED)


def put_ai_cache(source: str, entry: dict) -> None:
    load_ai_cache()
    with _LOCK:
        _AI_CACHE[source] = entry
    _save_json_cache(AI_CACHE_FILE, _AI_CACHE)


def leftover_cjk(text: str) -> list[str]:
    return _CJK.findall(text or "")


def _provider_for_local(source: str, translated: str, field: str) -> str:
    if not source:
        return "rule"
    try:
        from .translate import _load
        for name in ("words.json", "phrases.json", "companies.json", "types.json",
                     "type_atoms.json", "people.json", "places.json",
                     "scope_atoms.json", "commodities.json", "scope_extra.json"):
            table = _load(name)
            if source in table or any(k in source and table.get(k) == translated for k in table):
                if name in ("places.json",):
                    return "division_db"
                return "manual_glossary"
    except Exception:
        LOG.exception("glossary provider lookup failed")
    if field in ("established", "issue_date", "term", "capital"):
        return "rule"
    if not leftover_cjk(translated):
        return "rule"
    return "rule"


class TranslationResolver:
    """Canonical Chinese → English with provenance."""

    def __init__(self, config: dict | None = None, trace: PipelineTrace | None = None) -> None:
        self._config = config or {}
        self._trace = trace
        self._ai = AIClient(config)
        self._web = WebTranslationVerifier()
        self.provenance: dict[str, TranslationEntry] = {}

    def translate(self, canonical: dict) -> dict:
        english = translate_fields(canonical)
        if self._trace:
            self._trace.emit("LOCAL_TRANSLATION", fields=list(english.keys()))

        for field in ("name", "type", "legal_person", "scope", "address",
                      "authority", "capital", "established", "term", "issue_date"):
            zh = canonical.get(field) or ""
            en = english.get(field) or ""
            leftover = leftover_cjk(en)
            provider = _provider_for_local(zh, en, field)
            verified = provider in ("manual_glossary", "division_db", "rule") and not leftover
            confidence = 0.95 if verified else 0.6

            if leftover and zh:
                cached = self._resolve_fragments(field, zh, en, leftover)
                en = cached["text"]
                english[field] = en
                provider = cached["provider"]
                verified = cached["verified"]
                confidence = cached.get("confidence")
                leftover = leftover_cjk(en)

            self.provenance[field] = TranslationEntry(
                source=zh,
                translated=en,
                provider=provider,
                confidence=confidence,
                verified=verified,
            )

        english["provenance"] = {k: v.to_dict() for k, v in self.provenance.items()}
        english["source"] = {
            k: canonical.get(k) for k in (
                "credit_code", "name", "type", "legal_person", "scope", "capital",
                "established", "address", "authority", "issue_date", "term",
            )
        }
        return english

    def _resolve_fragments(self, field: str, zh: str, en: str, leftover: list[str]) -> dict:
        load_verified_cache()
        load_ai_cache()
        text = en
        provider = "rule"
        verified = False
        confidence: float | None = 0.5
        for frag in leftover:
            if frag in _VERIFIED:
                hit = _VERIFIED[frag]
                text = text.replace(frag, hit.get("translation") or hit.get("translated") or "")
                provider = "verified_cache"
                verified = True
                confidence = 0.9
                continue
            if frag in _AI_CACHE:
                hit = _AI_CACHE[frag]
                text = text.replace(frag, hit.get("translation") or hit.get("translated") or frag)
                provider = "ai"
                verified = False
                confidence = 0.4
                continue
            ai_en = self._ai_translate_fragment(frag)
            if ai_en:
                text = text.replace(frag, ai_en)
                provider = "ai"
                verified = False
                confidence = 0.4
                put_ai_cache(frag, {
                    "source": frag,
                    "translation": ai_en,
                    "verified": False,
                    "provider": "ai",
                })
                if self._trace:
                    self._trace.emit("AI_TRANSLATION", field=field, fragment=frag)
        return {"text": text, "provider": provider, "verified": verified, "confidence": confidence}

    def _ai_translate_fragment(self, fragment: str) -> str:
        if not fragment or not self._ai.is_available():
            return ""
        result = self._ai.chat_json(
            "You translate unknown Chinese business-license fragments to English. "
            "Return JSON {\"translation\": \"...\"} only. Do not translate already-English text.",
            json.dumps({"fragment": fragment}, ensure_ascii=False),
            temperature=0.1,
            max_tokens=200,
            prompt_version=PROMPT_TRANSLATION,
        )
        if not isinstance(result, dict):
            return ""
        text = str(result.get("translation") or "").strip()
        if text and not leftover_cjk(text):
            return text
        return ""

    def apply_web_flags(
        self,
        english: dict,
        chinese: dict,
        flags: dict[str, str],
    ) -> dict:
        """Run web verification for flagged fields. Never overwrites on failure."""
        review: list[str] = []
        for field, action in flags.items():
            if action == "needs_review":
                review.append(field)
                continue
            if action not in ("web_verify", "suspected_error"):
                continue
            zh = chinese.get(field) or ""
            en = english.get(field) or ""
            if not zh or not en:
                continue
            try:
                evidence = self._web.verify(zh, en, field=field)
            except Exception:
                LOG.exception("web verifier crashed")
                if self._trace:
                    self._trace.emit("WEB_VERIFY", field=field, verified=False, reason="exception")
                continue
            if self._trace:
                self._trace.emit(
                    "WEB_VERIFY",
                    field=field,
                    verified=evidence.verified,
                    source_url=evidence.source_url,
                    source_type=evidence.source_type,
                )
            entry = self.provenance.get(field)
            if evidence.verified:
                if entry:
                    entry.provider = "web_verified"
                    entry.verified = True
                    entry.source_url = evidence.source_url
                    entry.source_domain = evidence.source_domain
                    entry.source_type = evidence.source_type
                    entry.confidence = 0.95
                put_verified(zh, evidence.to_dict())
            else:
                if field in WebTranslationVerifier.HIGH_RISK_FIELDS:
                    review.append(field)
                if entry:
                    entry.verified = False
        if review:
            warnings = list(english.get("warnings") or [])
            for field in review:
                tag = f"en_{field}_needs_review"
                if tag not in warnings:
                    warnings.append(tag)
            english["warnings"] = warnings
        english["provenance"] = {k: v.to_dict() for k, v in self.provenance.items()}
        english["web_review_fields"] = review
        return english
