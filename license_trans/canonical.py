"""Canonical Chinese Record — the validated, post-repair Chinese data
that feeds into the translation layer.

Flow:
  raw OCR → parse_license → rule validation → AI validation → repair
  → Canonical Chinese Record → translation

The canonical record is the single source of truth for translation.
It includes per-field status, repair history, and AI validation results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any

from .field_state import FieldState, FieldStatus, FieldStateTracker
from .parse import LicenseFields

LOG = logging.getLogger("license_trans.canonical")


@dataclass
class CanonicalChineseRecord:
    """Validated Chinese license data ready for translation."""
    credit_code: str = ""
    copy_no: str = "1-1"
    name: str = ""
    type: str = ""
    legal_person: str = ""
    scope: str = ""
    capital: str = ""
    established: str = ""
    address: str = ""
    term: str = ""
    authority: str = ""
    issue_date: str = ""
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)
    field_states: dict[str, Any] = field(default_factory=dict)
    repairs: list[dict] = field(default_factory=list)
    frozen: bool = False
    trace_id: str = ""
    ocr_primary: str = ""
    ocr_actual: str = ""
    fallback_used: bool = False
    ai_validated: bool = False
    needs_review: list[str] = field(default_factory=list)

    def freeze(self) -> None:
        object.__setattr__(self, "frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "frozen" and getattr(self, "frozen", False):
            raise AttributeError(
                f"CanonicalChineseRecord is frozen; cannot set {name}"
            )
        object.__setattr__(self, name, value)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_fields(
        cls,
        fields: LicenseFields,
        tracker: FieldStateTracker | None = None,
    ) -> "CanonicalChineseRecord":
        """Build canonical record from parsed fields and state tracker."""
        record = cls(
            credit_code=fields.credit_code,
            copy_no=fields.copy_no,
            name=fields.name,
            type=fields.type,
            legal_person=fields.legal_person,
            scope=fields.scope,
            capital=fields.capital,
            established=fields.established,
            address=fields.address,
            term=fields.term,
            authority=fields.authority,
            issue_date=fields.issue_date,
            duplicate=fields.duplicate,
            warnings=list(fields.warnings),
        )

        if tracker is not None:
            record.field_states = tracker.to_dict()
            # Collect all repairs
            for fs in tracker.all_fields().values():
                for r in fs.repairs:
                    record.repairs.append({
                        "field": r.field,
                        "before": r.before,
                        "after": r.after,
                        "source": r.source,
                        "confidence": r.confidence,
                        "reason": r.reason,
                    })
            # Collect fields needing review
            record.needs_review = tracker.fields_needing_review()
            record.ai_validated = tracker.ai_round > 0 and not record.needs_review

        return record

    def to_translation_input(self) -> dict:
        """Return the dict format expected by translate_fields()."""
        return {
            "credit_code": self.credit_code,
            "copy_no": self.copy_no,
            "name": self.name,
            "type": self.type,
            "legal_person": self.legal_person,
            "scope": self.scope,
            "capital": self.capital,
            "established": self.established,
            "address": self.address,
            "term": self.term,
            "authority": self.authority,
            "issue_date": self.issue_date,
            "duplicate": self.duplicate,
            "warnings": list(self.warnings),
        }
