"""Translation provenance, cache isolation, offline behavior."""
from unittest.mock import MagicMock, patch

from license_trans.translation_resolver import (
    TranslationEntry,
    TranslationResolver,
    leftover_cjk,
    load_ai_cache,
    load_verified_cache,
    put_ai_cache,
    put_verified,
    AI_CACHE_FILE,
    VERIFIED_CACHE_FILE,
)
from license_trans.web_verifier import WebEvidence, WebTranslationVerifier, classify_domain
from license_trans.translate import translate_company


def test_limited_company_uses_local_glossary():
    resolver = TranslationResolver({"ai": {"enabled": False}})
    english = resolver.translate({"name": "有限公司", "warnings": []})
    entry = resolver.provenance["name"]
    assert leftover_cjk(english["name"]) == []
    assert entry.provider in ("manual_glossary", "rule")
    assert "Ltd" in english["name"] or "Limited" in english["name"] or english["name"]


def test_ai_unknown_fragment_not_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "license_trans.translation_resolver.AI_CACHE_FILE", tmp_path / "ai.json",
    )
    monkeypatch.setattr(
        "license_trans.translation_resolver.VERIFIED_CACHE_FILE", tmp_path / "ver.json",
    )
    import license_trans.translation_resolver as tr
    tr._AI_CACHE.clear()
    tr._VERIFIED.clear()
    tr._AI_LOADED = False
    tr._VERIFIED_LOADED = False

    class FakeAI:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return {"translation": "Zzzwidget"}

    resolver = TranslationResolver({"ai": {"enabled": False}})
    resolver._ai = FakeAI()
    out = resolver._resolve_fragments("scope", "zzz未知词", "zzz未知词", ["未知词"])
    assert out["provider"] == "ai"
    assert out["verified"] is False
    load_ai_cache()
    load_verified_cache()
    assert "未知词" in tr._AI_CACHE
    assert "未知词" not in tr._VERIFIED


def test_web_verified_updates_provenance_and_verified_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "license_trans.translation_resolver.VERIFIED_CACHE_FILE", tmp_path / "ver.json",
    )
    import license_trans.translation_resolver as tr
    tr._VERIFIED.clear()
    tr._VERIFIED_LOADED = False
    resolver = TranslationResolver({"ai": {"enabled": False}})
    resolver.provenance["address"] = TranslationEntry(
        source="杭州高新技术产业开发区",
        translated="Hangzhou High-tech Zone",
        provider="ai",
        confidence=0.4,
        verified=False,
    )
    english = {
        "address": "Hangzhou High-tech Zone",
        "warnings": [],
    }
    chinese = {"address": "杭州高新技术产业开发区"}
    resolver._web = MagicMock()
    resolver._web.verify.return_value = WebEvidence(
        source="杭州高新技术产业开发区",
        translation="Hangzhou High-tech Zone",
        verified=True,
        source_url="https://www.hangzhou.gov.cn/x",
        source_domain="www.hangzhou.gov.cn",
        source_type="government",
    )
    english = resolver.apply_web_flags(english, chinese, {"address": "web_verify"})
    assert resolver.provenance["address"].provider == "web_verified"
    assert resolver.provenance["address"].verified is True
    assert resolver.provenance["address"].source_url.endswith("/x")
    assert "杭州高新技术产业开发区" in tr._VERIFIED


def test_web_offline_does_not_fail(monkeypatch):
    verifier = WebTranslationVerifier()

    def boom(*a, **k):
        raise OSError("DNS fail")

    monkeypatch.setattr("license_trans.web_verifier._ddg_search", boom)
    ev = verifier.verify("杭州市", "Hangzhou")
    assert ev.verified is False
    assert "search_failed" in ev.reason


def test_classify_government_over_blog():
    assert classify_domain("www.hangzhou.gov.cn") == "government"
    assert classify_domain("blog.csdn.net") == "low_priority"


def test_ai_offline_local_translation_still_works():
    resolver = TranslationResolver({"ai": {"enabled": False}})
    english = resolver.translate({
        "name": "测试有限公司",
        "type": "有限责任公司",
        "credit_code": "91110101MA004KQM97",
        "warnings": [],
    })
    assert english["type"]
    assert english["name"]
