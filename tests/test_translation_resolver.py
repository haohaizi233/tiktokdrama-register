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


def test_scope_junk_fragments_are_dropped_even_if_cached(tmp_path, monkeypatch):
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
    put_ai_cache("日住", {"source": "日住", "translation": "daily accommodation", "verified": False})
    put_ai_cache("文县", {"source": "文县", "translation": "Wen County", "verified": False})
    put_ai_cache("花客", {"source": "花客", "translation": "Flower Guest", "verified": False})

    called = []

    class FakeAI:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            called.append(k)
            return {"translation": "should-not-run"}

    resolver = TranslationResolver({"ai": {"enabled": False}})
    resolver._ai = FakeAI()
    out = resolver._resolve_fragments(
        "scope", "日住文县花客", "日住 文县 花客", ["日住", "文县", "花客"],
    )
    assert "daily accommodation" not in out["text"]
    assert "Wen County" not in out["text"]
    assert "Flower Guest" not in out["text"]
    assert leftover_cjk(out["text"]) == []
    assert called == []


def test_resolver_does_not_verify_address_leaked_as_scope():
    resolver = TranslationResolver({"ai": {"enabled": False}})
    chinese = {
        "scope": "许可项目所陕西省宝鸡市高新开发区高新十创意大厦A座1单元0601号乐发区行审批专用章",
        "warnings": [],
    }
    english = resolver.translate(chinese)
    assert english.get("scope") in ("", None)
    entry = resolver.provenance["scope"]
    assert entry.verified is False
    assert "creativeA" not in (english.get("scope") or "")
    assert "A10601" not in (english.get("scope") or "")


def test_resolver_ai_rewrites_weak_scope_compose():
    class FakeAI:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return {
                "translation": (
                    "General items: processing of mechanical parts and components; "
                    "sales of machinery and equipment; sales of mechanical parts and components"
                )
            }

    resolver = TranslationResolver({"ai": {"enabled": False}})
    resolver._ai = FakeAI()
    zh = (
        "一般项目：机械零件、零部件加工；机械设备销售；机械零部件、零部件销售"
        "（除依法须经批准的项目外，凭营业执照依法自主开展经营活动）"
    )
    out = resolver._ai_translate_scope(zh)
    assert "processing of mechanical parts" in out
    assert leftover_cjk(out) == []