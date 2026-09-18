"""Field-level crop and re-OCR — one complete candidate per engine × variant."""
from __future__ import annotations

import logging
import re

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
                    if len(lab_compact) >= 4 and lab_compact in compact:
                        label_line = ln
                        break
                    if len(lab_compact) >= 3 and compact.startswith(lab_compact[1:]):
                        label_line = ln
                        break
                if label_line:
                    break

        if label_line is None and field_name == "address":
            for ln in self._lines:
                compact = ln.text.replace(" ", "").replace("：", ":").strip()
                if compact in ("所", "住所") and ln.x0 > self._w * 0.45:
                    label_line = ln
                    break

        if label_line is None and field_name == "scope":
            return infer_scope_region(self._image, self._lines)

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


def infer_scope_region(image: np.ndarray, lines: list[OcrLine]) -> tuple[int, int, int, int] | None:
    """Left-column 经营范围 band on a 横版 license.

    WeChat often skips this dense paragraph and only returns the legal
    footer '…制的项目)'. Anchor on 法定代表人 / that footer / the right
    column (住所/注册资本) instead of the missing 经营范围 label.
    """
    h, w = image.shape[:2]
    if h < 40 or w < 40:
        return None
    legal = addr = cap = auth = tail = scope_lab = None
    for ln in lines:
        t = re.sub(r"\s+", "", ln.text or "")
        if legal is None and "法定代表人" in t:
            legal = ln
        if scope_lab is None and (t == "经营范围" or (t.startswith("经营范围") and len(t) <= 8)):
            scope_lab = ln
        if addr is None and (
            t.startswith("住所")
            or ((t.startswith("所") or t.startswith("住所")) and ln.x0 > w * 0.38)
        ):
            addr = ln
        if cap is None and "注册资本" in t:
            cap = ln
        if auth is None and "登记机关" in t:
            auth = ln
        if tail is None and (
            "制的项目" in t or "限制的项目" in t
            or t.endswith("的项目)") or t.endswith("的项目）")
        ):
            tail = ln
    right_col = w * 0.58
    for ln in (addr, cap):
        if ln is not None and ln.x0 > w * 0.38:
            right_col = min(right_col, ln.x0 - 6)
    if tail is not None:
        tw = max(24.0, tail.x1 - tail.x0)
        th = max(14.0, tail.y1 - tail.y0)
        x0 = int(max(0, min(tail.x0 - tw * 5, w * 0.08)))
        x1 = int(min(w, max(tail.x1 + tw, right_col)))
        y0 = int(legal.y1 - 8) if legal else int(max(0, tail.y0 - th * 10))
        y1 = int(min(h, tail.y1 + th * 1.8))
    elif legal is not None or scope_lab is not None:
        anchor = scope_lab or legal
        lh = max(16.0, anchor.y1 - anchor.y0)
        x0 = int(max(0, (legal.x0 if legal else anchor.x0) - 30))
        x1 = int(min(w, right_col))
        y0 = int(scope_lab.y0 - 4) if scope_lab else int(legal.y1)
        y1 = int(auth.y0 - 8) if auth else int(min(h, y0 + lh * 14))
    else:
        return None
    if x1 - x0 < 40 or y1 - y0 < 18:
        return None
    return (x0, y0, x1, y1)


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
