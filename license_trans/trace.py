"""Per-license trace id and structured event logging."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

LOG = logging.getLogger("license_trans.trace")

EVENTS = (
    "OCR_PRIMARY_START",
    "OCR_PRIMARY_OK",
    "OCR_PRIMARY_FAILED",
    "OCR_FALLBACK",
    "PARSE_OK",
    "RULE_VALIDATION",
    "AI_VALIDATION",
    "FIELD_RETRY",
    "OCR_FUSION",
    "QR_VALIDATION",
    "CANONICAL_READY",
    "LOCAL_TRANSLATION",
    "AI_TRANSLATION",
    "TRANSLATION_VALIDATION",
    "WEB_VERIFY",
    "PDF_RENDER",
)


def new_trace_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:6]
    return f"license_{stamp}_{suffix}"


class PipelineTrace:
    """Collects structured events for one license conversion."""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or new_trace_id()
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **payload: Any) -> None:
        record = {
            "trace_id": self.trace_id,
            "event": event,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if payload:
            record["payload"] = payload
        self.events.append(record)
        try:
            extra = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
        except TypeError:
            extra = str(payload)
        LOG.info("[%s] %s %s", self.trace_id, event, extra)

    def to_list(self) -> list[dict[str, Any]]:
        return list(self.events)
