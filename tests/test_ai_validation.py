"""Tests for AI validators and OCR ensemble/candidate fusion."""
from unittest.mock import MagicMock, patch, PropertyMock

from license_trans.ai_client import AIClient, load_ai_config, is_ai_enabled
from license_trans.ai_validator import (
    LicenseSemanticValidator, TranslationValidator, VALID_ACTIONS,
)
from license_trans.ocr_ensemble import OcrCandidate, OcrCandidateFusion
from license_trans.field_state import FieldStateTracker, FieldStatus


def test_ai_disabled_by_default():
    cfg = load_ai_config(None)
    assert cfg["enabled"] is False
    assert is_ai_enabled(None) is False


def test_ai_enabled_check():
    cfg = {"ai": {"enabled": True, "base_url": "http://x", "api_key": "k", "model": "m"}}
    assert is_ai_enabled(cfg) is True
    cfg = {"ai": {"enabled": True, "base_url": "", "api_key": "k", "model": "m"}}
    assert is_ai_enabled(cfg) is False


def test_ai_client_unavailable():
    client = AIClient(None)
    assert client.is_available() is False
    assert client.chat_json("sys", "user") is None


def test_validator_no_ai_returns_ok():
    client = AIClient(None)
    validator = LicenseSemanticValidator(client)
    result = validator.validate({}, [], [])
    assert result["overall_status"] == "ok"
    assert result["issues"] == []


def test_translation_validator_no_ai():
    client = AIClient(None)
    validator = TranslationValidator(client)
    result = validator.validate({}, {})
    assert result["overall_status"] == "ok"
    assert result["issues"] == []


def test_validator_sanitizes_invalid_actions():
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value={
        "overall_status": "retry_required",
        "issues": [
            {"field": "scope", "action": "DELETE_EVERYTHING", "confidence": 0.9},
            {"field": "name", "action": "needs_review", "confidence": 0.8},
        ]
    })
    validator = LicenseSemanticValidator(client)
    result = validator.validate({}, [], [])
    actions = [i["action"] for i in result["issues"]]
    assert "DELETE_EVERYTHING" not in actions
    assert "needs_review" in actions
    assert all(a in VALID_ACTIONS for a in actions)


def test_validator_apply_to_tracker():
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value={
        "overall_status": "retry_required",
        "issues": [
            {"field": "scope", "type": "suspected_ocr_error",
             "fragment": "依法直主", "expected_pattern": "某个AI猜出来的值",
             "confidence": 0.95, "action": "rule_repair",
             "reason": "常见OCR混淆"},
        ]
    })
    validator = LicenseSemanticValidator(client)
    tracker = FieldStateTracker()
    tracker.init_field("scope", "依法直主开展经营活动")
    result = validator.validate({}, [], [])
    retry_fields = validator.apply_to_tracker(result, tracker)
    fs = tracker.get("scope")
    assert retry_fields == ["scope"]
    assert fs.value == "依法直主开展经营活动"
    assert "某个AI猜出来的值" not in fs.value
    assert fs.status == FieldStatus.RETRY_OCR


def test_candidate_fusion_single():
    fusion = OcrCandidateFusion()
    c = OcrCandidate(text="测试", score=0.9, engine="ppocr_v6")
    result = fusion.fuse([c])
    assert result is not None
    assert result.text == "测试"


def test_candidate_fusion_agreement():
    fusion = OcrCandidateFusion()
    candidates = [
        OcrCandidate(text="金属材料销售", score=1.0, engine="wechat"),
        OcrCandidate(text="金属材料销售", score=0.93, engine="ppocr_v6"),
        OcrCandidate(text="金属材科销售", score=0.95, engine="ppocr_2x"),
    ]
    result = fusion.fuse(candidates)
    assert result.text == "金属材料销售"  # 2/3 agreement


def test_candidate_fusion_usci_checksum():
    fusion = OcrCandidateFusion()
    candidates = [
        OcrCandidate(text="91110101MA004KQM97", score=0.9, engine="ppocr_v6"),
        OcrCandidate(text="O1110101MA004KQM97", score=0.95, engine="wechat"),
    ]
    # The one with valid checksum should win
    from license_trans.usci import checksum_ok
    result = fusion.fuse_field("credit_code", candidates)
    assert result is not None
    assert checksum_ok(result.text)


def test_translation_validator_does_not_apply_corrections():
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value={
        "overall_status": "needs_review",
        "issues": [
            {"field": "address", "action": "rule_repair",
             "suggested": "Erdao District, Changchun City"},
        ]
    })
    validator = TranslationValidator(client)
    result = validator.validate(
        {"address": "长春市二道区"},
        {"address": "Changchun City Erdao District"},
    )
    english = {"address": "Changchun City Erdao District"}
    flags = validator.collect_flags(result)
    assert english["address"] == "Changchun City Erdao District"
    assert flags["address"] == "web_verify"
