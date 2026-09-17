"""Deterministic repairs for Chinese license fields.

AI is not allowed to invent replacements. Only unique, local, rule-based
fixes are applied to Chinese facts.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .field_state import FieldStateTracker, FieldStatus
from .parse import DATE_RE, OCR_FIXES, LicenseFields
from .usci import checksum_ok, repair_usci

LOG = logging.getLogger("license_trans.rule")

_NUM_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")


def _unique_ocr_fix(text: str) -> tuple[str, str] | None:
    """Apply OCR_FIXES; return (after, reason) if exactly the text changed."""
    if not text:
        return None
    updated = text
    applied: list[str] = []
    for src, dst in OCR_FIXES:
        if src in updated:
            updated = updated.replace(src, dst)
            applied.append(f"{src}→{dst}")
    if updated != text:
        return updated, "ocr_phrase:" + ",".join(applied)
    return None


def _repair_date(text: str) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = DATE_RE.search(raw)
    if m:
        fixed = f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
        if fixed != raw:
            return fixed, "date_format"
        return None
    m = _NUM_DATE_RE.fullmatch(raw)
    if m:
        return (
            f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日",
            "date_format",
        )
    return None


def _repair_usci_field(text: str) -> tuple[str, str] | None:
    code = (text or "").strip().upper().replace(" ", "")
    if not code:
        return None
    if checksum_ok(code):
        if code != text:
            return code, "usci_normalize"
        return None
    repaired = repair_usci(code)
    if repaired != code and checksum_ok(repaired):
        return repaired, "usci_checksum"
    return None


def _repair_division(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    try:
        from .divisions import snap_text
    except Exception as exc:
        LOG.exception("division lookup failed: %s", exc)
        return None
    snapped = snap_text(text)
    if snapped and snapped != text:
        return snapped, "division_unique"
    return None


def repair_field_value(field: str, value: str) -> tuple[str, str] | None:
    """Return (new_value, reason) if a unique deterministic repair exists."""
    if field in ("credit_code", "usci"):
        return _repair_usci_field(value)
    if field in ("established", "issue_date", "term"):
        date_fix = _repair_date(value)
        if date_fix:
            return date_fix
    phrase = _unique_ocr_fix(value)
    if phrase:
        return phrase
    if field in ("address", "authority"):
        return _repair_division(value)
    return None


def apply_deterministic_repairs(
    fields: LicenseFields,
    tracker: FieldStateTracker,
) -> list[dict[str, Any]]:
    """Mutate fields/tracker with deterministic repairs only."""
    applied: list[dict[str, Any]] = []
    attrs = (
        "credit_code", "name", "type", "legal_person", "scope", "capital",
        "established", "address", "term", "authority", "issue_date",
    )
    for attr in attrs:
        current = getattr(fields, attr, "") or ""
        result = repair_field_value(attr, current)
        if not result:
            continue
        new_val, reason = result
        if new_val == current:
            continue
        fs = tracker.get(attr)
        if fs is None:
            fs = tracker.init_field(attr, current)
        fs.add_repair(current, new_val, "rule", reason=reason)
        setattr(fields, attr, new_val)
        applied.append({"field": attr, "before": current, "after": new_val, "reason": reason})
        LOG.info("[RULE] %s: %s → %s (%s)", attr, current[:40], new_val[:40], reason)
    return applied


def field_passes_rules(field: str, value: str) -> bool:
    """True if the value satisfies the deterministic checks for this field."""
    value = (value or "").strip()
    if not value:
        return False
    if field in ("credit_code", "usci"):
        return checksum_ok(value)
    if field in ("established", "issue_date"):
        return bool(DATE_RE.search(value) or _NUM_DATE_RE.fullmatch(value))
    if field == "scope":
        labels = compact_is_label(value, "经营范围")
        return (not labels) and len(value) >= 2
    if field in ("name", "address", "legal_person"):
        return len(value) >= 2 and value not in ("名称", "住所", "地址", "法定代表人")
    return True


def compact_is_label(value: str, label: str) -> bool:
    t = re.sub(r"[\s\u3000：:]+", "", value or "")
    return t == label.replace(" ", "")
