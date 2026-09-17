"""AI-powered semantic validation for parsed license fields and translation review.

LicenseSemanticValidator flags OCR issues. It cannot write Chinese facts.
Actions: accept | retry_ocr | needs_review

TranslationValidator flags translation issues. It cannot overwrite English.
Actions: pass | web_verify | needs_review
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .ai_client import (
    AIClient,
    PROMPT_LICENSE_VALIDATOR,
    PROMPT_TRANSLATION_VALIDATOR,
)
from .field_state import FieldStatus, FieldStateTracker

LOG = logging.getLogger("license_trans.ai")

VALID_LICENSE_ACTIONS = {"accept", "retry_ocr", "needs_review"}
# Legacy model outputs are remapped; never applied as Chinese fact edits.
LEGACY_LICENSE_ACTIONS = {"rule_repair", "ai_repair", "replace"}
VALID_TRANSLATION_ACTIONS = {"pass", "accept", "web_verify", "needs_review", "suspected_error"}
# Keep a combined set so older tests that import VALID_ACTIONS still work.
VALID_ACTIONS = VALID_LICENSE_ACTIONS | LEGACY_LICENSE_ACTIONS | VALID_TRANSLATION_ACTIONS


def _client_ready(client: Any) -> bool:
    avail = getattr(client, "is_available", False)
    if callable(avail):
        return bool(avail())
    return bool(avail)


_SYSTEM_PROMPT_LICENSE = """\
You are a Chinese business license (营业执照) OCR quality auditor.

You receive the parsed fields from an OCR'd business license, along with
the raw OCR text and any rule-engine warnings.

Your job:
1. Detect fields that look like OCR errors (wrong characters, missing text).
2. Detect field misplacement (e.g. address text in legal_person field).
3. Detect scope truncation (incomplete 经营范围).
4. Detect date anomalies (impossible dates, swapped established/issue date).
5. Detect company type anomalies.
6. Detect address anomalies (missing district, wrong province).

CRITICAL RULES:
- You must NOT generate new field values. You cannot repair text.
- For each issue, suggest an action from: accept, retry_ocr, needs_review.
- "retry_ocr" means the field should be re-OCR'd at higher resolution.
- "needs_review" means a human must check.
- If you suspect a fixed license phrase was misread (e.g. 依法直主开展经营活动),
  use action retry_ocr and put the fragment in "fragment". Do not output a
  replacement string to be written into the record.
- Output ONLY valid JSON, no prose.

Output format:
{
  "overall_status": "ok" | "retry_required" | "needs_review",
  "issues": [
    {
      "field": "scope",
      "type": "suspected_ocr_error",
      "fragment": "依法直主开展经营活动",
      "reason": "常见营业执照固定表述疑似识别错误",
      "confidence": 0.95,
      "action": "retry_ocr"
    }
  ]
}
"""


class LicenseSemanticValidator:
    """Validates parsed Chinese license fields using AI. Never writes facts."""

    def __init__(self, client: AIClient) -> None:
        self._client = client

    def validate(
        self,
        parsed: dict,
        raw_ocr_texts: list[str],
        warnings: list[str],
    ) -> dict:
        if not _client_ready(self._client):
            return {"overall_status": "ok", "issues": []}

        user_content = json.dumps({
            "parsed": parsed,
            "raw_ocr": raw_ocr_texts[:30],
            "warnings": warnings,
        }, ensure_ascii=False, indent=2)

        result = self._client.chat_json(
            _SYSTEM_PROMPT_LICENSE,
            user_content,
            temperature=0.1,
            max_tokens=2000,
            prompt_version=PROMPT_LICENSE_VALIDATOR,
        )
        if result is None:
            return {"overall_status": "ok", "issues": []}

        issues = result.get("issues", [])
        clean_issues = []
        for issue in issues:
            action = issue.get("action", "accept")
            if action in LEGACY_LICENSE_ACTIONS:
                LOG.warning(
                    "[AI] Rejected fact-mutating action %s on %s",
                    action, issue.get("field"),
                )
                action = "retry_ocr"
            if action not in VALID_LICENSE_ACTIONS:
                action = "needs_review"
            issue["action"] = action
            issue.pop("expected_pattern", None)
            clean_issues.append(issue)
        result["issues"] = clean_issues
        return result

    def apply_to_tracker(
        self,
        result: dict,
        tracker: FieldStateTracker,
    ) -> list[str]:
        """Apply AI flags to tracker. Never copies AI text into field values.

        Returns list of field names that need retry.
        """
        retry_fields: list[str] = []
        for issue in result.get("issues", []):
            field_name = issue.get("field", "")
            action = issue.get("action", "accept")
            fs = tracker.get(field_name)
            if fs is None:
                continue
            fs.ai_issues.append(issue)
            if action == "retry_ocr":
                fs.status = FieldStatus.RETRY_OCR
                retry_fields.append(field_name)
            elif action == "needs_review":
                fs.status = FieldStatus.NEED_REVIEW
        return retry_fields


_SYSTEM_PROMPT_TRANSLATION = """\
You are a professional Chinese-to-English business license translation reviewer.

You receive the original Chinese fields and their English translations.

Your job:
1. Check if place names (provinces, cities, districts) are translated correctly.
2. Check if company names follow standard English naming conventions.
3. Check if official body names (市场监督管理局, 行政审批局) are correct.
4. Check if technical/business terms in scope are translated properly.
5. Check if dates, capital amounts, and terms are formatted correctly.
6. Flag any remaining untranslated Chinese text.

CRITICAL RULES:
- You must NOT rewrite translations. You cannot replace the English text.
- For each issue, suggest an action from: pass, web_verify, needs_review.
- "web_verify" means an official-source check should be attempted.
- "needs_review" means a human translator must check.
- Output ONLY valid JSON, no prose.

Output format:
{
  "overall_status": "ok" | "needs_review",
  "issues": [
    {
      "field": "address",
      "type": "place_name_error",
      "chinese": "长春市二道区",
      "english": "Changchun City Erdao District",
      "reason": "Standard English address order is smallest-to-largest",
      "confidence": 0.9,
      "action": "web_verify"
    }
  ]
}
"""


class TranslationValidator:
    """Reviews English translations. Never overwrites the English dict."""

    def __init__(self, client: AIClient) -> None:
        self._client = client

    def validate(self, chinese: dict, english: dict) -> dict:
        if not _client_ready(self._client):
            return {"overall_status": "ok", "issues": []}

        user_content = json.dumps({
            "chinese": chinese,
            "english": english,
        }, ensure_ascii=False, indent=2)

        result = self._client.chat_json(
            _SYSTEM_PROMPT_TRANSLATION,
            user_content,
            temperature=0.1,
            max_tokens=2000,
            prompt_version=PROMPT_TRANSLATION_VALIDATOR,
        )
        if result is None:
            return {"overall_status": "ok", "issues": []}

        issues = result.get("issues", [])
        for issue in issues:
            action = issue.get("action", "pass")
            if action in ("rule_repair", "replace", "suggested"):
                LOG.warning(
                    "[AI] Rejected translation overwrite action %s on %s",
                    action, issue.get("field"),
                )
                action = "web_verify"
            if action == "accept":
                action = "pass"
            if action == "retry_ocr":
                action = "needs_review"
            if action not in VALID_TRANSLATION_ACTIONS:
                action = "needs_review"
            issue["action"] = action
        result["issues"] = issues
        return result

    def collect_flags(self, result: dict) -> dict[str, str]:
        """Map field → action without mutating translations."""
        flags: dict[str, str] = {}
        for issue in result.get("issues", []):
            field_name = issue.get("field", "")
            action = issue.get("action", "pass")
            if field_name and action in ("web_verify", "needs_review", "suspected_error"):
                flags[field_name] = action
        return flags
