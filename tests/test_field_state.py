"""Tests for field state model and canonical record."""
from license_trans.field_state import (
    FieldState, FieldStatus, FieldStateTracker, RepairRecord, CRITICAL_FIELDS,
)
from license_trans.canonical import CanonicalChineseRecord
from license_trans.parse import LicenseFields


def test_field_status_enum():
    assert FieldStatus.OK == "ok"
    assert FieldStatus.NEED_REVIEW == "need_review"
    assert FieldStatus.RETRY_OCR == "retry_ocr"


def test_field_state_needs_retry():
    fs = FieldState(name="scope", value="test", status=FieldStatus.RETRY_OCR)
    assert fs.needs_retry is True
    assert fs.needs_review is False


def test_field_state_needs_review():
    fs = FieldState(name="name", value="", status=FieldStatus.NEED_REVIEW)
    assert fs.needs_review is True
    assert fs.needs_retry is False


def test_field_state_add_repair():
    fs = FieldState(name="usci", value="91XXX", status=FieldStatus.SUSPECTED_OCR_ERROR)
    fs.add_repair("91XXX", "91110108MA01ABCDEF", "checksum", 1.0, "USCI checksum fix")
    assert fs.value == "91110108MA01ABCDEF"
    assert fs.status == FieldStatus.RULE_REPAIRED
    assert len(fs.repairs) == 1
    assert fs.repairs[0].source == "checksum"


def test_tracker_init_field():
    tracker = FieldStateTracker()
    fs = tracker.init_field("name", "测试公司", 0.95)
    assert fs.value == "测试公司"
    assert fs.ocr_score == 0.95
    assert tracker.get("name") is fs


def test_tracker_retry_limit():
    tracker = FieldStateTracker()
    tracker.max_retries = 2
    tracker.init_field("scope", "test")
    assert tracker.can_retry("scope") is True
    tracker.mark_retry("scope")
    tracker.mark_retry("scope")
    assert tracker.can_retry("scope") is False


def test_tracker_missing_critical():
    tracker = FieldStateTracker()
    for f in CRITICAL_FIELDS:
        tracker.init_field(f, "")
    assert sorted(tracker.missing_critical()) == sorted(CRITICAL_FIELDS)
    tracker.init_field("name", "有值")
    assert "name" not in tracker.missing_critical()


def test_tracker_fields_needing_retry():
    tracker = FieldStateTracker()
    tracker.init_field("scope", "", status=FieldStatus.RETRY_OCR)
    tracker.init_field("name", "OK", status=FieldStatus.OK)
    assert tracker.fields_needing_retry() == ["scope"]


def test_canonical_from_fields():
    fields = LicenseFields(
        name="测试公司",
        credit_code="91110108MA01ABCDEF",
        scope="技术服务",
        legal_person="张三",
        capital="100万元",
    )
    record = CanonicalChineseRecord.from_fields(fields)
    assert record.name == "测试公司"
    assert record.credit_code == "91110108MA01ABCDEF"
    assert record.scope == "技术服务"
    assert record.needs_review == []


def test_canonical_to_translation_input():
    fields = LicenseFields(name="测试公司", credit_code="91110108MA01ABCDEF")
    record = CanonicalChineseRecord.from_fields(fields)
    inp = record.to_translation_input()
    assert inp["name"] == "测试公司"
    assert inp["credit_code"] == "91110108MA01ABCDEF"
    assert "warnings" in inp


def test_canonical_with_tracker():
    fields = LicenseFields(name="测试公司", credit_code="91110108MA01ABCDEF")
    tracker = FieldStateTracker()
    tracker.init_field("name", "测试公司")
    tracker.init_field("credit_code", "91110108MA01ABCDEF")
    fs = tracker.get("credit_code")
    fs.add_repair("91XXX", "91110108MA01ABCDEF", "checksum", 1.0, "Fixed")
    record = CanonicalChineseRecord.from_fields(fields, tracker)
    assert len(record.repairs) == 1
    assert record.repairs[0]["source"] == "checksum"
