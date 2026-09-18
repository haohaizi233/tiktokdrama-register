"""Pre-PDF semantic arbitration.

Workflow (no invented facts, no silent ship of garbage):

  Canonical Chinese
    → local glossary compose
    → AI rewrite only when English is already weak
    → translation validator (flags only, cannot overwrite)
    → THIS GATE
         1. rule_gate: address-as-scope, incoherent English, leftover CJK,
            blank term, empty name
         2. ai_gate: ZH↔EN semantic correspondence (severe mismatches only)
    → fail → 待人工处理 (PDF may still be written for inspection)
    → pass → 资料

AI cannot rewrite at this step. It only decides pass | fail.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .ai_client import AIClient, PROMPT_SEMANTIC_ARBITER
from .rule_repair import field_passes_rules, scope_text_is_plausible
from .translate import english_scope_is_weak, leftover_cjk

LOG = logging.getLogger("license_trans.arbiter")

_HOUSE_NO_RE = re.compile(r"\b\d{1,4}-\d{1,4}-\d{1,4}\b")
_ADDRESSY_EN_RE = re.compile(
    r"\b(?:urban and|city,|province,|subdistrict|village committee|"
    r"office building|former site)\b",
    re.I,
)


@dataclass
class ArbitrationResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    field_failures: dict[str, str] = field(default_factory=dict)
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "field_failures": dict(self.field_failures),
            "source": self.source,
        }

    def fail_message(self) -> str:
        if self.passed:
            return ""
        head = "语义仲裁未通过"
        detail = "; ".join(self.reasons[:4])
        return f"{head}: {detail}" if detail else head


def _zh(fields: dict, key: str) -> str:
    return str((fields or {}).get(key) or "").strip()


def _en(english: dict, key: str) -> str:
    return str((english or {}).get(key) or "").strip()


def rule_gate(fields: dict, english: dict) -> ArbitrationResult:
    """Deterministic pre-PDF checks. Empty reasons means the rule gate passed."""
    reasons: list[str] = []
    failures: dict[str, str] = {}

    zh_scope = _zh(fields, "scope")
    en_scope = _en(english, "scope")
    zh_addr = _zh(fields, "address")

    if not zh_scope:
        reasons.append("经营范围为空")
        failures["scope"] = "empty"
    elif not scope_text_is_plausible(zh_scope, zh_addr):
        reasons.append("经营范围像住所/印章/网址，不是经营项目")
        failures["scope"] = "implausible_zh"
    elif not en_scope:
        reasons.append("经营范围英译为空")
        failures["scope"] = "empty_en"
    elif leftover_cjk(en_scope):
        reasons.append("经营范围英译残留中文")
        failures["scope"] = "cjk"
    elif english_scope_is_weak(zh_scope, en_scope):
        reasons.append("经营范围英译语义不成立")
        failures["scope"] = "weak_en"
    elif _HOUSE_NO_RE.search(en_scope) and not re.search(
        r"\b(?:sales?|processing|manufactur|services?|wholesale|retail)\b",
        en_scope,
        re.I,
    ):
        reasons.append("经营范围英译含门牌号、缺少经营项目")
        failures["scope"] = "address_in_en"
    elif _ADDRESSY_EN_RE.search(en_scope) and not re.search(
        r"\b(?:sales?|processing|manufactur|services?|wholesale|retail)\b",
        en_scope,
        re.I,
    ):
        reasons.append("经营范围英译像地址")
        failures["scope"] = "address_in_en"

    en_name = _en(english, "name")
    if not en_name:
        reasons.append("英译名称为空")
        failures["name"] = "empty"
    elif leftover_cjk(en_name):
        reasons.append("英译名称残留中文")
        failures["name"] = "cjk"

    for field in ("address", "legal_person", "type", "authority"):
        bits = leftover_cjk(_en(english, field))
        if bits:
            reasons.append(f"英译残留中文({field})")
            failures[field] = "cjk"

    en_term = _en(english, "term")
    if re.search(r"to\s*\(blank\)", en_term, re.I):
        reasons.append("营业期限英译不完整")
        failures["term"] = "blank_end"

    zh_term = _zh(fields, "term")
    if zh_term and not field_passes_rules("term", zh_term):
        reasons.append("营业期限中文不完整")
        failures.setdefault("term", "incomplete_zh")

    zh_type = _zh(fields, "type")
    if zh_type and not field_passes_rules("type", zh_type):
        reasons.append("类型不是公司类型")
        failures["type"] = "invalid"

    warnings = list((fields or {}).get("warnings") or [])
    warnings += list((english or {}).get("warnings") or [])
    if "credit_code_checksum" in warnings:
        reasons.append("信用代码校验失败")
        failures["credit_code"] = "checksum"

    for tag in warnings:
        if str(tag).endswith("_needs_review") and str(tag).startswith("en_scope"):
            if "scope" not in failures and (
                not en_scope or english_scope_is_weak(zh_scope, en_scope)
            ):
                reasons.append("翻译校验判定经营范围需复核")
                failures["scope"] = "validator_review"

    return ArbitrationResult(
        passed=not reasons,
        reasons=reasons,
        field_failures=failures,
        source="rule",
    )


_SYSTEM_PROMPT = """\
You are the final gate before a certified English translation of a Chinese
business license (营业执照) is issued as a PDF.

Compare the Chinese fields with the English fields.

FAIL only for severe problems:
- 经营范围 Chinese is actually an address, URL, seal, or watermark.
- English 经营范围 is incoherent, leftover house numbers, or does not
  contain the main business activities present in Chinese.
- Company name English is not a translation of the Chinese name.
- Dates that are impossible (issue date years before establishment by >1 year
  without explanation), or Business Term ending in "(blank)".
- English still contains Chinese characters.

Do NOT fail for style, comma vs semicolon, "Co., Ltd." vs "Limited",
or minor word-order differences in addresses.

You must NOT rewrite any field. Decision only.

Return JSON only:
{
  "decision": "pass" | "fail",
  "confidence": 0.0,
  "reasons": ["..."],
  "field_failures": {"scope": "short reason"}
}
"""


def ai_gate(
    fields: dict,
    english: dict,
    config: dict | None = None,
    client: AIClient | None = None,
) -> ArbitrationResult | None:
    """Optional AI correspondence check. None if AI is unavailable."""
    ai = client or AIClient(config)
    if not ai.is_available():
        return None
    payload = {
        "chinese": {
            k: _zh(fields, k)
            for k in (
                "name", "type", "legal_person", "scope", "address",
                "capital", "established", "term", "authority", "issue_date",
                "credit_code",
            )
        },
        "english": {
            k: _en(english, k)
            for k in (
                "name", "type", "legal_person", "scope", "address",
                "capital", "established", "term", "authority", "issue_date",
            )
        },
    }
    result = ai.chat_json(
        _SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False),
        temperature=0.0,
        max_tokens=600,
        prompt_version=PROMPT_SEMANTIC_ARBITER,
    )
    if not isinstance(result, dict):
        return None
    decision = str(result.get("decision") or "pass").strip().lower()
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    reasons = [str(x) for x in (result.get("reasons") or []) if str(x).strip()]
    failures = result.get("field_failures") or {}
    if not isinstance(failures, dict):
        failures = {}
    failures = {str(k): str(v) for k, v in failures.items() if k}
    if decision != "fail":
        return ArbitrationResult(passed=True, source="ai")
    if confidence < 0.7 and not failures:
        LOG.info("[AI] 语义仲裁 fail 但置信不足 (%.2f), 放行", confidence)
        return ArbitrationResult(passed=True, source="ai")
    if not reasons:
        reasons = ["英译与中文严重不对应"]
    return ArbitrationResult(
        passed=False,
        reasons=reasons,
        field_failures=failures,
        source="ai",
    )


def arbitrate(
    fields: dict,
    english: dict,
    config: dict | None = None,
    client: AIClient | None = None,
) -> ArbitrationResult:
    """Run rule gate, then AI gate when rules passed."""
    ruled = rule_gate(fields, english)
    if not ruled.passed:
        LOG.warning("[ARB] 规则闸门未过: %s", "; ".join(ruled.reasons[:4]))
        return ruled
    judged = ai_gate(fields, english, config, client)
    if judged is None:
        return ruled
    if not judged.passed:
        LOG.warning("[ARB] AI 闸门未过: %s", "; ".join(judged.reasons[:4]))
        return judged
    return judged if judged.source == "ai" else ruled


def fail_reason(fields: dict, english: dict, config: dict | None = None) -> str:
    """Human-readable fail string, or empty if the conversion may enter 资料."""
    return arbitrate(fields, english, config).fail_message()
