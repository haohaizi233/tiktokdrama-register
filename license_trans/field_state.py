"""Field-level state model for tracking OCR → parse → validation → repair pipeline.

Each critical field (name, credit_code, scope, legal_person, capital, address,
type, term, authority, issue_date, established) carries a status and optional
repair log so the pipeline can make informed retry/review decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldStatus(str, Enum):
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"
    SUSPECTED_OCR_ERROR = "suspected_ocr_error"
    RETRY_OCR = "retry_ocr"
    RULE_REPAIRED = "rule_repaired"
    AI_REPAIRED = "ai_repaired"
    AMBIGUOUS = "ambiguous"
    NEED_REVIEW = "need_review"


# Fields that the pipeline considers "critical" — missing any of these
# triggers high-resolution retry or field-level re-OCR.
CRITICAL_FIELDS = (
    "name",
    "credit_code",
    "scope",
    "legal_person",
    "capital",
)


@dataclass
class RepairRecord:
    """Single repair event for a field."""
    field: str
    before: str
    after: str
    source: str  # "checksum" | "division" | "rule" | "ai" | "ocr_fusion"
    confidence: float = 1.0
    reason: str = ""


@dataclass
class FieldState:
    """Per-field state tracking through the pipeline."""
    name: str
    value: str = ""
    status: FieldStatus = FieldStatus.OK
    ocr_score: float = 0.0
    repairs: list[RepairRecord] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)  # OCR candidates
    ai_issues: list[dict[str, Any]] = field(default_factory=list)  # AI validator issues

    @property
    def needs_retry(self) -> bool:
        return self.status in (FieldStatus.RETRY_OCR, FieldStatus.SUSPECTED_OCR_ERROR)

    @property
    def needs_review(self) -> bool:
        return self.status == FieldStatus.NEED_REVIEW

    def add_repair(self, before: str, after: str, source: str,
                   confidence: float = 1.0, reason: str = "") -> None:
        self.repairs.append(RepairRecord(
            field=self.name, before=before, after=after,
            source=source, confidence=confidence, reason=reason,
        ))
        self.value = after
        if source in ("checksum", "division", "rule"):
            self.status = FieldStatus.RULE_REPAIRED
        elif source == "ai":
            self.status = FieldStatus.AI_REPAIRED
        elif source == "ocr_fusion":
            self.status = FieldStatus.RULE_REPAIRED

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "status": self.status.value,
            "ocr_score": self.ocr_score,
            "repairs": [
                {"field": r.field, "before": r.before, "after": r.after,
                 "source": r.source, "confidence": r.confidence, "reason": r.reason}
                for r in self.repairs
            ],
            "candidates": self.candidates,
            "ai_issues": self.ai_issues,
        }


class FieldStateTracker:
    """Manages FieldState for all license fields throughout the pipeline."""

    def __init__(self) -> None:
        self._fields: dict[str, FieldState] = {}
        self._retry_count: dict[str, int] = {}
        self.max_retries: int = 2
        self.max_ai_rounds: int = 2
        self.ai_round: int = 0

    def init_field(self, name: str, value: str = "", score: float = 0.0,
                   status: FieldStatus | None = None) -> FieldState:
        fs = FieldState(name=name, value=value, ocr_score=score)
        if status is not None:
            fs.status = status
        self._fields[name] = fs
        return fs

    def get(self, name: str) -> FieldState | None:
        return self._fields.get(name)

    def all_fields(self) -> dict[str, FieldState]:
        return dict(self._fields)

    def can_retry(self, name: str) -> bool:
        return self._retry_count.get(name, 0) < self.max_retries

    def mark_retry(self, name: str) -> None:
        self._retry_count[name] = self._retry_count.get(name, 0) + 1

    def retry_count(self, name: str) -> int:
        return self._retry_count.get(name, 0)

    def missing_critical(self) -> list[str]:
        return [f for f in CRITICAL_FIELDS
                if not self._fields.get(f) or not self._fields[f].value]

    def fields_needing_retry(self) -> list[str]:
        return [name for name, fs in self._fields.items() if fs.needs_retry]

    def fields_needing_review(self) -> list[str]:
        return [name for name, fs in self._fields.items() if fs.needs_review]

    def finalize_retries(self) -> list[str]:
        """Any leftover RETRY_OCR / suspected error becomes NEED_REVIEW."""
        moved: list[str] = []
        for name, fs in self._fields.items():
            if fs.status in (FieldStatus.RETRY_OCR, FieldStatus.SUSPECTED_OCR_ERROR):
                fs.status = FieldStatus.NEED_REVIEW
                moved.append(name)
        return moved

    def mark_need_review(self, name: str, reason: str = "") -> None:
        fs = self._fields.get(name)
        if fs is None:
            fs = self.init_field(name, "")
        fs.status = FieldStatus.NEED_REVIEW
        if reason:
            fs.ai_issues.append({"reason": reason, "action": "needs_review"})

    def to_dict(self) -> dict:
        return {name: fs.to_dict() for name, fs in self._fields.items()}
