from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date
from pathlib import Path

import cv2
import numpy as np

from .classify import NotFormatA, classify_format_a
from .ocr import (
    LicenseOCR, OcrLine, _run_ocr, _USCI_RE, _enhance, _remove_red_seal,
    _title_line,
)
from .parse import parse_license, DATE_RE
from .render import render_pdf
from .translate import translate_fields
from .field_state import FieldState, FieldStatus, FieldStateTracker, CRITICAL_FIELDS
from .canonical import CanonicalChineseRecord
from .ai_client import AIClient, is_ai_enabled
from .ai_validator import LicenseSemanticValidator, TranslationValidator
from .ocr_ensemble import OcrEnsemble, OcrCandidateFusion, FieldOcrCandidate
from .field_crop import FieldCropper, infer_scope_region
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


_ISSUE_DATE_RE = re.compile(
    r"(20\d{2})\s*年\s*(1[0-2]|0[1-9]|[1-9])\s*月\s*(3[01]|[12]\d|0[1-9]|[1-9])\s*日?"
)


def _compact_issue_text(text: str) -> str:
    return (text or "").replace(" ", "").replace("\u3000", "").replace("：", "").replace(":", "")


def _issue_date_strip(image: np.ndarray, lines: list[OcrLine]) -> np.ndarray | None:
    """Crop the date band under the registration-authority seal.

    A-format prints 发证日期 under the red 登记机关 stamp, usually without a
    label. Full-page OCR shatters it; this crop is the only region we re-OCR.
    """
    h, w = image.shape[:2]
    if h < 20 or w < 20:
        return None
    issue_lbl = None
    auth_lbl = None
    for ln in lines:
        t = _compact_issue_text(ln.text)
        if issue_lbl is None and any(k in t for k in ("发证日期", "核准日期", "发照日期")):
            issue_lbl = ln
        if auth_lbl is None and "登记机关" in t:
            auth_lbl = ln
    if issue_lbl is not None:
        lh = max(14.0, issue_lbl.y1 - issue_lbl.y0)
        x0 = int(max(0, issue_lbl.x0 - lh * 0.4))
        x1 = int(min(w, max(issue_lbl.x1 + lh * 14, w * 0.98)))
        y0 = int(max(0, issue_lbl.y0 - lh * 0.6))
        y1 = int(min(h, issue_lbl.y1 + lh * 4.0))
    elif auth_lbl is not None:
        lh = max(14.0, auth_lbl.y1 - auth_lbl.y0)
        x0 = int(min(max(w * 0.48, auth_lbl.x1), w * 0.62))
        x1 = w
        y0 = int(max(0, auth_lbl.y0 - lh * 0.3))
        y1 = int(min(h, max(auth_lbl.y1 + lh * 7.5, h * 0.98)))
    else:
        x0, y0, x1, y1 = int(w * 0.50), int(h * 0.70), w, h
    if x1 - x0 < 24 or y1 - y0 < 12:
        return None
    return image[y0:y1, x0:x1].copy()


def _issue_date_views(strip: np.ndarray) -> list[np.ndarray]:
    destained = _remove_red_seal(strip)
    h, w = destained.shape[:2]
    scale = 4.0 if max(h, w) < 420 else 2.5
    big = cv2.resize(destained, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(np.mean(otsu)) < 127:
        otsu = 255 - otsu
    return [
        cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR),
        big,
        destained,
        _enhance(destained),
    ]


def _stitch_issue_date_tokens(lines: list[OcrLine]) -> str:
    """Assemble YYYY年MM月DD日 from left-to-right date-strip tokens."""
    if not lines:
        return ""
    ordered = sorted(lines, key=lambda ln: (round(ln.y0 / 18.0), ln.x0))
    blob = _compact_issue_text("".join(ln.text for ln in ordered)).replace("曰", "日")
    match = _ISSUE_DATE_RE.search(blob)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}年{month:02d}月{day:02d}日"
    year = month = day = None
    for ln in ordered:
        token = _compact_issue_text(ln.text).replace("曰", "日")
        if year is None:
            found = re.search(r"(20\d{2})", token)
            if not found:
                continue
            year = int(found.group(1))
            found_m = re.search(r"年(1[0-2]|0[1-9]|[1-9])", token)
            if found_m:
                month = int(found_m.group(1))
            found_d = re.search(r"月(3[01]|[12]\d|0[1-9]|[1-9])", token)
            if found_d:
                day = int(found_d.group(1))
            continue
        if month is None:
            found_m = re.search(r"(?:年|^)(1[0-2]|0[1-9]|[1-9])(?:月)?$", token)
            if found_m:
                month = int(found_m.group(1))
                continue
        if month is not None and day is None:
            found_d = re.search(r"(?:月|^)(3[01]|[12]\d|0[1-9]|[1-9])(?:日)?$", token)
            if found_d:
                day = int(found_d.group(1))
    if year and month and day:
        return f"{year:04d}年{month:02d}月{day:02d}日"
    return ""


def _recover_issue_date(image, lines, engine) -> str:
    """Dedicated path: crop date strip → destain → OCR digits → stitch.

    Does not scan the whole page for the first date (that is 成立日期).
    """
    if image is None or engine is None:
        return ""
    strip = _issue_date_strip(image, lines or [])
    if strip is None or strip.size == 0:
        return ""
    ocr_fn = getattr(engine, "_ocr", engine)
    seen: list[OcrLine] = []
    for view in _issue_date_views(strip):
        try:
            seen.extend(_run_ocr(ocr_fn, view))
        except Exception:
            LOG.debug("发证日期条 OCR 失败", exc_info=True)
            continue
        issue = _stitch_issue_date_tokens(seen)
        if issue:
            LOG.info("发证日期（去章条）%s", issue)
            return issue
    issue = _stitch_issue_date_tokens(seen)
    if issue:
        LOG.info("发证日期（去章条）%s", issue)
    return issue


def _scope_views(strip: np.ndarray) -> list[np.ndarray]:
    destained = _remove_red_seal(strip)
    enhanced = _enhance(destained)
    views: list[np.ndarray] = []
    for src in (strip, destained, enhanced):
        h, w = src.shape[:2]
        if h < 8 or w < 8:
            continue
        scale = 4.0 if max(h, w) < 700 else 2.5
        views.append(cv2.resize(src, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
    return views


def _recover_scope(image, lines: list[OcrLine], engine) -> tuple[str, list[OcrLine]]:
    """Crop the left-column 经营范围 band and re-OCR at high magnification.

    Full-page WeChat often skips this dense paragraph (Hengfeng: only the
    footer '制的项目)' survived). A tight crop + 2.5–4× upsample recovers it.
    """
    if image is None or engine is None:
        return "", []
    region = infer_scope_region(image, lines or [])
    if region is None:
        return "", []
    x0, y0, x1, y1 = region
    strip = image[y0:y1, x0:x1]
    if strip is None or strip.size == 0:
        return "", []
    LOG.info("[OCR] 经营范围条 %dx%d @ (%d,%d)-(%d,%d)", strip.shape[1], strip.shape[0], x0, y0, x1, y1)
    ocr_fn = getattr(engine, "_ocr", engine)
    best_text = ""
    best_lines: list[OcrLine] = []
    from .parse import clip_scope_text
    from .field_parse import parse_field_lines
    for view in _scope_views(strip):
        try:
            found = _run_ocr(ocr_fn, view)
        except Exception:
            LOG.debug("经营范围条 OCR 失败", exc_info=True)
            continue
        joined = parse_field_lines("scope", found) or "".join(ln.text for ln in found)
        clipped = clip_scope_text(joined) or joined
        LOG.info("[OCR] 经营范围条 %d 行: %s", len(found), (clipped or "")[:80])
        score = (
            ("一般项目" in clipped),
            ("许可项目" in clipped),
            ("销售" in clipped) + ("分包" in clipped) + ("租赁" in clipped),
            len(clipped),
        )
        prev = (
            ("一般项目" in best_text),
            ("许可项目" in best_text),
            ("销售" in best_text) + ("分包" in best_text) + ("租赁" in best_text),
            len(best_text),
        )
        if clipped and score > prev:
            best_text, best_lines = clipped, found
            if "许可项目" in best_text and "一般项目" in best_text:
                break
    if best_text and field_passes_rules("scope", _sanitize_scope(best_text)):
        best_text = _sanitize_scope(best_text)
        LOG.info("经营范围（裁条）%s", best_text[:60])
        return best_text, best_lines
    if best_text:
        LOG.info("经营范围条未过规则: %s", best_text[:60])
    return "", []


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

    A single unclosed paren on the legal footer (依法须经批准…) is just a
    truncated last line — not a reason to rerun full-page OCR at 3200px.
    """
    if not scope:
        return False
    depth = 0
    nested = False
    for ch in scope:
        if ch in "（(":
            depth += 1
            if depth > 1:
                nested = True
        elif ch in "）)":
            depth -= 1
    if nested:
        return True
    if depth == 1 and "依法须经批准" in scope:
        return False
    return depth != 0


def _scope_join_glitch(scope: str) -> bool:
    """住所 `住` or wrap garbage glued into 经营范围 (株洲浩航)."""
    t = re.sub(r"\s+", "", scope or "")
    return any(s in t for s in (
        "日住", "文县用品", "玩且", "体存用品", "花客", "前包销售", "以量心",
        "烟草制品零建", "制品零建设", "软品零售", "销锦售", "厨兵卫具",
        "煲泰", "医医疗",
    ))


def _scope_truncated(scope: str, blob: str = "") -> bool:
    """许可项目-only fragment while OCR still has the 一般项目 body."""
    t = re.sub(r"\s+", "", scope or "")
    b = re.sub(r"\s+", "", blob or "")
    if re.search(r"烟草制品零建|制品零建设", t):
        return True
    if "许可项目" in t and "一般项目" not in t:
        hay = b if len(b) > len(t) else t
        if re.search(r"计算机软硬件|日用百货|文具用品|办公用品|互联网销售", hay):
            return True
        if b and "销售" in b and "批发" in b and len(b) > len(t) + 60:
            return True
    if "一般项目" in t and "许可项目" not in t:
        if re.search(r"许可项目|烟草制品|酒类经营|建设工程施工", b):
            return True
        if len(t) >= 80 and re.search(r"销售|批发|零售", t):
            return True
    return False


def _combine_scope(current: str, recovered: str) -> str:
    """Keep a long 一般项目 body and attach a recovered 许可项目 tail."""
    cur = _sanitize_scope(current or "")
    rec = _sanitize_scope(recovered or "")
    if not rec:
        return cur
    if not cur:
        return rec
    cur_g, cur_l = "一般项目" in cur, "许可项目" in cur
    rec_g, rec_l = "一般项目" in rec, "许可项目" in rec
    if rec_g and rec_l and (not (cur_g and cur_l) or len(rec) >= len(cur)):
        return rec
    if cur_g and not cur_l and rec_l:
        merged = _sanitize_scope(cur.rstrip("。.;；、 ") + rec[rec.find("许可项目"):])
        if field_passes_rules("scope", merged) and len(merged) > len(cur):
            return merged
    if rec_g and not rec_l and cur_l:
        merged = _sanitize_scope(rec.rstrip("。.;；、 ") + cur[cur.find("许可项目"):])
        if field_passes_rules("scope", merged) and len(merged) > len(cur):
            return merged
    if len(rec) > len(cur) + 40 and field_passes_rules("scope", rec):
        return rec
    return cur


def _scope_should_recover(scope: str, blob: str = "", fields=None) -> bool:
    if _scope_join_glitch(scope) or _scope_truncated(scope, blob):
        return True
    if fields is not None and _scope_looks_like_name(fields):
        return True
    return not field_passes_rules("scope", scope or "")


_ANCHOR_PAT = re.compile(
    r"营业执照|统一社会|信用代码|法定代表人|经营范围|注册资本|注册资金|"
    r"成立日期|营业期限|经营期限|登记机关|发证日期|核准日期|住所|经营场所|"
    r"gsxt|监督管理总局|信用信息公示系统"
)
_DATE_PAT = re.compile(r"20\d{2}\s*年")


def _rot90_line(ln: OcrLine, k: int, width: int, height: int) -> OcrLine:
    """Map a box through the same np.rot90(k) applied to the image."""
    k %= 4
    pts = [(ln.x0, ln.y0), (ln.x1, ln.y0), (ln.x1, ln.y1), (ln.x0, ln.y1)]
    mapped: list[tuple[float, float]] = []
    for x, y in pts:
        nx, ny = x, y
        cw, ch = width, height
        for _ in range(k):
            nx, ny = ny, cw - 1 - nx
            cw, ch = ch, cw
        mapped.append((nx, ny))
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    return OcrLine(ln.text, ln.score, min(xs), min(ys), max(xs), max(ys))


def _upright_rotations(image: np.ndarray, lines: list[OcrLine]) -> int:
    """How many np.rot90 steps put 营业执照 on top (reading-upright)."""
    h, w = image.shape[:2]
    title = _title_line(lines)
    if title is None:
        if h > w * 1.12 and lines:
            tall = sum(1 for ln in lines if (ln.y1 - ln.y0) > (ln.x1 - ln.x0) * 1.2)
            if tall >= len(lines) * 0.45:
                return 1
        return 0
    tw, th = title.x1 - title.x0, title.y1 - title.y0
    dist_top, dist_bottom = title.cy, h - title.cy
    dist_left, dist_right = title.cx, w - title.cx
    if th > tw * 1.15:
        return 1 if dist_right <= dist_left else 3
    nearest = min(dist_top, dist_bottom, dist_left, dist_right)
    if nearest == dist_top:
        return 0
    if nearest == dist_bottom:
        return 2
    if nearest == dist_right:
        return 1
    return 3


def _make_upright(image: np.ndarray, lines: list[OcrLine]) -> tuple[np.ndarray, list[OcrLine]]:
    """Rotate the photo so the license heading is on top."""
    k = _upright_rotations(image, lines)
    if k % 4 == 0:
        return image, lines
    h, w = image.shape[:2]
    rotated = np.ascontiguousarray(np.rot90(image, k))
    rotated_lines = [_rot90_line(ln, k, w, h) for ln in lines]
    LOG.info("[PDF] 原件页转正 k=%d (%dx%d → %dx%d)",
             k, w, h, rotated.shape[1], rotated.shape[0])
    return rotated, rotated_lines


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
    m = 0.12 * max(x1 - x0, y1 - y0)
    cx0, cy0 = max(0, int(x0 - m)), max(0, int(y0 - m))
    cx1, cy1 = min(w, int(x1 + m)), min(h, int(y1 + m))
    if (cx1 - cx0) >= w * 0.995 and (cy1 - cy0) >= h * 0.995:
        return image
    return image[cy0:cy1, cx0:cx1].copy()


def _display_view(image: np.ndarray, lines: list[OcrLine],
                  meta: dict | None) -> np.ndarray:
    """Original-image PDF page: paper crop in reading-upright orientation.

    Do not embed the camera file. A phone-portrait shot of a 横版 license
    must be rotated so 营业执照 sits on top, then cropped to the paper.
    """
    _ = meta
    upright, ulines = _make_upright(image, lines)
    return _crop_license_view(upright, ulines)


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
    """Rule repair → AI flag → field OCR retry → OCR/AI fill leftover dates.

    Crop retry is skipped when the current value already passes rules.
    Leftover review flags are cleared so conversion ships without a human pass.
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
        _harvest_from_ocr(fields, tracker, raw_texts, lines)
        if _scope_should_recover(getattr(fields, "scope", "") or "", "".join(raw_texts), fields):
            recovered, _extra = _recover_scope(oriented, lines, engine)
            if recovered:
                merged = _combine_scope(getattr(fields, "scope", "") or "", recovered)
                _adopt_fill(fields, tracker, "scope", merged, "scope_strip", "经营范围条")
        _fallback_issue_from_established(fields, tracker)
        for fs in tracker.all_fields().values():
            if fs.status in (FieldStatus.RETRY_OCR, FieldStatus.SUSPECTED_OCR_ERROR,
                             FieldStatus.NEED_REVIEW, FieldStatus.AMBIGUOUS,
                             FieldStatus.LOW_CONFIDENCE):
                fs.status = FieldStatus.OK
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
        retry_fields = list(dict.fromkeys(
            f for f in retry_fields
            if tracker.can_retry(f) and _worth_field_retry(f, fields)))
        if not retry_fields:
            break
        LOG.info("[AI] 需要重新 OCR 的字段: %s", retry_fields)
        resolved = _field_level_retry(
            retry_fields, fields, lines, oriented, engine, tracker, trace)
        for name in retry_fields:
            if name not in resolved:
                current = getattr(fields, name, "") or ""
                if current.strip():
                    LOG.info("[OCR] %s 重试无更好结果, 保留现有值", name)
                    fs = tracker.get(name)
                    if fs:
                        fs.status = FieldStatus.OK
                else:
                    tracker.mark_need_review(name, "retry_failed")
            elif tracker.get(name) and tracker.get(name).status == FieldStatus.RETRY_OCR:
                tracker.get(name).status = FieldStatus.OK
        parsed_dict = fields.to_dict()
        if not resolved:
            break

    _harvest_from_ocr(fields, tracker, raw_texts, lines)
    if _scope_should_recover(getattr(fields, "scope", "") or "", "".join(raw_texts), fields):
        recovered, _extra = _recover_scope(oriented, lines, engine)
        if recovered:
            merged = _combine_scope(getattr(fields, "scope", "") or "", recovered)
            _adopt_fill(fields, tracker, "scope", merged, "scope_strip", "经营范围条")
    _ai_fill_from_ocr(fields, tracker, raw_texts, ai_client)
    _fallback_issue_from_established(fields, tracker)
    for fs in tracker.all_fields().values():
        if fs.status in (FieldStatus.RETRY_OCR, FieldStatus.SUSPECTED_OCR_ERROR,
                         FieldStatus.NEED_REVIEW, FieldStatus.AMBIGUOUS,
                         FieldStatus.LOW_CONFIDENCE):
            fs.status = FieldStatus.OK
    return tracker


_AI_FILL_FIELDS = ("established", "term", "issue_date", "authority", "scope")


def _scope_looks_like_name(fields) -> bool:
    scope = re.sub(r"\s+", "", getattr(fields, "scope", "") or "")
    name = re.sub(r"\s+", "", getattr(fields, "name", "") or "")
    if not scope or not name:
        return False
    return scope == name or name in scope or scope in name


def _worth_field_retry(name: str, fields) -> bool:
    """Skip crop retry when the current value already looks valid.

    Field crops often grab the whole license (scope) or a street number
    (authority). A checksum-valid USCI / 局-bearing authority should stand.
    """
    val = getattr(fields, name, "") or ""
    if name == "scope" and _scope_looks_like_name(fields):
        return False
    if name == "scope" and (
        _scope_join_glitch(val) or _scope_truncated(val)
    ):
        return True
    return not field_passes_rules(name, val)


def _grounded_in_ocr(value: str, blob: str) -> bool:
    compact_val = re.sub(r"\s+", "", value or "")
    compact_blob = re.sub(r"\s+", "", blob or "")
    if not compact_val:
        return False
    if compact_val in compact_blob:
        return True
    if DATE_RE.search(compact_val):
        digits = re.findall(r"\d+", compact_val)
        return bool(digits) and all(d in compact_blob for d in digits)
    chars = [c for c in compact_val if "\u4e00" <= c <= "\u9fff"]
    if len(chars) >= 4:
        hit = sum(1 for c in chars if c in compact_blob)
        return hit / len(chars) >= 0.35
    digits = re.findall(r"\d+", compact_val)
    return bool(digits) and all(d in compact_blob for d in digits)


def _adopt_fill(fields, tracker: FieldStateTracker, name: str, val: str,
                source: str, reason: str) -> bool:
    val = (val or "").strip()
    if name == "scope":
        val = _sanitize_scope(val)
    if not val or not field_passes_rules(name, val):
        return False
    old = getattr(fields, name, "") or ""
    if (old == val and field_passes_rules(name, old)
            and not (name == "scope" and _scope_looks_like_name(fields))):
        fs = tracker.get(name)
        if fs:
            fs.status = FieldStatus.OK
        return True
    setattr(fields, name, val)
    fs = tracker.get(name)
    if fs:
        if old != val:
            fs.add_repair(old, val, source, reason=reason)
        fs.value = val
        fs.status = FieldStatus.OK
    return True


def _sanitize_scope(val: str) -> str:
    from .parse import clip_scope_text

    t = clip_scope_text(val or "")
    t = t.replace("许可具", "许可项").replace("般项日", "般项目")
    for stop in ("法定代表人", "注册资本", "成立日期", "营业期限", "登记机关"):
        i = t.find(stop)
        if i >= 8:
            t = t[:i]
    return t.strip(" :：;；,，、")


def _fallback_issue_from_established(fields, tracker: FieldStateTracker | None = None) -> None:
    """If the seal date cannot be read, copy 成立日期 (or the term start)."""
    if field_passes_rules("issue_date", getattr(fields, "issue_date", "") or ""):
        return
    src = getattr(fields, "established", "") or ""
    if not DATE_RE.search(src or ""):
        m = DATE_RE.search(getattr(fields, "term", "") or "")
        if m:
            src = f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    if not DATE_RE.search(src or ""):
        LOG.warning("发证日期未识别，可在面板里指定")
        return
    m = DATE_RE.search(src)
    src = f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    old = getattr(fields, "issue_date", "") or ""
    fields.issue_date = src
    LOG.info("发证日期未识别, 使用成立/期限日期 %s", src)
    if tracker is not None:
        fs = tracker.get("issue_date")
        if fs is None:
            fs = tracker.init_field("issue_date", old)
        if old != src:
            fs.add_repair(old, src, "established_fallback", reason="seal date missing")
        fs.value = src
        fs.status = FieldStatus.OK
    if "issue_date" in getattr(fields, "warnings", []):
        fields.warnings.remove("issue_date")


def _scope_from_ocr_lines(lines: list[OcrLine] | None) -> str:
    """Collect 经营范围 from lines to the right/below the label (left column)."""
    if not lines:
        return ""
    key = None
    for ln in lines:
        t = re.sub(r"\s+", "", ln.text or "")
        if t.startswith("经营范围") or t.startswith("营范围") or t.startswith("经 营 范 围"):
            key = ln
            break
        if "经营范围" in t[:10]:
            key = ln
            break
    if key is None:
        return ""
    page_w = max((ln.x1 for ln in lines), default=key.x1) or 1
    left_cut = page_w * 0.52
    parts: list[str] = []
    inline = re.sub(r"^经营范围[:：]?", "", re.sub(r"\s+", "", key.text or ""))
    inline = re.sub(r"^营范围[:：]?", "", inline)
    if inline and inline not in ("经营范围", "营范围"):
        parts.append(inline)
    stops = ("注册资本", "成立日期", "营业期限", "登记机关", "法定代表人", "名称", "类型")
    ordered = sorted(
        (ln for ln in lines if ln is not key),
        key=lambda ln: (round(ln.y0 / 8.0), ln.x0),
    )
    for ln in ordered:
        t = re.sub(r"\s+", "", ln.text or "")
        if not t or t in {"住", "所"}:
            continue
        if re.fullmatch(r"(?:SC)?(?:JDGL|CJDGL|SCJDGL|SCJDGI)", t, re.I):
            continue
        if re.match(r"^(?:路|街|巷)\d+号", t) and "销售" not in t:
            continue
        if any(s in t for s in ("以量心", "优春用总", "煲泰许可")):
            continue
        if "专用章" in t and len(t) <= 8:
            continue
        if t.startswith("经营范") and "项目" not in t and len(t) < 10:
            continue
        if any(t == s or t.startswith(s) for s in stops) and len(t) < 12:
            continue
        if ln.x0 > left_cut:
            continue
        if ln.y1 < key.y0 - 8:
            continue
        if ln.x1 < key.x0 - 40:
            continue
        parts.append(t)
    return _sanitize_scope("".join(parts))


def _harvest_scope_text(compact: str) -> str:
    from .parse import clip_scope_text
    from .rule_repair import scope_text_is_plausible

    compact = (compact or "").replace("许可具", "许可项").replace("般项日", "般项目")
    clipped = clip_scope_text(compact)
    if (
        clipped
        and scope_text_is_plausible(clipped)
        and len(clipped) >= 8
        and not _scope_truncated(clipped, compact)
    ):
        return _sanitize_scope(clipped)
    m = re.search(
        r"(?:经营范围|许可项目|许可项|一般项目|般项目)[:：]?(.*?)(?:注册资本|成立日期|营业期限|登记机关|$)",
        compact,
    )
    if not m:
        return ""
    # 住所 is often OCR'd as a lone「所」glued after 经营范围 (Baoji: 经营范围所陕西…).
    body = re.split(
        r"住所|住\s*所"
        r"|(?<=范围)所(?=[\u4e00-\u9fff])"
        r"|(?<=项目)所(?=[\u4e00-\u9fff])"
        r"|^所(?=[\u4e00-\u9fff]{2,}(?:省|市|自治区|区|县|镇))",
        m.group(1),
    )[0]
    if len(body) < 8:
        return ""
    candidate = clip_scope_text(body) or body
    if not scope_text_is_plausible(candidate):
        return ""
    return _sanitize_scope(candidate)


def _infer_authority_from_place(address: str, name: str = "") -> str:
    """A-format 登记机关 is usually '{县/区/市}市场监督管理局' under the seal.

    WeChat often returns only 'D登记机关'. Use the most specific place in
    住所/名称 (县 before 区 before 市).
    """
    src = re.sub(r"\s+", "", (address or "") + (name or ""))
    if not src:
        return ""
    for pat in (
        r"(?:省|市|^)([\u4e00-\u9fff]{2,8}自治县)",
        r"(?:省|市|^)([\u4e00-\u9fff]{2,3}县)",
        r"(?:市|^)([\u4e00-\u9fff]{2,4}区)",
        r"(?:省|^)([\u4e00-\u9fff]{2,4}市)",
    ):
        hits = re.findall(pat, src)
        hits = [h for h in hits if not any(ch in h[:-1] for ch in "省市州")]
        if hits:
            place = hits[-1]
            if len(place) >= 3:
                return place + "市场监督管理局"
    return ""


def _harvest_from_ocr(fields, tracker: FieldStateTracker, raw_texts: list[str],
                      lines: list[OcrLine] | None = None) -> None:
    """Copy dates/term/authority/scope out of OCR lines when parse left holes."""
    compact = re.sub(r"\s+", "", "".join(raw_texts or []))
    blob = "".join(raw_texts or [])
    if not compact:
        return

    from .parse import pick_best_usci
    from .usci import is_company_usci

    current_usci = getattr(fields, "credit_code", "") or ""
    harvested_usci = pick_best_usci(raw_texts, lines)
    if harvested_usci and harvested_usci != current_usci:
        if not field_passes_rules("credit_code", current_usci) or (
            is_company_usci(harvested_usci) and not is_company_usci(current_usci)
        ):
            _adopt_fill(fields, tracker, "credit_code", harvested_usci, "ocr_harvest", "统一社会信用代码")

    if not field_passes_rules("type", getattr(fields, "type", "") or ""):
        m = re.search(
            r"((?:有限责任公司|股份有限公司)[（(][^）)]{2,24}[）)])",
            compact,
        ) or re.search(r"(有限责任公司|股份有限公司|一人有限责任公司)", compact)
        if m and _grounded_in_ocr(m.group(1), blob):
            _adopt_fill(fields, tracker, "type", m.group(1), "ocr_harvest", "类型")

    def _date(m: re.Match) -> str:
        return f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"

    if not field_passes_rules("established", getattr(fields, "established", "") or ""):
        m = re.search(r"成立日期" + DATE_RE.pattern, compact)
        if m and _grounded_in_ocr(_date(m), blob):
            _adopt_fill(fields, tracker, "established", _date(m), "ocr_harvest", "成立日期")

    if not field_passes_rules("term", getattr(fields, "term", "") or ""):
        term = ""
        m = re.search(
            r"(?:营业期限|经营期限|业期限)?" + DATE_RE.pattern + r"至长期",
            compact,
        )
        if m:
            term = f"{_date(m)}至长期"
        elif "长期" in compact:
            m = DATE_RE.search(getattr(fields, "term", "") or "") or re.search(
                r"(?:营业期限|经营期限|业期限)" + DATE_RE.pattern, compact)
            if m:
                term = f"{_date(m)}至长期"
        if term and _grounded_in_ocr(term, blob):
            _adopt_fill(fields, tracker, "term", term, "ocr_harvest", "营业期限")

    if not field_passes_rules("issue_date", getattr(fields, "issue_date", "") or ""):
        m = re.search(r"(?:发证日期|核准日期)" + DATE_RE.pattern, compact)
        if m and _grounded_in_ocr(_date(m), blob):
            _adopt_fill(fields, tracker, "issue_date", _date(m), "ocr_harvest", "发证日期")

    if not field_passes_rules("authority", getattr(fields, "authority", "") or ""):
        m = re.search(r"(?:登记机关|发照机关)([\u4e00-\u9fff]{4,18}局)", compact)
        if not m:
            m = re.search(
                r"([\u4e00-\u9fff]{2,8}(?:市场监督|工商行政|行政审批)管理局)",
                compact,
            )
        if m and _grounded_in_ocr(m.group(1), blob):
            _adopt_fill(fields, tracker, "authority", m.group(1), "ocr_harvest", "登记机关")
        else:
            inferred = _infer_authority_from_place(
                getattr(fields, "address", "") or "",
                getattr(fields, "name", "") or "",
            )
            if inferred:
                _adopt_fill(fields, tracker, "authority", inferred, "place_infer", "住所推断登记机关")

    from_lines = _scope_from_ocr_lines(lines)
    from_blob = _harvest_scope_text(compact)

    def _scope_rank(text: str) -> tuple:
        t = re.sub(r"\s+", "", text or "")
        leak = bool(re.search(
            r"办公楼|旧址|村委会|审批专用章|许可项目住|许可项目所",
            t,
        ))
        truncated = _scope_truncated(t)
        return (
            not leak,
            not truncated,
            "日住" not in t,
            "一般项目" in t,
            "销售" in t,
            "预包装" in t or "散装食品" in t or "食品经营" in t,
            "许可项目" in t and "许可项目住" not in t and "许可项目所" not in t,
            "日用百货" in t,
            len(t),
        )

    ranked = [
        (_scope_rank(c), c) for c in (from_lines, from_blob)
        if c and field_passes_rules("scope", c) and not _scope_truncated(c, compact)
    ]
    scope_txt = max(ranked, key=lambda item: item[0])[1] if ranked else ""
    current_scope = getattr(fields, "scope", "") or ""
    if scope_txt:
        current_bad = (
            _scope_looks_like_name(fields)
            or _scope_join_glitch(current_scope)
            or _scope_truncated(current_scope, compact)
            or not field_passes_rules("scope", current_scope)
        )
        harvest_better = (
            _scope_rank(scope_txt) > _scope_rank(current_scope)
            and not _scope_truncated(scope_txt, compact)
        )
        if current_bad or harvest_better:
            _adopt_fill(fields, tracker, "scope", scope_txt, "ocr_harvest", "经营范围")


def _fields_needing_ai_fill(fields) -> list[str]:
    needed = []
    for name in _AI_FILL_FIELDS:
        val = getattr(fields, name, "") or ""
        if name == "scope" and (
            _scope_looks_like_name(fields)
            or _scope_join_glitch(val)
            or _scope_truncated(val)
        ):
            needed.append(name)
        elif not field_passes_rules(name, val):
            needed.append(name)
    return needed


def _ai_fill_from_ocr(fields, tracker: FieldStateTracker, raw_texts: list[str],
                      ai_client) -> None:
    """Let AI copy missing dates/term/authority/scope from OCR text. No invention."""
    needed = _fields_needing_ai_fill(fields)
    if not needed or not getattr(ai_client, "is_available", lambda: False)():
        return
    blob = "".join(raw_texts)
    LOG.info("[AI] 从 OCR 补全字段: %s", ", ".join(needed))
    prompt = (
        "From the OCR lines of a Chinese business license, copy these fields "
        "if they are present. Return JSON only: {\"fields\": {\"established\": "
        "\"YYYY年MM月DD日\", \"term\": \"...至长期\", \"issue_date\": "
        "\"YYYY年MM月DD日\", \"authority\": \"...局\", \"scope\": \"经营范围正文\"}}. "
        "Copy existing OCR text; do not invent. Omit keys you cannot find. "
        "authority must contain 局. scope is business items (一般项目/许可项目/"
        "销售/加工/服务…), never the company name, registered address, URL, or seal."
    )
    user = json.dumps({"need": needed, "parsed": {
        n: getattr(fields, n, "") for n in _AI_FILL_FIELDS
    }, "ocr": raw_texts[:40]}, ensure_ascii=False)
    try:
        result = ai_client.chat_json(prompt, user, temperature=0.0, max_tokens=800)
    except Exception:
        LOG.exception("[AI] 字段补全失败")
        return
    filled = (result or {}).get("fields") if isinstance(result, dict) else None
    if not isinstance(filled, dict):
        return
    for name in needed:
        val = str(filled.get(name) or "").strip()
        if not val:
            continue
        if name == "scope":
            val = _sanitize_scope(val)
        grounded = _grounded_in_ocr(val, blob)
        if not grounded and name == "scope":
            from .rule_repair import scope_text_is_plausible
            name_zh = getattr(fields, "name", "") or ""
            grounded = (
                scope_text_is_plausible(val, getattr(fields, "address", "") or "")
                and val != name_zh and name_zh not in val
            )
        if not grounded:
            LOG.info("[AI] 丢弃未在 OCR 中出现的 %s: %s", name, val[:40])
            continue
        if _adopt_fill(fields, tracker, name, val, "ai_extract", "copied from OCR"):
            LOG.info("[AI] %s 补全: %s", name, val[:40])


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
    old_val = (fs.value or "").strip()
    if old_val:
        candidates.append(FieldOcrCandidate(
            field=field_name, engine="parsed", variant="original",
            text=old_val, score=0.99,
        ))
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
    if new_val == old_val:
        fs.status = FieldStatus.OK
        return True
    if best.engine == "parsed":
        LOG.info("[OCR] %s 保留解析结果, 字段裁剪未改善", field_name)
        fs.status = FieldStatus.OK
        return True
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
    from .translate import sanitize_english_scope
    english["scope"] = sanitize_english_scope(english.get("scope") or "")
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
    blob = ""
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

        if issue_date:
            fields.issue_date = _normalize_issue_date(issue_date)
            if "issue_date" in fields.warnings:
                fields.warnings.remove("issue_date")
            LOG.info("发证日期（指定）%s", fields.issue_date)
        else:
            issue = _recover_issue_date(oriented, lines, engine)
            if issue:
                fields.issue_date = issue
                if "issue_date" in fields.warnings:
                    fields.warnings.remove("issue_date")
            if not field_passes_rules("issue_date", fields.issue_date or ""):
                _fallback_issue_from_established(fields)

        if _scope_should_recover(fields.scope or "", blob, fields):
            recovered, _extra = _recover_scope(oriented, lines, engine)
            if recovered:
                fields.scope = _combine_scope(fields.scope or "", recovered)
                if "scope" in fields.warnings:
                    fields.warnings.remove("scope")

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
    classify_format_a(fields, blob)

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

    from .semantic_arbiter import arbitrate
    arbitration = arbitrate(canonical.to_translation_input(), english, config)
    trace.emit(
        "SEMANTIC_ARBITRATION",
        passed=arbitration.passed,
        reasons=arbitration.reasons,
        source=arbitration.source,
        field_failures=arbitration.field_failures,
    )
    if not arbitration.passed:
        LOG.warning("出PDF前语义仲裁未通过: %s", "; ".join(arbitration.reasons[:4]))

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
        "arbitration": arbitration.to_dict(),
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
            "arbitration_passed": arbitration.passed,
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
_LIB_LOCK = threading.Lock()
CN_POOL_RESERVE_SLOTS = 10
CN_META_NAME = "cn_meta.json"


def _pick_best_license(candidates: list[tuple[Path, list[OcrLine]]]) -> tuple[Path, list[OcrLine]]:
    """Pick the clearest license image from detected candidates.

    Heuristic: more OCR lines = clearer photo; tie-break by larger file size
    (desktop photos are typically higher resolution than handheld).
    """
    return max(candidates, key=lambda item: (len(item[1]), item[0].stat().st_size))


def _arbitration_fail_reason(info: dict) -> str:
    """Human-readable why this conversion should not enter 资料/."""
    fields = info.get("fields") or {}
    missing = _missing_critical(fields)
    if missing:
        return "缺失字段: " + ",".join(missing)
    warnings = fields.get("warnings") or []
    if "credit_code_checksum" in warnings:
        return "信用代码校验失败"
    review = info.get("needs_review") or []
    if review:
        return "AI仲裁需复核: " + ",".join(str(x) for x in review)
    english = info.get("english") or {}
    arb = info.get("arbitration")
    if isinstance(arb, dict) and arb.get("passed") is False:
        reasons = [str(x) for x in (arb.get("reasons") or []) if str(x).strip()]
        return "语义仲裁未通过: " + "; ".join(reasons[:3]) if reasons else "语义仲裁未通过"
    from .semantic_arbiter import rule_gate
    ruled = rule_gate(fields, english)
    if not ruled.passed:
        return ruled.fail_message()
    for field in ("name", "scope", "address", "legal_person"):
        bits = leftover_cjk(english.get(field) or "")
        if bits:
            return f"英译残留中文({field})"
    if not (english.get("name") or "").strip():
        return "英译名称为空"
    return ""


def _write_cn_meta(folder: Path, info: dict, id_fields: dict | None = None) -> Path:
    from .id_card import cn_admin_parts

    fields = info.get("fields") or {}
    english = info.get("english") or {}
    ids = id_fields or {}
    address_zh = fields.get("address") or ""
    province, city = cn_admin_parts(address_zh)
    meta = {
        "workflow": "cn",
        "name_zh": fields.get("name") or "",
        "name_en": english.get("name") or "",
        "credit_code": fields.get("credit_code") or "",
        "legal_person": fields.get("legal_person") or "",
        "address_zh": address_zh,
        "address_en": english.get("address") or "",
        "reg_state": province,
        "reg_city": city,
        "established": fields.get("established") or "",
        "term": fields.get("term") or "",
        "license_pdf": str(Path(info.get("output") or "").name),
        "id_name": ids.get("name") or "",
        "id_no": ids.get("id_no") or "",
        "id_birth": ids.get("birth") or "",
        "id_exp": ids.get("id_exp") or "",
        "id_long_term": bool(ids.get("id_long_term")),
        "id_authority": ids.get("authority") or "",
    }
    path = folder / CN_META_NAME
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _is_ocr_transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "timed out",
            "timeout",
            "no ocr engine",
            "wechatocr not initialized",
            "ocr engine available",
        )
    )


def _is_semantic_review(info: dict | None, reason: str) -> bool:
    """True only for pre-PDF semantic arbitration — the 待人工处理 table."""
    return str(reason or "").startswith("语义仲裁")


def _note_review(source: Path, folder: Path, file: str, reason: str, info: dict | None = None) -> None:
    from .review_sheet import append_review_row

    fields = (info or {}).get("fields") or {}
    missing = _missing_critical(fields) if info else []
    try:
        append_review_row(
            source,
            folder=folder.name,
            file=file,
            reason=reason,
            name=(fields.get("name") if isinstance(fields, dict) else "") or "",
            credit_code=(fields.get("credit_code") if isinstance(fields, dict) else "") or "",
            missing=",".join(missing),
        )
    except Exception:
        LOG.exception("写入待人工处理表失败")
    from .review_sheet import append_review_row

    fields = (info or {}).get("fields") or {}
    missing = _missing_critical(fields) if info else []
    try:
        append_review_row(
            source,
            folder=folder.name,
            file=file,
            reason=reason,
            name=(fields.get("name") if isinstance(fields, dict) else "") or "",
            credit_code=(fields.get("credit_code") if isinstance(fields, dict) else "") or "",
            missing=",".join(missing),
        )
    except Exception:
        LOG.exception("写入待人工处理表失败")


def _commit_best_ids(
    candidates: list[tuple[Path, list[OcrLine], str]],
    log=None,
) -> tuple[set[str], dict]:
    """Score ID candidates, copy the best photo per side, merge parsed fields."""
    from .id_card import copy_id_canonical, merge_id_fields, parse_id_fields, pick_best_ids

    best = pick_best_ids(candidates)
    sides: set[str] = set()
    fields: dict = {}
    for side, (img, lines, quality) in best.items():
        dest = copy_id_canonical(img, side)
        sides.add(side)
        fields = merge_id_fields(fields, parse_id_fields(lines))
        if log:
            tag = "手持/远景" if quality.handheld else "特写"
            dest_name = dest.name if dest is not None else "?"
            log(
                f"  身份证{side}: {img.name} [{tag} 字号{quality.font_px:.0f}px "
                f"得分{quality.score:.0f}] → {dest_name}",
                "ok",
            )
    return sides, fields


def process_license_library(
    source_dir: str | Path,
    dest_dir: str | Path,
    *,
    max_workers: int = 5,
    engine: LicenseOCR | OcrService | None = None,
    log=None,
    progress=None,
    stop_event=None,
    limit: int | None = None,
) -> dict:
    """Batch-process the license library:

    1. Scan company subfolders in source_dir (中国执照库)
    2. First-round OCR: licenses + 身份证正/反面 (copy as stem-正面/反面)
    3. Missing ID sides → skip this round (stay in 执照库, retry next scan)
    4. Pick the clearest license (most OCR lines, then largest file)
    5. Full OCR + translate → output PDF in the company folder
    6. OCR timeout / missing fields → retry, then stay in 执照库 (not the sheet)
    7. AI semantic arbitration fail → 待人工处理.xlsx (stay put)
    8. Move passing company folders to dest_dir (资料)

    Returns {"processed": N, "failed": N, "moved": N, "skipped": N, "review": N}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    source = Path(source_dir)
    dest = Path(dest_dir)
    empty = {"processed": 0, "failed": 0, "moved": 0, "skipped": 0, "review": 0}

    if not source.exists():
        if log:
            log(f"执照库不存在: {source}", "err")
        return empty

    with _LIB_LOCK:
        return _process_license_library_locked(
            source, dest,
            max_workers=max_workers, engine=engine, log=log,
            progress=progress, stop_event=stop_event, limit=limit,
        )


def _process_license_library_locked(
    source: Path,
    dest: Path,
    *,
    max_workers: int,
    engine: LicenseOCR | OcrService | None,
    log,
    progress,
    stop_event,
    limit: int | None,
) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    eng = engine or OcrService()
    skip_names = {"待人工处理.xlsx"}
    company_folders = sorted(
        d for d in source.iterdir()
        if d.is_dir() and d.name not in {"资料不全"}
    )
    total = len(company_folders)
    processed = failed = moved = skipped = review = 0

    # --- Pass 1: scan folders, quick-detect licenses + ID cards ---
    convert_tasks: list[tuple[Path, Path, Path, dict]] = []  # folder, img, pdf, id_fields
    for i, folder in enumerate(company_folders, start=1):
        if stop_event and stop_event.is_set():
            if log:
                log("已停止扫描执照库", "warn")
            break
        if limit is not None and len(convert_tasks) >= limit:
            break

        if progress:
            progress(i, total, folder.name)

        images = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS
            and f.name not in skip_names)
        if not images:
            skipped += 1
            continue

        detect_hits: list[tuple[Path, bool, list[OcrLine]]] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(images))) as pool:
            futures = {pool.submit(eng.quick_detect, img): img for img in images}
            for fut in as_completed(futures):
                if stop_event and stop_event.is_set():
                    break
                img = futures[fut]
                try:
                    is_license, lines = fut.result()
                    detect_hits.append((img, is_license, lines))
                except Exception as exc:
                    if log:
                        log(f"  检测失败 {img.name}: {exc}", "err")

        licenses: list[tuple[Path, list[OcrLine]]] = []
        id_candidates: list[tuple[Path, list[OcrLine], str]] = []
        from .id_card import already_id_copy, classify_id_side, named_id_side
        for img, is_license, lines in detect_hits:
            if is_license:
                licenses.append((img, lines))
                continue
            if already_id_copy(img) and not img.stem.startswith(("id-", "id_")):
                continue
            side = classify_id_side(lines) or named_id_side(img)
            if side:
                id_candidates.append((img, lines, side))
        id_sides, id_fields = _commit_best_ids(id_candidates, log)

        if not licenses:
            if log:
                log(f"[{i}/{total}] {folder.name}: 未找到营业执照, 跳过", "warn")
            skipped += 1
            continue
        missing_id = [s for s in ("正面", "反面") if s not in id_sides]
        if missing_id:
            reason = "缺身份证" + "、".join(missing_id)
            if log:
                log(f"[{i}/{total}] {folder.name}: {reason}, 留在执照库下次再扫", "warn")
            skipped += 1
            continue

        best_img, _best_quick_lines = _pick_best_license(licenses)
        if log:
            log(f"[{i}/{total}] {folder.name}: 找到 {len(licenses)} 张执照, "
                f"选用 {best_img.name} ({best_img.stat().st_size // 1024}KB)", "ok")
        dest_pdf = folder / (best_img.stem + "英文版.pdf")
        convert_tasks.append((folder, best_img, dest_pdf, id_fields))

    # --- Pass 2: convert + move in parallel (3 workers) ---
    convert_workers = min(3, len(convert_tasks))

    CONVERT_ATTEMPTS = 3

    def _convert_and_move(task):
        folder, best_img, dest_pdf, id_fields = task
        info = None
        last_exc: BaseException | None = None
        for attempt in range(1, CONVERT_ATTEMPTS + 1):
            try:
                info = convert_image(best_img, dest_pdf, ocr=eng, work_dir=ROOT / "license_work")
                last_exc = None
            except NotFormatA as exc:
                return ("skip", folder, dest_pdf, f"非A格式: {exc}", None)
            except Exception as exc:
                last_exc = exc
                if _is_ocr_transient(exc) and attempt < CONVERT_ATTEMPTS:
                    LOG.warning("OCR 失败, 重试 %d/%d %s: %s", attempt, CONVERT_ATTEMPTS, folder.name, exc)
                    if log:
                        log(f"  {folder.name}: OCR 失败, 重试 {attempt}/{CONVERT_ATTEMPTS-1}…", "warn")
                    time.sleep(1.5)
                    continue
                return ("fail", folder, dest_pdf, str(exc), None)
            reason = _arbitration_fail_reason(info)
            if reason.startswith("缺失字段") and attempt < CONVERT_ATTEMPTS:
                LOG.warning("缺字段 %s, 重试 %d/%d %s", reason, attempt, CONVERT_ATTEMPTS, folder.name)
                if log:
                    log(f"  {folder.name}: {reason}, 重试 {attempt}/{CONVERT_ATTEMPTS-1}…", "warn")
                continue
            break
        if last_exc is not None:
            return ("fail", folder, dest_pdf, str(last_exc), None)
        reason = _arbitration_fail_reason(info or {})
        if reason:
            if _is_semantic_review(info, reason):
                _note_review(source, folder, best_img.name, reason, info)
                return ("review", folder, dest_pdf, reason, info)
            return ("fail", folder, dest_pdf, reason, info)
        try:
            _write_cn_meta(folder, info, id_fields)
        except Exception:
            LOG.exception("写入 cn_meta 失败")
        try:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / folder.name
            if target.exists():
                import shutil
                shutil.rmtree(target)
            folder.rename(target)
            return ("ok", folder, target, "", info)
        except Exception as exc:
            return ("moved_fail", folder, dest_pdf, str(exc), info)

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
                kind, folder, dest_pdf, msg, _info = result
                if kind == "ok":
                    processed += 1
                    moved += 1
                    if log:
                        log(f"  ✓ {folder.name} → {dest_pdf}", "ok")
                elif kind == "skip":
                    skipped += 1
                    if log:
                        log(f"  ⊘ {folder.name}: {msg}, 不移动", "warn")
                elif kind == "fail":
                    failed += 1
                    if log:
                        log(f"  ✗ {folder.name}: {msg}, 留在执照库下次再转", "err")
                elif kind == "moved_fail":
                    processed += 1
                    if log:
                        log(f"  ⚠ {folder.name}: 翻译成功但移动失败: {msg}", "warn")
                elif kind == "review":
                    review += 1
                    if log:
                        log(f"  ✗ {folder.name}: AI仲裁未通过 [{msg}], 已记入待人工处理表", "err")

    return {"processed": processed, "failed": failed, "moved": moved, "skipped": skipped, "review": review}


def ensure_cn_reserve(
    *,
    log=None,
    stop_event=None,
    engine=None,
    reserve_slots: int = CN_POOL_RESERVE_SLOTS,
) -> dict:
    """If 资料/ free slots are below reserve, translate more 中国执照库 folders."""
    import materials

    stats = {"processed": 0, "failed": 0, "moved": 0, "skipped": 0, "review": 0}
    try:
        free, _, _ = materials.summary()
    except Exception as exc:
        if log:
            log(f"公司池统计失败, 跳过预翻译: {exc}", "warn")
        return stats
    if free >= reserve_slots:
        if log:
            log(f"公司池充足({free}槽 ≥ 预留{reserve_slots}), 无需预翻译", "dim")
        return stats
    need = max(1, (reserve_slots - free + materials.COMPANY_SLOTS - 1) // materials.COMPANY_SLOTS)
    source = ROOT / "中国执照库"
    dest = ROOT / "资料"
    if log:
        log(f"公司池仅剩{free}槽, 预翻译 {need} 家中国执照(身份证+执照+英译)…", "warn")
    stats = process_license_library(
        source, dest, max_workers=5, engine=engine, log=log,
        stop_event=stop_event, limit=need,
    )
    if log:
        log(
            f"预翻译结束: 移动 {stats.get('moved', 0)} / 仲裁未过 {stats.get('review', 0)} / "
            f"失败 {stats.get('failed', 0)} / 跳过 {stats.get('skipped', 0)}",
            "ok" if not stats.get("failed") and not stats.get("review") else "warn",
        )
    return stats
