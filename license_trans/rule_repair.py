"""Deterministic repairs for Chinese license fields.

AI is not allowed to invent replacements. Only unique, local, rule-based
fixes are applied to Chinese facts.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .field_state import FieldStateTracker, FieldStatus
from .parse import DATE_RE, OCR_FIXES, LicenseFields, clip_scope_text
from .usci import checksum_ok, repair_usci

LOG = logging.getLogger("license_trans.rule")

_NUM_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")

# Real 经营范围 items. Bare「许可项目」without a delimiter is not enough —
# harvest used to prefix it onto 住所 text (Baoji Jindikang).
_SCOPE_BUSINESS_RE = re.compile(
    r"一般项目|一般经营项目|许可经营项目|"
    r"许可项目[:：]|"
    r"销售|制造|加工|服务|批发|零售|技术|租赁|回收|种植|生产|"
    r"咨询|运输|研发|维修|安装|收购|设计|施工|劳务|餐饮|食品|"
    r"软件|互联网|进出口|分包|饲养|养殖|仓储|培训|修理|印刷|"
    r"广告|经纪|代理|教育|办学|诊疗|医疗|货运|客运|"
    r"开发(?!区)|研究|检测|检验|保洁|清洗|消毒"
)
_SCOPE_ADDRESS_LEAK_RE = re.compile(
    r"审批专用章|登记机关|"
    r"(?:https?://|www\.|gsxt|\.gov)|"
    r"(?:省|市|自治区).{0,16}(?:市|区|县).{0,24}(?:路|街|道|巷|大厦)|"
    r"(?:大厦|座).{0,12}(?:单元|室).{0,8}号"
)
_SCOPE_ADDRESS_FRAG_RE = re.compile(
    r"办公楼|旧址|村委会|审批专用章|许可项目住|许可项目所"
)
_SCOPE_URL_RE = re.compile(r"(?:https?://|www\.|gsxt|\.gov)", re.I)


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
        rest = raw[m.end():]
        if "长期" in raw:
            term = f"{fixed}至长期"
            return (term, "date_format") if term != raw else None
        if "至" in rest:
            m2 = DATE_RE.search(rest)
            if m2:
                end = f"{int(m2.group(1)):04d}年{int(m2.group(2)):02d}月{int(m2.group(3)):02d}日"
                term = f"{fixed}至{end}"
                return (term, "date_format") if term != raw else None
            rest_compact = re.sub(r"[\s\u3000_—－]+", "", rest)
            if re.fullmatch(r"至(?:年月日)*", rest_compact):
                term = f"{fixed}至长期"
                return (term, "term_blank_end") if term != raw else None
            return None
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


def _repair_division(text: str, prefer: list[str] | None = None) -> tuple[str, str] | None:
    if not text:
        return None
    try:
        from .divisions import snap_text
    except Exception as exc:
        LOG.exception("division lookup failed: %s", exc)
        return None
    snapped = snap_text(text, prefer)
    if snapped and snapped != text:
        return snapped, "division_unique"
    return None


def repair_field_value(
    field: str,
    value: str,
    prefer: list[str] | None = None,
) -> tuple[str, str] | None:
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
        return _repair_division(value, prefer)
    return None


def apply_deterministic_repairs(
    fields: LicenseFields,
    tracker: FieldStateTracker,
) -> list[dict[str, Any]]:
    """Mutate fields/tracker with deterministic repairs only."""
    from .divisions import places_from_usci

    applied: list[dict[str, Any]] = []
    prefer = places_from_usci(getattr(fields, "credit_code", "") or "")
    attrs = (
        "credit_code", "name", "type", "legal_person", "scope", "capital",
        "established", "address", "term", "authority", "issue_date",
    )
    for attr in attrs:
        current = getattr(fields, attr, "") or ""
        result = repair_field_value(attr, current, prefer)
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


def scope_text_is_plausible(value: str, address: str = "") -> bool:
    """True when text looks like 经营范围 rather than a URL, 住所, or stamp."""
    raw = (value or "").strip()
    if not raw:
        return False
    clipped = clip_scope_text(raw) or raw
    value = clipped.strip()
    t = re.sub(r"[\s\u3000]+", "", value)
    t_nosep = re.sub(r"[:：]", "", t)
    if compact_is_label(value, "经营范围") or len(t_nosep) < 4:
        return False
    if t_nosep.startswith("经营范") and not t_nosep.startswith("经营范围"):
        return False
    if re.search(r"(?:SC)?JDGL", value, re.I):
        return False
    if any(k in t_nosep for k in ("法定代表人", "成立日期", "注册资本", "统一社会信用")):
        return False
    if re.search(r"(有限责任公司|有限公司|股份有限公司)$", t_nosep) and "项目" not in t_nosep:
        return False
    if _SCOPE_URL_RE.search(value) or re.fullmatch(r"[A-Za-z0-9:./_\-]+", value):
        return False
    if "审批专用章" in t:
        return False
    if _SCOPE_ADDRESS_FRAG_RE.search(t):
        return False
    has_biz = bool(_SCOPE_BUSINESS_RE.search(t))
    header_stripped = re.sub(
        r"^(?:许可经营项目|许可项目|一般经营项目|一般项目)[:：]?",
        "",
        t,
    )
    if re.match(r"许可项目所", t):
        return False
    if re.match(r"许可项目所?[省市县]", t) and not _SCOPE_BUSINESS_RE.search(t[4:]):
        return False
    if re.search(
        r"(?:省|市|自治区).{0,20}(?:市|区|县).{0,32}(?:路|街|道|巷).{0,24}\d",
        t,
    ):
        return False
    if _SCOPE_ADDRESS_LEAK_RE.search(t) and not _SCOPE_BUSINESS_RE.search(header_stripped):
        return False
    addr = re.sub(r"[\s\u3000]+", "", address or "")
    if addr and len(addr) >= 10 and addr[:10] in t:
        return False
    if addr:
        house = re.search(r"\d{1,4}-\d{1,4}-\d{1,4}", addr)
        if house and house.group(0) in t:
            return False
    if not has_biz:
        return False
    return True


def field_passes_rules(field: str, value: str) -> bool:
    """True if the value satisfies the deterministic checks for this field."""
    value = (value or "").strip()
    if not value:
        return False
    if field in ("credit_code", "usci"):
        # A checksum-valid G-prefix window (watermark glue + one confusable
        # repair) is not a company USCI. Real codes start with a digit.
        return checksum_ok(value) and value[0].isdigit()
    if field in ("established", "issue_date"):
        return bool(DATE_RE.search(value) or _NUM_DATE_RE.fullmatch(value))
    if field == "scope":
        return scope_text_is_plausible(value)
    if field == "type":
        t = re.sub(r"[\s\u3000]+", "", value)
        if t in ("型", "类", "类型") or len(t) < 4:
            return False
        return bool(re.search(r"有限责任公司|股份有限公司|有限公司|合伙企业|个人独资", t))
    if field == "authority":
        return "局" in value and "监制" not in value and len(value) >= 4
    if field == "term":
        if "长期" in value:
            return True
        dates = DATE_RE.findall(value)
        return "至" in value and len(dates) >= 2
    if field in ("name", "address", "legal_person"):
        return len(value) >= 2 and value not in ("名称", "住所", "地址", "法定代表人")
    return True


def compact_is_label(value: str, label: str) -> bool:
    t = re.sub(r"[\s\u3000：:]+", "", value or "")
    return t == label.replace(" ", "")
