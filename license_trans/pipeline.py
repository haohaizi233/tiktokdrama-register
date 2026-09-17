from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

import cv2
import numpy as np

from .classify import NotFormatA, classify_format_a
from .ocr import LicenseOCR, OcrLine, _run_ocr, _USCI_RE
from .parse import parse_license
from .render import render_pdf
from .translate import translate_fields
from .field_state import FieldState, FieldStatus, FieldStateTracker, CRITICAL_FIELDS
from .canonical import CanonicalChineseRecord
from .ai_client import AIClient, is_ai_enabled
from .ai_validator import LicenseSemanticValidator, TranslationValidator
from .ocr_ensemble import OcrEnsemble, OcrCandidateFusion, FieldOcrCandidate
from .field_crop import FieldCropper
from .ocr_service import OcrService
from .rule_repair import apply_deterministic_repairs, field_passes_rules
from .translation_resolver import TranslationResolver, leftover_cjk
from .trace import PipelineTrace, new_trace_id

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
LOG = logging.getLogger("license_trans")


def load_config(path: str | Path | None = None) -> dict:
    cfg = Path(path) if path else ROOT / "config.json"
    return json.loads(cfg.read_text(encoding="utf-8"))


def _detect_qr_points(image_bgr) -> np.ndarray | None:
    """Try to detect QR code corner points. Returns 4x2 array or None."""
    detector = cv2.QRCodeDetector()
    try:
        _, points, _ = detector.detectAndDecode(image_bgr)
        if points is not None:
            return points.reshape(-1, 2)
    except Exception as exc:
        LOG.debug("QR detect failed: %s", exc, exc_info=True)
    # Try pyzbar if available (more robust)
    try:
        from pyzbar import pyzbar
        results = pyzbar.decode(image_bgr, symbols=[pyzbar.ZBarSymbol.QRCODE])
        if results:
            rect = results[0].rect
            # Build corner points from rect
            x, y, w, h = rect.left, rect.top, rect.width, rect.height
            return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32)
    except ImportError:
        LOG.debug("pyzbar not installed")
    except Exception as exc:
        LOG.debug("pyzbar QR detect failed: %s", exc, exc_info=True)
    return None


def _order_pts(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as TL, TR, BR, BL."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _crop_from_points(image_bgr, pts, h, w) -> bytes | None:
    """Crop QR code using perspective transform + background cleaning.

    Instead of a padded axis-aligned crop (which includes photo background),
    we warp the QR code to a clean square, threshold to pure black/white,
    and add a thin white border.
    """
    rect = _order_pts(pts.astype(np.float32))
    side_w = int(max(np.linalg.norm(rect[0] - rect[1]),
                     np.linalg.norm(rect[2] - rect[3])))
    side_h = int(max(np.linalg.norm(rect[0] - rect[3]),
                     np.linalg.norm(rect[1] - rect[2])))
    side = max(side_w, side_h)
    if side < 30 or side > 500:
        return None
    dst = np.array([[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]],
                    dtype=np.float32)
    M = cv2.getPerspectiveTransform(rect, dst)
    qr = cv2.warpPerspective(image_bgr, M, (side, side))
    if qr.size == 0:
        return None
    # Clean: convert to pure black/white via threshold
    gray = cv2.cvtColor(qr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Add thin white border for clean appearance
    border = 6
    result = np.full((side + 2 * border, side + 2 * border), 255, dtype=np.uint8)
    result[border:border + side, border:border + side] = binary
    ok, buf = cv2.imencode(".png", result)
    return buf.tobytes() if ok else None


def _enhance_for_qr(image_bgr) -> np.ndarray:
    """Enhance image for QR detection: grayscale + CLAHE + sharpen."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    enhanced = clahe.apply(gray)
    # Slight sharpening
    kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
    sharpened = cv2.filter2D(enhanced, -1, kernel)
    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)


def _find_qr_by_contour(image_bgr) -> bytes | None:
    """Last-resort: find QR-like square contours (finder patterns)."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    # Look for square contours with high edge density (QR modules)
    edges = cv2.Canny(gray, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = image_bgr.shape[:2]
    candidates = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 1000 or area > w * h * 0.3:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) != 4:
            continue
        # Check aspect ratio (QR is roughly square)
        x, y, cw, ch = cv2.boundingRect(approx)
        aspect = float(cw) / max(ch, 1)
        if 0.5 < aspect < 2.0 and 30 < cw < 500 and 30 < ch < 500:
            # Check edge density inside (QR has lots of edges)
            mask = np.zeros(gray.shape, dtype=np.uint8)
            cv2.drawContours(mask, [approx], -1, 255, -1)
            inside = cv2.bitwise_and(edges, mask)
            density = cv2.countNonZero(inside) / max(area, 1)
            if density > 0.05:  # QR codes have high edge density
                candidates.append((density, approx))
    if not candidates:
        return None
    # Pick the candidate with highest edge density
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1].reshape(4, 2)
    return _crop_from_points(image_bgr, best, h, w)


def _crop_qr(image_bgr, lines: list[OcrLine]) -> bytes | None:
    """Multi-strategy QR code detection and cropping.

    Tries: original → grayscale → enhanced → upscaled → rotated → contour-based.
    """
    h, w = image_bgr.shape[:2]

    # Strategy 1: Direct detection on original
    pts = _detect_qr_points(image_bgr)
    if pts is not None:
        crop = _crop_from_points(image_bgr, pts, h, w)
        if crop:
            return crop

    # Strategy 2: Grayscale
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    pts = _detect_qr_points(gray_bgr)
    if pts is not None:
        crop = _crop_from_points(image_bgr, pts, h, w)
        if crop:
            return crop

    # Strategy 3: Enhanced (CLAHE + sharpen)
    enhanced = _enhance_for_qr(image_bgr)
    pts = _detect_qr_points(enhanced)
    if pts is not None:
        crop = _crop_from_points(image_bgr, pts, h, w)
        if crop:
            return crop

    # Strategy 4: Upscaled (for small QR codes)
    for scale in (2, 3):
        upscaled = cv2.resize(image_bgr, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_CUBIC)
        pts = _detect_qr_points(upscaled)
        if pts is not None:
            pts_scaled = pts / scale
            crop = _crop_from_points(image_bgr, pts_scaled, h, w)
            if crop:
                return crop

    # Strategy 5: Try all 4 rotations (detector may fail on rotated images)
    for k in (1, 2, 3):
        rotated = np.ascontiguousarray(np.rot90(image_bgr, k))
        pts = _detect_qr_points(rotated)
        if pts is not None:
            # Rotate points back to original frame
            rh, rw = rotated.shape[:2]
            if k == 1:  # 90° CCW
                pts_back = np.array([[rh - p[1], p[0]] for p in pts])
            elif k == 2:  # 180°
                pts_back = np.array([[w - p[0], h - p[1]] for p in pts])
            else:  # 270° CCW
                pts_back = np.array([[p[1], rw - p[0]] for p in pts])
            crop = _crop_from_points(image_bgr, pts_back, h, w)
            if crop:
                return crop

    # Strategy 6: Contour-based detection (last resort)
    result = _find_qr_by_contour(image_bgr)
    if result:
        return result

    # Strategy 7: Default QR fallback
    default_qr = ASSETS / "default_qr.png"
    if default_qr.exists():
        return default_qr.read_bytes()

    return None


def _decode_qr(image_bgr) -> str:
    """Decode QR code content using multi-strategy detection."""
    # Try on original
    detector = cv2.QRCodeDetector()
    try:
        data, _, _ = detector.detectAndDecode(image_bgr)
        if data:
            return data.strip()
    except Exception as exc:
        LOG.debug("QR decode failed: %s", exc, exc_info=True)
    # Try on enhanced
    enhanced = _enhance_for_qr(image_bgr)
    try:
        data, _, _ = detector.detectAndDecode(enhanced)
        if data:
            return data.strip()
    except Exception as exc:
        LOG.debug("QR enhanced decode failed: %s", exc, exc_info=True)
    # Try on upscaled
    for scale in (2, 3):
        upscaled = cv2.resize(image_bgr, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_CUBIC)
        try:
            data, _, _ = detector.detectAndDecode(upscaled)
            if data:
                return data.strip()
        except Exception as exc:
            LOG.debug("QR upscaled decode failed: %s", exc, exc_info=True)
    # Try pyzbar
    try:
        from pyzbar import pyzbar
        results = pyzbar.decode(image_bgr, symbols=[pyzbar.ZBarSymbol.QRCODE])
        if results:
            return (results[0].data.decode("utf-8", errors="replace")).strip()
    except ImportError:
        LOG.debug("pyzbar not installed")
    except Exception as exc:
        LOG.debug("pyzbar decode failed: %s", exc, exc_info=True)
    return ""


def _extract_usci_from_qr(qr_data: str) -> str:
    """Extract USCI from a gsxt.gov.cn QR URL or raw payload."""
    import re
    if not qr_data:
        return ""
    m = re.search(r"([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})", qr_data)
    return m.group(1) if m else ""


def _recover_issue_date(image, lines, engine) -> str:
    import re

    year_ln = next((ln for ln in lines if re.fullmatch(r"20\d{2}年", ln.text.replace(" ", ""))), None)
    day_ln = next((ln for ln in lines if re.match(r"月\s*\d{1,2}", ln.text.replace(" ", ""))), None)
    if year_ln is None or day_ln is None:
        blob = "".join(ln.text for ln in lines)
        from .parse import DATE_RE
        match = DATE_RE.search(blob)
        return match.group(0) if match else ""
    year = re.search(r"20\d{2}", year_ln.text).group(0)
    day = re.search(r"(\d{1,2})", day_ln.text).group(1)
    month = ""
    h, w = image.shape[:2]
    x0 = int(max(0, year_ln.x1 - 6))
    x1 = int(min(w, day_ln.x0 + 8))
    y0 = int(max(0, min(year_ln.y0, day_ln.y0) - 8))
    y1 = int(min(h, max(year_ln.y1, day_ln.y1) + 8))
    if x1 > x0 + 4:
        gap = image[y0:y1, x0:x1]
        if gap.size:
            gap = cv2.resize(gap, None, fx=5.0, fy=5.0, interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(gap, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            gap_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
            for item in _run_ocr(engine._ocr, gap_bgr) + _run_ocr(engine._ocr, gap):
                found = re.search(r"\b(1[0-2]|0?[1-9])\b", item.text)
                if found:
                    month = found.group(1)
                    break
    if not month:
        for ln in lines:
            if ln is year_ln or ln is day_ln:
                continue
            if abs(ln.cy - year_ln.cy) < 24 and year_ln.x1 - 8 <= ln.cx <= day_ln.x0 + 8:
                found = re.search(r"(1[0-2]|0?[1-9])", ln.text)
                if found:
                    month = found.group(1)
                    break
    if not month:
        return ""
    return f"{int(year):04d}年{int(month):02d}月{int(day):02d}日"


def detect_license(lines: list[OcrLine]) -> bool:
    """Check if OCR lines contain '营业执照' (business license title)."""
    blob = "".join(line.text for line in lines)
    compact = blob.replace(" ", "").replace("\n", "").replace("\u3000", "")
    return "营业执照" in compact


def _normalize_issue_date(text: str) -> str:
    import re

    text = text.strip()
    match = re.fullmatch(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}年{int(match.group(2)):02d}月{int(match.group(3)):02d}日"
    match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if match:
        return f"{int(match.group(1)):04d}年{int(match.group(2)):02d}月{int(match.group(3)):02d}日"
    return text


CRITICAL_FIELDS = ("name", "credit_code", "scope", "legal_person", "capital")


def _scope_broken(scope: str) -> bool:
    """Detect a scope assembled with a missing middle line.

    License scope text never nests parentheses; a second open paren while
    one is still open (…食用农产品（不（依法须…) means an OCR row went
    missing between two wrapped rows and the join glued the fragments.
    """
    if not scope:
        return False
    depth = 0
    for ch in scope:
        if ch in "（(":
            depth += 1
            if depth > 1:
                return True
        elif ch in "）)":
            depth -= 1
    return depth != 0


_ANCHOR_PAT = re.compile(
    r"营业执照|统一社会|信用代码|法定代表人|经营范围|注册资本|注册资金|"
    r"成立日期|营业期限|经营期限|登记机关|发证日期|核准日期|住所|经营场所|"
    r"gsxt|监督管理总局|信用信息公示系统"
)
_DATE_PAT = re.compile(r"20\d{2}\s*年")


def _crop_license_view(image: np.ndarray, lines: list[OcrLine]) -> np.ndarray:
    """Crop the embedded original-image page down to the license paper.

    Anchors on OCR lines that certainly sit on the license (field labels,
    USCI, dates, title, footer), so background junk — the person holding
    the paper, room background, photo frames — never inflates the crop.
    Falls back to the full image when anchors are too few or clustered.
    """
    anchors = [
        ln for ln in lines
        if _ANCHOR_PAT.search(ln.text) or _USCI_RE.search(ln.text)
        or _DATE_PAT.search(ln.text)
    ]
    h, w = image.shape[:2]
    if len(anchors) < 5:
        return image
    x0 = min(a.x0 for a in anchors)
    x1 = max(a.x1 for a in anchors)
    y0 = min(a.y0 for a in anchors)
    y1 = max(a.y1 for a in anchors)
    if (x1 - x0) < w * 0.15 or (y1 - y0) < h * 0.15:
        return image
    m = 0.08 * min(x1 - x0, y1 - y0)
    cx0, cy0 = max(0, int(x0 - m)), max(0, int(y0 - m))
    cx1, cy1 = min(w, int(x1 + m)), min(h, int(y1 + m))
    if (cx1 - cx0) >= w * 0.995 and (cy1 - cy0) >= h * 0.995:
        return image
    return image[cy0:cy1, cx0:cx1].copy()


def _display_view(image: np.ndarray, lines: list[OcrLine],
                  meta: dict | None) -> np.ndarray:
    """Build the original-image page: cropped to the paper and upright.

    OCR may rotate a portrait vertical-text license (竖版执照) 90° to read
    its columns as rows; for display the document is turned back to its
    natural orientation. Landscape licenses pass through unchanged.
    """
    view = _crop_license_view(image, lines)
    if meta and meta.get("vertical_native") and meta.get("rotation") in (1, 3):
        view = np.ascontiguousarray(np.rot90(view, 4 - meta["rotation"]))
    return view


def _missing_critical(fields) -> list[str]:
    """Return list of critical field names that are empty.

    Accepts both LicenseFields objects (from parse_license) and plain dicts
    (from convert_image's result["fields"], which is fields.to_dict()).
    """
    if isinstance(fields, dict):
        return [f for f in CRITICAL_FIELDS if not str(fields.get(f, "") or "").strip()]
    return [f for f in CRITICAL_FIELDS if not getattr(fields, f, "")]


def _build_tracker(fields) -> FieldStateTracker:
    """Build a FieldStateTracker from parsed LicenseFields."""
    tracker = FieldStateTracker()
    for attr in ("credit_code", "name", "type", "legal_person", "scope",
                 "capital", "established", "address", "term", "authority",
                 "issue_date"):
        val = getattr(fields, attr, "") or ""
        score = 0.0
        # Use warnings to set initial status
        status = FieldStatus.OK
        if not val and attr in fields.warnings:
            status = FieldStatus.SUSPECTED_OCR_ERROR
        elif f"{attr}_low_confidence" in fields.warnings:
            status = FieldStatus.LOW_CONFIDENCE
        tracker.init_field(attr, val, score)
        tracker.get(attr).status = status
    return tracker


MAX_FIELD_OCR_RETRY = 2
MAX_AI_VALIDATION_ROUND = 2


def _run_ai_validation_and_retry(
    fields,
    lines: list[OcrLine],
    oriented: np.ndarray,
    config: dict,
    engine,
    trace: PipelineTrace | None = None,
) -> FieldStateTracker:
    """Rule repair → AI flag → field OCR retry → second validation.

    AI never writes Chinese facts. Retry failures become NEED_REVIEW.
    """
    tracker = _build_tracker(fields)
    apply_deterministic_repairs(fields, tracker)
    if trace:
        trace.emit("RULE_VALIDATION", repairs=len(tracker.to_dict()))

    ai_client = AIClient(config)
    validator = LicenseSemanticValidator(ai_client)
    parsed_dict = fields.to_dict()
    raw_texts = [ln.text for ln in lines]

    if not ai_client.is_available():
        tracker.finalize_retries()
        return tracker

    for round_i in range(MAX_AI_VALIDATION_ROUND):
        tracker.ai_round = round_i + 1
        LOG.info("[AI] 中文语义校验 round %d…", round_i + 1)
        result = validator.validate(parsed_dict, raw_texts, list(fields.warnings))
        if trace:
            trace.emit(
                "AI_VALIDATION",
                round=round_i + 1,
                overall=result.get("overall_status"),
                issues=len(result.get("issues") or []),
            )
        retry_fields = validator.apply_to_tracker(result, tracker)
        retry_fields = [f for f in retry_fields if tracker.can_retry(f)]
        if not retry_fields:
            break
        LOG.info("[AI] 需要重新 OCR 的字段: %s", retry_fields)
        resolved = _field_level_retry(
            retry_fields, fields, lines, oriented, engine, tracker, trace)
        for name in retry_fields:
            if name not in resolved:
                tracker.mark_need_review(name, "retry_failed")
            elif tracker.get(name) and tracker.get(name).status == FieldStatus.RETRY_OCR:
                tracker.get(name).status = FieldStatus.OK
        parsed_dict = fields.to_dict()
        if not resolved:
            break

    tracker.finalize_retries()
    return tracker


def _retry_one_field(
    field_name: str,
    fields,
    cropper: FieldCropper,
    ensemble: OcrEnsemble,
    fusion: OcrCandidateFusion,
    tracker: FieldStateTracker,
    trace: PipelineTrace | None = None,
) -> bool:
    """Return True if a rule-valid candidate was adopted."""
    fs = tracker.get(field_name)
    if fs is None:
        return False
    if not tracker.can_retry(field_name):
        LOG.info("[OCR] %s 已达最大重试次数", field_name)
        return False
    tracker.mark_retry(field_name)
    LOG.info("[OCR] 字段级重 OCR: %s", field_name)
    if trace:
        trace.emit("FIELD_RETRY", field=field_name, attempt=tracker.retry_count(field_name))
    try:
        candidates = cropper.generate_candidates(field_name, ensemble)
    except Exception:
        LOG.exception("[OCR] 字段裁剪失败 %s", field_name)
        return False
    if not candidates:
        LOG.info("[OCR] %s 无候选结果", field_name)
        return False
    fused = fusion.fuse_field_ex(field_name, candidates)
    if trace:
        trace.emit(
            "OCR_FUSION",
            field=field_name,
            conflict=fused.engine_conflict,
            independent=fused.independent_engines,
        )
    best = fused.winner
    if best is None or not str(best.text).strip():
        return False
    fs.candidates = [
        {"text": c.text, "score": c.score, "engine": c.engine,
         "variant": getattr(c, "variant", "")}
        for c in candidates
    ]
    new_val = best.text.strip()
    if not field_passes_rules(field_name, new_val):
        LOG.info("[OCR] %s 候选未通过规则: %s", field_name, new_val[:40])
        return False
    old_val = fs.value
    if new_val != old_val:
        fs.add_repair(
            old_val, new_val, "ocr_fusion",
            confidence=best.score if best.score is not None else 0.0,
            reason=f"Field-level re-OCR via {best.engine}/{best.variant}",
        )
        setattr(fields, field_name, new_val)
        LOG.info("[OCR] %s 修复: '%s' → '%s' (via %s)",
                 field_name, old_val[:30], new_val[:30], best.engine)
    fs.status = FieldStatus.OK
    return True


def _field_level_retry(
    retry_fields: list[str],
    fields,
    lines: list[OcrLine],
    oriented: np.ndarray,
    engine,
    tracker: FieldStateTracker,
    trace: PipelineTrace | None = None,
) -> list[str]:
    """Re-OCR fields. Returns names that produced a rule-valid candidate."""
    config = load_config()
    try:
        wechat = getattr(engine, "_wechat", None)
        ppocr = getattr(engine, "_ppocr", None)
        ensemble = OcrEnsemble(
            config, wechat=wechat, ppocr=ppocr,
            init_engines=wechat is None and ppocr is None,
        )
    except Exception:
        LOG.exception("[OCR] Ensemble init failed for field retry")
        return []

    cropper = FieldCropper(oriented, lines)
    fusion = OcrCandidateFusion()
    resolved: list[str] = []
    for field_name in retry_fields:
        if _retry_one_field(
            field_name, fields, cropper, ensemble, fusion, tracker, trace,
        ):
            resolved.append(field_name)
    return resolved


def _apply_qr_usci(fields, qr_usci: str, qr_data: str, trace: PipelineTrace | None = None) -> None:
    from .usci import checksum_ok
    if qr_usci and fields.credit_code and qr_usci != fields.credit_code:
        if checksum_ok(qr_usci) and not checksum_ok(fields.credit_code):
            fields.credit_code = qr_usci
            if "credit_code" in fields.warnings:
                fields.warnings.remove("credit_code")
            LOG.info("QR USCI 覆盖无效 OCR 代码: %s", qr_usci)
        else:
            fields.warnings.append("qr_mismatch")
            LOG.warning("QR USCI %s 与信用代码 %s 不一致", qr_usci, fields.credit_code)
    elif qr_data and not qr_usci and fields.credit_code and fields.credit_code not in qr_data:
        fields.warnings.append("qr_mismatch")
        LOG.warning("QR 内容与信用代码不一致: %s", qr_data[:80])
    elif qr_usci and not fields.credit_code:
        fields.credit_code = qr_usci
        if "credit_code" in fields.warnings:
            fields.warnings.remove("credit_code")
        LOG.info("从 QR 恢复信用代码: %s", qr_usci)
    if trace:
        trace.emit(
            "QR_VALIDATION",
            qr_usci=qr_usci,
            credit_code=fields.credit_code,
            mismatch="qr_mismatch" in fields.warnings,
        )


def _run_translation_pipeline(
    canonical: CanonicalChineseRecord,
    config: dict,
    trace: PipelineTrace | None = None,
) -> dict:
    """Translate from Canonical Chinese only. Validator cannot overwrite English."""
    chinese = canonical.to_translation_input()
    resolver = TranslationResolver(config, trace=trace)
    english = resolver.translate(chinese)

    ai_client = AIClient(config)
    flags: dict[str, str] = {}
    if ai_client.is_available():
        LOG.info("[AI] 翻译校验…")
        validator = TranslationValidator(ai_client)
        result = validator.validate(chinese, english)
        flags = validator.collect_flags(result)
        if trace:
            trace.emit(
                "TRANSLATION_VALIDATION",
                issues=len(result.get("issues") or []),
                flags=flags,
            )
        if result.get("issues"):
            LOG.info("[AI] 翻译校验完成, 发现 %d 个问题（不直接改译文）",
                     len(result["issues"]))
        else:
            LOG.info("[AI] 翻译校验通过")
    for field in ("address", "authority", "name"):
        if leftover_cjk(english.get(field) or ""):
            flags.setdefault(field, "web_verify")
    if flags:
        english = resolver.apply_web_flags(english, chinese, flags)
    return english


def convert_image(
    image_path: str | Path,
    output_path: str | Path,
    *,
    config_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    ocr: LicenseOCR | OcrService | None = None,
    translation_date: date | None = None,
    issue_date: str | None = None,
    fields_override=None,
    trace: PipelineTrace | None = None,
) -> dict:
    image_path = Path(image_path)
    output_path = Path(output_path)
    config = load_config(config_path)
    trace = trace or PipelineTrace()
    LOG.info("[%s] 读取 %s", trace.trace_id, image_path.name)
    meta: dict = {}
    if fields_override is not None:
        from .ocr import _read_image
        from .parse import LicenseFields
        engine = ocr
        fields = fields_override if isinstance(fields_override, LicenseFields) else LicenseFields(**fields_override)
        LOG.info("使用手动修正的字段, 跳过 OCR")
        oriented = _read_image(image_path)
        lines = []
        trace.emit("OCR_PRIMARY_START", skipped=True, reason="fields_override")
    else:
        engine = ocr or OcrService(config)
        trace.emit("OCR_PRIMARY_START", engine=getattr(engine, "engine_name", ""))
        LOG.info("OCR 四向纠偏…")
        oriented, lines = engine.recognize(image_path, meta=meta)
        LOG.info("识别 %d 行文字", len(lines))
        fields = parse_license(lines)
        blob = "".join(line.text for line in lines)

        # Retry with higher resolution if critical fields are missing or the
        # scope shows a broken multi-row join (missing middle line).
        missing = _missing_critical(fields)
        broken = _scope_broken(fields.scope)
        if missing or broken:
            for retry_side in (2400, 3200):
                reason = f"字段缺失 {missing}" if missing else "scope 疑似断行"
                LOG.info("%s, 重试 OCR (max_side=%d)…", reason, retry_side)
                meta_r: dict = {}
                oriented_r, lines_r = engine.recognize(image_path, max_side=retry_side, meta=meta_r)
                fields_r = parse_license(lines_r)
                blob_r = "".join(line.text for line in lines_r)
                missing_r = _missing_critical(fields_r)
                broken_r = _scope_broken(fields_r.scope)
                if (len(missing_r), int(broken_r)) < (len(missing), int(broken)):
                    oriented, lines, fields, blob = oriented_r, lines_r, fields_r, blob_r
                    meta = meta_r
                    missing, broken = missing_r, broken_r
                    LOG.info("重试改善: 仍缺 %s%s", missing or "无",
                             "，scope 断行" if broken else "")
                if not missing and not broken:
                    break

        classify_format_a(fields, blob)
        issue = _recover_issue_date(oriented, lines, engine)
        if issue_date:
            fields.issue_date = _normalize_issue_date(issue_date)
            if "issue_date" in fields.warnings:
                fields.warnings.remove("issue_date")
            LOG.info("发证日期（指定）%s", fields.issue_date)
        elif issue:
            fields.issue_date = issue
            if "issue_date" in fields.warnings:
                fields.warnings.remove("issue_date")
            LOG.info("发证日期（识别）%s", fields.issue_date)
        elif not fields.issue_date:
            from .parse import refine_issue_date
            refine_issue_date(fields, blob)
            if fields.issue_date:
                LOG.info("发证日期（碎片拼接）%s", fields.issue_date)
            else:
                LOG.warning("发证日期未识别，可在面板里指定")

        if hasattr(engine, "last_meta") and engine.last_meta:
            meta.update(engine.last_meta)
        ocr_meta = {
            "ocr_primary": meta.get("ocr_primary") or meta.get("primary") or getattr(engine, "primary_name", None) or getattr(engine, "engine_name", ""),
            "ocr_actual": meta.get("ocr_actual") or getattr(engine, "engine_name", ""),
            "fallback_used": bool(meta.get("fallback_used")),
            "fallback": meta.get("fallback"),
            "reason": meta.get("reason"),
        }
        if ocr_meta["fallback_used"]:
            trace.emit("OCR_FALLBACK", **{k: v for k, v in ocr_meta.items() if v is not None})
        elif str(ocr_meta["ocr_actual"]).startswith("wechat") or ocr_meta["ocr_primary"] == "wechat":
            trace.emit("OCR_PRIMARY_OK", **ocr_meta)
        else:
            trace.emit("OCR_PRIMARY_FAILED", **ocr_meta)
        trace.emit("PARSE_OK", missing=_missing_critical(fields))

    if hasattr(engine, "last_meta") and engine.last_meta:
        meta.update(engine.last_meta)

    # --- QR before Canonical ---
    qr_src = meta.get("full_view") if isinstance(meta, dict) else None
    if qr_src is None:
        qr_src = oriented
    qr_bytes = _crop_qr(qr_src, lines)
    qr_data = _decode_qr(qr_src)
    qr_usci = _extract_usci_from_qr(qr_data)
    _apply_qr_usci(fields, qr_usci, qr_data, trace)

    # --- Rule + AI validation + field retry (mutates fields, then freeze) ---
    if fields_override is not None:
        tracker = FieldStateTracker()
        for fname in CRITICAL_FIELDS:
            tracker.init_field(fname, getattr(fields, fname, "") or "")
        tracker.init_field("address", fields.address or "")
        LOG.info("手动字段跳过 AI/OCR retry")
    else:
        tracker = _run_ai_validation_and_retry(
            fields, lines, oriented, config, engine, trace)

    canonical = CanonicalChineseRecord.from_fields(fields, tracker)
    canonical.trace_id = trace.trace_id
    canonical.ocr_primary = str(meta.get("ocr_primary") or meta.get("primary") or "")
    canonical.ocr_actual = str(meta.get("ocr_actual") or "")
    canonical.fallback_used = bool(meta.get("fallback_used"))
    canonical.freeze()
    if canonical.needs_review:
        LOG.warning("以下字段需人工复核: %s", ", ".join(canonical.needs_review))
    trace.emit("CANONICAL_READY", needs_review=canonical.needs_review)

    english = _run_translation_pipeline(canonical, config, trace)
    LOG.info("译文  %s", english.get("name") or "（无名称）")

    display = _display_view(oriented, lines, meta)
    LOG.info("渲染英文模板 PDF")
    trace.emit("PDF_RENDER", output=str(output_path))
    render_pdf(
        english,
        config,
        output_path,
        original_image=image_path,
        oriented_bgr=display,
        qr_bytes=qr_bytes,
        translation_date=translation_date,
    )
    canonical_dict = canonical.to_translation_input()
    result = {
        "input": str(image_path),
        "output": str(output_path),
        "trace_id": trace.trace_id,
        "fields": canonical_dict,
        "english": english,
        "ocr": [line.to_dict() for line in lines],
        "qr_data": qr_data,
        "canonical": canonical.to_dict(),
        "ocr_primary": canonical.ocr_primary,
        "ocr_actual": canonical.ocr_actual,
        "fallback_used": canonical.fallback_used,
        "needs_review": list(canonical.needs_review),
        "events": trace.to_list(),
        "status": {
            "ocr_primary": canonical.ocr_primary,
            "fallback_used": canonical.fallback_used,
            "ai_validated": canonical.ai_validated,
            "field_retries": sum(tracker.retry_count(n) for n in tracker.all_fields()),
            "web_verified": sum(
                1 for e in (english.get("provenance") or {}).values()
                if isinstance(e, dict) and e.get("provider") == "web_verified"
            ),
            "need_review": len(canonical.needs_review),
        },
    }
    if work_dir:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        sidecar = work_dir / (output_path.stem + ".json")
        sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        preview = work_dir / (output_path.stem + "-oriented.jpg")
        cv2.imencode(".jpg", display)[1].tofile(str(preview))
    LOG.info("完成  %s", output_path)
    return result


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _pick_best_license(candidates: list[tuple[Path, list[OcrLine]]]) -> tuple[Path, list[OcrLine]]:
    """Pick the clearest license image from detected candidates.

    Heuristic: more OCR lines = clearer photo; tie-break by larger file size
    (desktop photos are typically higher resolution than handheld).
    """
    return max(candidates, key=lambda item: (len(item[1]), item[0].stat().st_size))


def process_license_library(
    source_dir: str | Path,
    dest_dir: str | Path,
    *,
    max_workers: int = 5,
    engine: LicenseOCR | OcrService | None = None,
    log=None,
    progress=None,
    stop_event=None,
) -> dict:
    """Batch-process the license library:

    1. Scan company subfolders in source_dir (中国执照库)
    2. For each subfolder: quick-detect which images are licenses (parallel, max_workers)
    3. Pick the clearest license (most OCR lines, then largest file)
    4. Full OCR + translate → output PDF in the company folder
    5. Move the company folder to dest_dir (资料)

    Returns {"processed": N, "failed": N, "moved": N, "skipped": N}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    source = Path(source_dir)
    dest = Path(dest_dir)

    if not source.exists():
        if log:
            log(f"执照库不存在: {source}", "err")
        return {"processed": 0, "failed": 0, "moved": 0, "skipped": 0}

    eng = engine or OcrService()
    company_folders = sorted([d for d in source.iterdir() if d.is_dir()])
    total = len(company_folders)
    processed = failed = moved = skipped = 0

    # --- Pass 1: scan all folders, quick-detect, pick best license ---
    convert_tasks: list[tuple[Path, Path, Path]] = []  # (folder, best_img, dest_pdf)
    for i, folder in enumerate(company_folders, start=1):
        if stop_event and stop_event.is_set():
            if log:
                log("已停止扫描执照库", "warn")
            break

        if progress:
            progress(i, total, folder.name)

        images = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
        if not images:
            skipped += 1
            continue

        # Quick-detect licenses in parallel
        licenses: list[tuple[Path, list[OcrLine]]] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(images))) as pool:
            futures = {pool.submit(eng.quick_detect, img): img for img in images}
            for fut in as_completed(futures):
                if stop_event and stop_event.is_set():
                    break
                img = futures[fut]
                try:
                    is_license, lines = fut.result()
                    if is_license:
                        licenses.append((img, lines))
                except Exception as exc:
                    if log:
                        log(f"  检测失败 {img.name}: {exc}", "err")

        if not licenses:
            if log:
                log(f"[{i}/{total}] {folder.name}: 未找到营业执照, 跳过", "warn")
            skipped += 1
            continue

        best_img, _best_quick_lines = _pick_best_license(licenses)
        if log:
            log(f"[{i}/{total}] {folder.name}: 找到 {len(licenses)} 张执照, "
                f"选用 {best_img.name} ({best_img.stat().st_size // 1024}KB)", "ok")
        dest_pdf = folder / (best_img.stem + "英文版.pdf")
        convert_tasks.append((folder, best_img, dest_pdf))

    # --- Pass 2: convert + move in parallel (3 workers) ---
    convert_workers = min(3, len(convert_tasks))

    def _convert_and_move(task):
        folder, best_img, dest_pdf = task
        try:
            info = convert_image(best_img, dest_pdf, ocr=eng, work_dir=ROOT / "license_work")
        except NotFormatA as exc:
            return ("skip", folder, dest_pdf, str(exc))
        except Exception as exc:
            return ("fail", folder, dest_pdf, str(exc))
        # Check if critical fields are missing — don't move if incomplete
        missing = _missing_critical(info["fields"])
        if missing:
            return ("incomplete", folder, dest_pdf, ", ".join(missing))
        # Move folder to dest
        try:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / folder.name
            if target.exists():
                import shutil
                shutil.rmtree(target)
            folder.rename(target)
            return ("ok", folder, target, "")
        except Exception as exc:
            return ("moved_fail", folder, dest_pdf, str(exc))

    if convert_tasks:
        convert_total = len(convert_tasks)
        convert_done = 0
        if log:
            log(f"扫描完成, 开始并行翻译 {convert_total} 家 ({convert_workers}线程)…", "ok")
        with ThreadPoolExecutor(max_workers=convert_workers) as pool:
            futures = {pool.submit(_convert_and_move, t): t for t in convert_tasks}
            for fut in as_completed(futures):
                if stop_event and stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                convert_done += 1
                if progress:
                    progress(convert_done, convert_total, "翻译中")
                try:
                    result = fut.result()
                except Exception as exc:
                    failed += 1
                    if log:
                        log(f"  异常: {exc}", "err")
                    continue
                kind, folder, dest_pdf, msg = result
                if kind == "ok":
                    processed += 1
                    moved += 1
                    if log:
                        log(f"  ✓ {folder.name} → {msg}", "ok")
                elif kind == "skip":
                    skipped += 1
                    if log:
                        log(f"  ⊘ {folder.name}: {msg}, 不移动", "warn")
                elif kind == "fail":
                    failed += 1
                    if log:
                        log(f"  ✗ {folder.name}: {msg}", "err")
                elif kind == "moved_fail":
                    processed += 1
                    if log:
                        log(f"  ⚠ {folder.name}: 翻译成功但移动失败: {msg}", "warn")
                elif kind == "incomplete":
                    failed += 1
                    if log:
                        log(f"  ⚠ {folder.name}: 字段缺失 [{msg}], 不移动, 留待重试", "warn")

    return {"processed": processed, "failed": failed, "moved": moved, "skipped": skipped}
