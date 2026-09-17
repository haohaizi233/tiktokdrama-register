"""Field-level crop and re-OCR — one complete candidate per engine × variant."""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .ocr import OcrLine, _enhance, _remove_red_seal
from .ocr_ensemble import (
    FieldOcrCandidate,
    OcrEnsemble,
    raw_to_ocr_lines,
)
from .field_parse import FIELD_LABELS, parse_field_lines

LOG = logging.getLogger("license_trans.ocr")

_EXPAND_RIGHT = 3.0
_EXPAND_DOWN = 8.0
_EXPAND_PAD = 0.15


class FieldCropper:
    """Crops a specific field region from the full image for re-OCR."""

    def __init__(self, image: np.ndarray, lines: list[OcrLine]) -> None:
        self._image = image
        self._lines = lines
        self._h, self._w = image.shape[:2]

    def find_field_region(self, field_name: str) -> tuple[int, int, int, int] | None:
        labels = FIELD_LABELS.get(field_name)
        if not labels:
            return None

        label_line = None
        for ln in self._lines:
            compact = ln.text.replace(" ", "").replace("：", ":").replace("（", "(").replace("）", ")").strip()
            for lab in labels:
                lab_compact = lab.replace(" ", "")
                if compact.startswith(lab_compact) or lab_compact == compact:
                    label_line = ln
                    break
            if label_line:
                break

        if label_line is None:
            for ln in self._lines:
                compact = ln.text.replace(" ", "").strip()
                for lab in labels:
                    lab_compact = lab.replace(" ", "")
                    if len(lab_compact) >= 3 and compact.startswith(lab_compact[1:]):
                        label_line = ln
                        break
                if label_line:
                    break

        if label_line is None:
            return None

        lw = label_line.x1 - label_line.x0
        lh = label_line.y1 - label_line.y0
        x0 = max(0, int(label_line.x0 - lh * _EXPAND_PAD))
        y0 = max(0, int(label_line.y0 - lh * _EXPAND_PAD))
        x1 = min(self._w, int(label_line.x1 + lw * _EXPAND_RIGHT + lh * _EXPAND_PAD))
        y1 = min(self._h, int(label_line.y0 + lh * _EXPAND_DOWN + lh * _EXPAND_PAD))
        return (x0, y0, x1, y1)

    def crop_field(self, field_name: str) -> np.ndarray | None:
        region = self.find_field_region(field_name)
        if region is None:
            return None
        x0, y0, x1, y1 = region
        crop = self._image[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        return crop

    def generate_candidates(
        self,
        field_name: str,
        ensemble: OcrEnsemble,
    ) -> list[FieldOcrCandidate]:
        crop = self.crop_field(field_name)
        if crop is None:
            return []

        candidates: list[FieldOcrCandidate] = []
        candidates.extend(self._run_ensemble(field_name, ensemble, crop, "original"))

        h, w = crop.shape[:2]
        if max(h, w) < 1200:
            upscaled = cv2.resize(crop, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
            candidates.extend(self._run_ensemble(field_name, ensemble, upscaled, "2x"))

        enhanced = _enhance(crop)
        candidates.extend(self._run_ensemble(field_name, ensemble, enhanced, "clahe"))

        if field_name in ("name", "capital", "scope", "authority"):
            cleaned = _remove_red_seal(crop)
            if not np.array_equal(cleaned, crop):
                candidates.extend(
                    self._run_ensemble(field_name, ensemble, cleaned, "seal_removed")
                )
        return [c for c in candidates if c.text]

    @staticmethod
    def _run_ensemble(
        field_name: str,
        ensemble: OcrEnsemble,
        image: np.ndarray,
        variant: str,
    ) -> list[FieldOcrCandidate]:
        results: list[FieldOcrCandidate] = []
        if ensemble.wechat_available:
            try:
                raw, _ = ensemble.run_wechat(image)
                results.append(_candidate_from_raw(field_name, "wechat", variant, raw))
            except Exception:
                LOG.exception("[OCR] WeChat field retry failed (%s)", variant)
        if ensemble.ppocr_available:
            try:
                raw, _ = ensemble.run_ppocr(image)
                results.append(_candidate_from_raw(field_name, "ppocr_v6", variant, raw))
            except Exception:
                LOG.exception("[OCR] PP-OCRv6 field retry failed (%s)", variant)
        return [c for c in results if c is not None and c.text]


def _mean_score(lines: list[OcrLine]) -> float | None:
    scores = [ln.score for ln in lines if ln.score is not None]
    if not scores:
        return None
    return float(sum(scores) / len(scores))


def _candidate_from_raw(
    field_name: str,
    engine: str,
    variant: str,
    raw: list,
) -> FieldOcrCandidate | None:
    lines = raw_to_ocr_lines(raw)
    text = parse_field_lines(field_name, lines)
    if not text:
        return None
    return FieldOcrCandidate(
        field=field_name,
        engine=engine,
        variant=variant,
        text=text,
        score=_mean_score(lines),
        lines=lines,
    )
