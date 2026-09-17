"""OCR ensemble types and field-level candidate fusion.

A candidate is one engine × one preprocess variant over a complete field
crop, never a single OCR line.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .ocr import OcrLine
from .ocr_wechat import WeChatOCREngine, wechat_ocr_health_check
from .ocr import RapidOCREngine

LOG = logging.getLogger("license_trans.ocr")


def _norm(text: str) -> str:
    return (text or "").replace(" ", "").replace("\u3000", "").strip()


def _engine_family(engine: str) -> str:
    name = (engine or "").lower()
    if name.startswith("wechat"):
        return "wechat"
    if name.startswith("ppocr") or name.startswith("rapid"):
        return "ppocr_v6"
    return name or "unknown"


def _score_value(score: float | None) -> float:
    """None confidence must not outrank a real PP-OCR score."""
    if score is None:
        return 0.0
    return float(score)


@dataclass
class OcrCandidate:
    """Legacy per-line candidate (kept for older tests)."""
    text: str
    score: float | None
    engine: str
    box: list | None = None
    variant: str = "original"


@dataclass
class FieldOcrCandidate:
    """One engine's complete parse of one field crop / variant."""
    field: str
    engine: str
    variant: str
    text: str
    score: float | None
    lines: list[OcrLine] = field(default_factory=list)
    rule_score: float | None = None
    box: list | None = None

    @property
    def engine_family(self) -> str:
        return _engine_family(self.engine)


@dataclass
class FusionResult:
    winner: FieldOcrCandidate | None
    engine_vote: dict[str, int] = field(default_factory=dict)
    variant_vote: dict[str, int] = field(default_factory=dict)
    independent_engines: dict[str, str] = field(default_factory=dict)
    engine_conflict: bool = False
    reason: str = ""


class OcrEnsemble:
    """Optional dual-engine helper used by field retry.

    Primary OCR in the main pipeline is OcrService. This class is for
    generating independent WeChat / PP-OCR candidates on a crop.
    """

    def __init__(
        self,
        config: dict | None = None,
        *,
        wechat: Any | None = None,
        ppocr: Any | None = None,
        init_engines: bool = True,
    ) -> None:
        self._config = config or {}
        self._wechat = wechat
        self._ppocr = ppocr
        self._primary_name = "ppocr_v6"
        self._fallback_mode = False

        wechat_enabled = True
        if self._config.get("wechat_ocr", {}).get("enabled", True) is False:
            wechat_enabled = False

        if init_engines:
            if self._wechat is None and wechat_enabled:
                health = wechat_ocr_health_check(self._config, init=False)
                if health.get("available"):
                    try:
                        self._wechat = WeChatOCREngine(self._config)
                        self._primary_name = "wechat"
                        LOG.info("[OCR] Ensemble WeChat ready: %s", health.get("path"))
                    except Exception:
                        LOG.exception("Ensemble WeChat init failed")
                        self._wechat = None
            if self._ppocr is None:
                try:
                    self._ppocr = RapidOCREngine()
                except Exception:
                    LOG.exception("Ensemble PP-OCRv6 init failed")
                    self._ppocr = None

        if self._wechat is not None:
            self._primary_name = "wechat"
            self._fallback_mode = False
        else:
            self._primary_name = "ppocr_v6"
            self._fallback_mode = True

        if self._wechat is None and self._ppocr is None:
            raise RuntimeError("No OCR engines available for ensemble")

    @property
    def primary_name(self) -> str:
        return self._primary_name

    @property
    def is_fallback_mode(self) -> bool:
        return self._fallback_mode

    @property
    def wechat_available(self) -> bool:
        return self._wechat is not None

    @property
    def ppocr_available(self) -> bool:
        return self._ppocr is not None

    def health_status(self) -> dict:
        wx_ready = self._wechat is not None
        return {
            "wechat": "READY" if wx_ready else "UNAVAILABLE",
            "ppocr_v6": "READY" if self._ppocr else "UNAVAILABLE",
            "primary": self._primary_name,
            "fallback_mode": self._fallback_mode,
        }

    def run_wechat(self, image) -> tuple[list, float]:
        if self._wechat is None:
            return [], 0.0
        return self._wechat(image)

    def run_ppocr(self, image) -> tuple[list, float]:
        if self._ppocr is None:
            return [], 0.0
        return self._ppocr(image)

    def run_primary(self, image) -> tuple[list, float]:
        if self._wechat is not None:
            return self._wechat(image)
        return self._ppocr(image)

    def run_secondary(self, image) -> tuple[list, float]:
        return self.run_ppocr(image)

    def __call__(self, image) -> tuple[list, float]:
        return self.run_primary(image)


def raw_to_ocr_lines(raw: list) -> list[OcrLine]:
    lines: list[OcrLine] = []
    for item in raw or []:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        box, text = item[0], item[1]
        score = item[2] if len(item) > 2 else None
        text = (text or "").strip()
        if not text:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(OcrLine(
            text=text,
            score=None if score is None else float(score),
            x0=float(min(xs)), y0=float(min(ys)),
            x1=float(max(xs)), y1=float(max(ys)),
        ))
    return lines


class OcrCandidateFusion:
    """Fuse complete field candidates with independent engine vs variant votes."""

    def __init__(self) -> None:
        self._scope_atoms: set[str] | None = None
        self._divisions: set[str] | None = None
        self._phrases: set[str] | None = None

    def _ensure_glossaries(self) -> None:
        if self._scope_atoms is not None:
            return
        try:
            from .translate import _load
            atoms = _load("scope_atoms.json")
            self._scope_atoms = set(atoms.keys())
        except Exception:
            LOG.exception("scope_atoms load failed")
            self._scope_atoms = set()
        try:
            from .divisions import all_rows
            self._divisions = {r.get("zh", "") for r in all_rows() if r.get("zh")}
        except Exception:
            LOG.exception("divisions load failed")
            self._divisions = set()
        try:
            from .translate import _load
            phrases = _load("phrases.json")
            self._phrases = set(phrases.keys())
        except Exception:
            LOG.exception("phrases load failed")
            self._phrases = set()

    def _rule_score(self, field_name: str, text: str) -> float:
        self._ensure_glossaries()
        t = _norm(text)
        score = 0.0
        if field_name in ("credit_code", "usci"):
            from .usci import checksum_ok, repair_usci
            if checksum_ok(t):
                return 5.0
            if checksum_ok(repair_usci(t)):
                return 3.0
            return 0.0
        if self._scope_atoms and t in self._scope_atoms:
            score += 2.0
        if self._phrases:
            for phrase in self._phrases:
                if phrase and phrase in text:
                    score += 0.4
        if self._divisions and any(d and d in text for d in self._divisions if len(d) >= 2):
            score += 0.8
        if field_name == "scope" and ("一般项目" in text or "许可项目" in text):
            score += 0.5
        if field_name == "name" and ("公司" in text or "厂" in text or "中心" in text):
            score += 0.3
        return score

    def fuse_field(
        self,
        field_name: str,
        candidates: list[FieldOcrCandidate] | list[OcrCandidate],
    ) -> FieldOcrCandidate | OcrCandidate | None:
        result = self.fuse_field_ex(field_name, candidates)
        return result.winner

    def fuse_field_ex(
        self,
        field_name: str,
        candidates: list[FieldOcrCandidate] | list[OcrCandidate],
    ) -> FusionResult:
        typed: list[FieldOcrCandidate] = []
        for c in candidates or []:
            if isinstance(c, FieldOcrCandidate):
                typed.append(c)
            else:
                typed.append(FieldOcrCandidate(
                    field=field_name,
                    engine=_engine_family(c.engine),
                    variant=getattr(c, "variant", None) or _variant_of(c.engine),
                    text=c.text,
                    score=c.score,
                    box=getattr(c, "box", None),
                ))
        if not typed:
            return FusionResult(winner=None, reason="no_candidates")

        # USCI: checksum unique winner
        if field_name in ("credit_code", "usci"):
            from .usci import checksum_ok, repair_usci
            for c in typed:
                if checksum_ok(_norm(c.text)):
                    c.rule_score = 5.0
                    return FusionResult(winner=c, reason="usci_checksum")
            for c in typed:
                repaired = repair_usci(_norm(c.text))
                if checksum_ok(repaired):
                    winner = FieldOcrCandidate(
                        field=field_name, engine=c.engine, variant=c.variant,
                        text=repaired, score=c.score, lines=c.lines,
                        rule_score=3.0,
                    )
                    return FusionResult(winner=winner, reason="usci_repair")

        for c in typed:
            c.rule_score = self._rule_score(field_name, c.text)

        by_text: dict[str, list[FieldOcrCandidate]] = {}
        independent: dict[str, str] = {}
        engine_vote: dict[str, int] = {}
        variant_vote: dict[str, int] = {}
        for c in typed:
            key = _norm(c.text)
            by_text.setdefault(key, []).append(c)
            fam = c.engine_family
            if fam not in independent:
                independent[fam] = key
            elif independent[fam] != key:
                independent[fam] = independent[fam]  # keep first; conflict vs other engines
            engine_vote[key] = len({x.engine_family for x in by_text[key]})
            variant_vote[key] = len({(x.engine_family, x.variant) for x in by_text[key]})

        unique_engine_texts = set(independent.values())
        engine_conflict = len(unique_engine_texts) > 1 and len(independent) > 1

        def rank(c: FieldOcrCandidate) -> tuple:
            key = _norm(c.text)
            return (
                engine_vote.get(key, 1),
                variant_vote.get(key, 1),
                c.rule_score or 0.0,
                _score_value(c.score),
                len(c.text or ""),
            )

        winner = max(typed, key=rank)
        return FusionResult(
            winner=winner,
            engine_vote={k: engine_vote[k] for k in engine_vote},
            variant_vote={k: variant_vote[k] for k in variant_vote},
            independent_engines=independent,
            engine_conflict=engine_conflict,
            reason="ranked",
        )

    def fuse(self, candidates: list[OcrCandidate] | list[FieldOcrCandidate]):
        if not candidates:
            return None
        field = getattr(candidates[0], "field", "generic")
        return self.fuse_field(field, candidates)


def _variant_of(engine: str) -> str:
    for tag in ("original", "clahe", "enhanced", "2x", "3x", "seal_removed"):
        if tag in (engine or ""):
            return tag
    return "original"
