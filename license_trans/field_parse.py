"""Field-specific parsers for a complete field-crop OCR result.

A candidate is one engine × one preprocess variant over the whole crop,
not one OCR line.
"""
from __future__ import annotations

import re

from .ocr import OcrLine

FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "name": ("名称", "名 称"),
    "type": ("类型", "类 型"),
    "legal_person": ("法定代表人",),
    "scope": ("经营范围", "经 营 范 围"),
    "capital": ("注册资本", "注册资金", "实收资本"),
    "established": ("成立日期", "注册日期"),
    "address": ("住所", "经营场所", "营业场所"),
    "term": ("营业期限", "经营期限"),
    "authority": ("登记机关",),
    "issue_date": ("发证日期", "核准日期", "发照日期"),
    "credit_code": ("统一社会信用代码", "社会信用代码", "信用代码"),
}

_LABEL_SEPS = ("：", ":", " ")
_PUNCT_END = ("；", ";", "。", "，", ",", "、", "：", ":")
_SPACE_RE = re.compile(r"[\s\u3000]+")


def compact_text(text: str) -> str:
    return _SPACE_RE.sub("", text or "")


def _label_forms(labels: tuple[str, ...]) -> list[str]:
    forms: list[str] = []
    for lab in labels:
        forms.append(compact_text(lab))
    return sorted(set(forms), key=len, reverse=True)


def is_label_only(text: str, field: str) -> bool:
    """True if text is only the field label (plus optional colon)."""
    labels = FIELD_LABELS.get(field)
    if not labels:
        return False
    t = compact_text(text).rstrip("：:")
    return t in _label_forms(labels)


def strip_field_label(text: str, field: str) -> str:
    """Remove a leading field label from a value string."""
    labels = FIELD_LABELS.get(field)
    if not labels:
        return (text or "").strip()
    raw = (text or "").strip()
    t = compact_text(raw)
    for lab in _label_forms(labels):
        if t == lab or t == lab + "：" or t == lab + ":":
            return ""
        if t.startswith(lab):
            rest = t[len(lab):]
            return rest.lstrip("：:")
    return t


def _sort_lines(lines: list[OcrLine]) -> list[OcrLine]:
    return sorted(lines, key=lambda ln: (round(ln.y0 / 8.0), ln.x0))


def _join_scope(parts: list[str]) -> str:
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not out:
            out.append(part)
            continue
        prev = out[-1]
        if prev.endswith(_PUNCT_END) or part.startswith(_PUNCT_END):
            out.append(part)
        else:
            out.append(part)
    return "".join(out)


def _join_address(parts: list[str]) -> str:
    return "".join(p.strip() for p in parts if p.strip())


def _join_generic(parts: list[str]) -> str:
    return "".join(p.strip() for p in parts if p.strip())


def parse_field_lines(field: str, lines: list[OcrLine]) -> str:
    """Turn OCR lines from a field crop into one complete field string.

    Drops label-only lines, strips a leading label from the first value
    line, then merges remaining lines in bbox order.
    """
    if not lines:
        return ""
    ordered = _sort_lines(lines)
    parts: list[str] = []
    for ln in ordered:
        text = (ln.text or "").strip()
        if not text:
            continue
        if is_label_only(text, field):
            continue
        stripped = strip_field_label(text, field)
        if not stripped:
            continue
        if is_label_only(stripped, field):
            continue
        parts.append(stripped)

    if field == "scope":
        return _join_scope(parts)
    if field == "address":
        return _join_address(parts)
    if field == "name":
        return _join_generic(parts)
    return _join_generic(parts)
