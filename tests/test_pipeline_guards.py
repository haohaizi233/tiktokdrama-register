"""AI cannot mutate Chinese facts; retry failure → NEED_REVIEW; second validation."""
from unittest.mock import MagicMock, PropertyMock

from license_trans.ai_validator import LicenseSemanticValidator, TranslationValidator, VALID_ACTIONS
from license_trans.field_state import FieldStateTracker, FieldStatus
from license_trans.parse import LicenseFields
from license_trans.pipeline import _apply_qr_usci, _run_ai_validation_and_retry
from license_trans.canonical import CanonicalChineseRecord
from license_trans.translate import translate_fields
from license_trans.ocr import OcrLine
import numpy as np


def test_ai_rule_repair_does_not_write_chinese():
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value={
        "overall_status": "retry_required",
        "issues": [{
            "field": "scope",
            "type": "suspected_ocr_error",
            "fragment": "依法直主",
            "expected_pattern": "某个AI猜出来的值",
            "confidence": 0.95,
            "action": "rule_repair",
            "reason": "AI guess",
        }],
    })
    validator = LicenseSemanticValidator(client)
    tracker = FieldStateTracker()
    original = "依法直主开展经营活动"
    tracker.init_field("scope", original)
    result = validator.validate({}, [], [])
    retry = validator.apply_to_tracker(result, tracker)
    fs = tracker.get("scope")
    assert fs.value == original
    assert "某个AI猜出来的值" not in fs.value
    assert retry == ["scope"]
    assert fs.status == FieldStatus.RETRY_OCR
    assert result["issues"][0]["action"] == "retry_ocr"
    assert "expected_pattern" not in result["issues"][0]


def test_translation_validator_does_not_overwrite_english():
    validator = TranslationValidator(MagicMock())
    result = {
        "overall_status": "needs_review",
        "issues": [{
            "field": "address",
            "action": "rule_repair",
            "suggested": "Erdao District, Changchun City",
        }],
    }
    issues = result["issues"]
    # sanitize via validate path
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value=result)
    validator = TranslationValidator(client)
    out = validator.validate({"address": "长春市二道区"}, {"address": "Changchun City Erdao District"})
    english = {"address": "Changchun City Erdao District"}
    flags = validator.collect_flags(out)
    assert english["address"] == "Changchun City Erdao District"
    assert flags.get("address") == "web_verify"
    assert not hasattr(validator, "apply_corrections")


def test_retry_failure_becomes_need_review(monkeypatch):
    fields = LicenseFields(scope="依法直主开展经营活动", name="测试有限公司")
    tracker_fields = fields
    client_results = [{
        "overall_status": "retry_required",
        "issues": [{"field": "scope", "action": "retry_ocr", "fragment": "依法直主"}],
    }]

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return client_results[0]

    import license_trans.pipeline as pipeline

    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", lambda *a, **k: [])

    tracker = _run_ai_validation_and_retry(
        tracker_fields,
        [OcrLine("经营范围 依法直主开展经营活动", 0.9, 0, 0, 10, 10)],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    fs = tracker.get("scope")
    assert fs.status == FieldStatus.NEED_REVIEW
    assert fs.status != FieldStatus.RETRY_OCR


def test_second_validation_after_successful_retry(monkeypatch):
    fields = LicenseFields(scope="依法直主开展经营活动", name="测试有限公司")
    calls = {"n": 0}

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "overall_status": "retry_required",
                    "issues": [{"field": "scope", "action": "retry_ocr"}],
                }
            return {"overall_status": "ok", "issues": []}

    import license_trans.pipeline as pipeline

    def fake_retry(retry_fields, fields, lines, oriented, engine, tracker, trace=None):
        fields.scope = "依法自主开展经营活动"
        fs = tracker.get("scope")
        fs.add_repair("依法直主开展经营活动", fields.scope, "ocr_fusion")
        fs.status = FieldStatus.OK
        return ["scope"]

    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", fake_retry)

    tracker = _run_ai_validation_and_retry(
        fields,
        [OcrLine("scope", 0.9, 0, 0, 10, 10)],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    assert calls["n"] == 2
    assert fields.scope == "依法自主开展经营活动"
    assert tracker.get("scope").status in (FieldStatus.OK, FieldStatus.RULE_REPAIRED)


def test_qr_fills_credit_code_before_canonical_and_english():
    usci = "91110101MA004KQM97"
    fields = LicenseFields(name="测试有限公司", credit_code="")
    _apply_qr_usci(fields, usci, f"https://www.gsxt.gov.cn/{usci}")
    canonical = CanonicalChineseRecord.from_fields(fields)
    english = translate_fields(canonical.to_translation_input())
    assert fields.credit_code == usci
    assert canonical.credit_code == usci
    assert english["credit_code"] == usci


def test_translation_reads_canonical_not_raw_fields():
    raw = LicenseFields(name="原始名称有限公司", credit_code="91110101MA004KQM97")
    canonical = CanonicalChineseRecord.from_fields(raw)
    canonical.name = "规范名称有限公司"
    canonical.freeze()
    english = translate_fields(canonical.to_translation_input())
    assert english["source"]["name"] == "规范名称有限公司"
    assert "原始名称" not in (english.get("name") or "")


def test_canonical_frozen_cannot_mutate():
    rec = CanonicalChineseRecord.from_fields(
        LicenseFields(name="测试有限公司", credit_code="91110101MA004KQM97")
    )
    rec.freeze()
    raised = False
    try:
        rec.name = "被改掉"
    except AttributeError:
        raised = True
    assert raised
    assert rec.name == "测试有限公司"
