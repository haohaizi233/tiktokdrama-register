from __future__ import annotations

import io
import logging
from datetime import date
from pathlib import Path

import fitz
from PIL import Image

LOG = logging.getLogger("license_trans.render")

from paths import BUNDLE_ROOT as ROOT
ASSETS = ROOT / "assets"

PAGE_W = 841.89
PAGE_H = 595.28

QR_HINT = [
    'Scan the QR code to log in the "National',
    'Enterprise Credit Information Publicity System"',
    "for more information about registration, filing,",
    "permission and supervision.",
]

BORDER_LEFT = 32.85
BORDER_TOP = 66.0
BORDER_RIGHT = 809.05
DEFAULT_AUTH_Y = 375.0
DEFAULT_FOOTER_Y = 436.0
DEFAULT_CERT_Y = 479.0
DEFAULT_BORDER_BOTTOM = 433.0
FIELD_Y = 192.0
FIELD_LEADING = 20.3
FIELD_SIZE = 10.4


def _font(path: str) -> fitz.Font:
    return fitz.Font(fontfile=path)


_FONT_FALLBACKS = {
    "regular": [
        "C:/Windows/Fonts/times.ttf",
        "/System/Library/Fonts/Times New Roman.ttf",
        "/Library/Fonts/Times New Roman.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ],
    "bold": [
        "C:/Windows/Fonts/timesbd.ttf",
        "/System/Library/Fonts/Times New Roman Bold.ttf",
        "/Library/Fonts/Times New Roman Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ],
}


def _resolve_font(config_path: str, kind: str) -> fitz.Font:
    candidates = [config_path] if config_path else []
    candidates += _FONT_FALLBACKS.get(kind, [])
    for path in candidates:
        if path and Path(path).exists():
            try:
                return fitz.Font(fontfile=path)
            except Exception:
                LOG.exception("Font load failed: %s", path)
                continue
    return fitz.Font("helv")


def _wrap(text: str, font: fitz.Font, size: float, width: float) -> list[str]:
    if not text:
        return []
    paragraphs = text.replace("\n", "\n").split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split(" ") if paragraph else [""]
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            if font.text_length(trial, fontsize=size) <= width:
                current = trial
                continue
            if current:
                lines.append(current)
            if font.text_length(word, fontsize=size) <= width:
                current = word
            else:
                chunk = ""
                for ch in word:
                    if font.text_length(chunk + ch, fontsize=size) <= width:
                        chunk += ch
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                current = chunk
        lines.append(current)
    return lines


def _baseline(y_top: float, size: float) -> float:
    return y_top + size * 0.82


def _draw(
    page: fitz.Page,
    font: fitz.Font,
    text: str,
    x: float,
    y_top: float,
    size: float,
    *,
    align: str = "left",
    right: float | None = None,
) -> None:
    if not text:
        return
    baseline = _baseline(y_top, size)
    width = font.text_length(text, fontsize=size)
    if align == "center" and right is not None:
        x = (x + right - width) / 2
    elif align == "right" and right is not None:
        x = right - width
    tw = fitz.TextWriter(page.rect)
    tw.append(fitz.Point(x, baseline), text, font=font, fontsize=size)
    tw.write_text(page, color=(0, 0, 0))


def _draw_lines(
    page: fitz.Page,
    font: fitz.Font,
    lines: list[str],
    x: float,
    y_top: float,
    size: float,
    leading: float,
) -> float:
    y = y_top
    for line in lines:
        _draw(page, font, line, x, y, size)
        y += leading
    return y


def compute_english_layout(
    *,
    n_scope_lines: int,
    n_addr_lines: int,
    has_term: bool,
    scope_leading: float = FIELD_LEADING,
    field_y: float = FIELD_Y,
    leading: float = FIELD_LEADING,
) -> dict:
    """Place Registration Authority / footer below a long Business Scope.

    Short licenses keep the printed A4-landscape positions. Long scope flows
    downward on the same page (page height grows); it is not moved onto a
    second sheet.
    """
    scope_top = field_y + leading * 3
    scope_end = scope_top + max(int(n_scope_lines), 1) * scope_leading
    right_y = field_y + leading * 2
    if has_term:
        right_y += leading
    right_end = right_y + max(int(n_addr_lines), 1) * leading
    content_end = max(scope_end, right_end)

    auth_y = DEFAULT_AUTH_Y
    if content_end + 12.0 > auth_y:
        auth_y = content_end + 14.0
    footer_y = auth_y + (DEFAULT_FOOTER_Y - DEFAULT_AUTH_Y)
    cert_y = footer_y + (DEFAULT_CERT_Y - DEFAULT_FOOTER_Y)
    border_bottom = footer_y - (DEFAULT_FOOTER_Y - DEFAULT_BORDER_BOTTOM)
    page_h = max(PAGE_H, cert_y + (PAGE_H - DEFAULT_CERT_Y))
    return {
        "page_h": page_h,
        "border_bottom": border_bottom,
        "auth_y": auth_y,
        "footer_y": footer_y,
        "cert_y": cert_y,
        "scope_end": scope_end,
        "content_end": content_end,
    }


def _make_qr(payload: str) -> bytes:
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_english_page(
    doc: fitz.Document,
    en: dict,
    config: dict,
    qr_bytes: bytes | None = None,
    translation_date: date | None = None,
) -> fitz.Page:
    regular = _resolve_font(config.get("fonts", {}).get("regular", ""), "regular")
    bold = _resolve_font(config.get("fonts", {}).get("bold", ""), "bold")
    trans = config["translator"]
    when = translation_date or date.today()
    month, day, year = when.strftime("%B"), str(when.day), str(when.year)
    date_text = f"{month} {day}, {year}"

    left_x = 43.9
    right_x = 490.6
    col_right = 479.8
    page_right = 803.4
    size = FIELD_SIZE
    leading = FIELD_LEADING
    y = FIELD_Y
    left_width = col_right - left_x

    from .translate import sanitize_english_scope
    scope_body = sanitize_english_scope(en.get("scope") or "").replace("General items:", "\nGeneral items:")
    scope_full = f"Business Scope: {scope_body}".strip()
    scope_size = size
    scope_lines = _wrap(scope_full, regular, scope_size, left_width)
    # Keep a readable size; extremely long scope still flows down rather
    # than being crushed into the printed box.
    while scope_size > 9.0 and len(scope_lines) > 28:
        scope_size -= 0.5
        scope_lines = _wrap(scope_full, regular, scope_size, left_width)
    scope_leading = leading if scope_size >= size else leading * (scope_size / size)

    addr_width = page_right - right_x
    addr_lines = _wrap("Address: " + (en.get("address") or ""), regular, size, addr_width)
    layout = compute_english_layout(
        n_scope_lines=len(scope_lines),
        n_addr_lines=len(addr_lines),
        has_term=bool(en.get("term")),
        scope_leading=scope_leading,
    )

    page = doc.new_page(width=PAGE_W, height=layout["page_h"])
    page.draw_rect(
        fitz.Rect(BORDER_LEFT, BORDER_TOP, BORDER_RIGHT, layout["border_bottom"]),
        color=(0, 0, 0),
        width=0.5,
    )

    emblem = ASSETS / "emblem.jpg"
    if emblem.exists():
        page.insert_image(fitz.Rect(399.0, 20.0, 461.8, 85.0), filename=str(emblem))

    _draw(page, bold, "Unified Social Credit Code", 43.8, 91.0, 12)
    code = en.get("credit_code") or ""
    copy_no = en.get("copy_no") or "1-1"
    _draw(page, regular, f"{code} ({copy_no})" if code else "", 43.8, 114.0, 12)

    title = "Business License"
    title_w = bold.text_length(title, fontsize=36)
    _draw(page, bold, title, (PAGE_W - title_w) / 2, 86.0, 36)
    if en.get("duplicate"):
        dup = "(Duplicate)"
        dup_w = regular.text_length(dup, fontsize=12)
        _draw(page, regular, dup, (PAGE_W - dup_w) / 2, 135.0, 12)

    qr_rect = fitz.Rect(575.8, 95.0, 638.0, 157.0)
    if qr_bytes:
        page.insert_image(qr_rect, stream=qr_bytes)

    y_hint = 90.0
    for line in QR_HINT:
        _draw(page, regular, line, 651.2, y_hint, 7.5)
        y_hint += 15.6

    _draw(page, regular, f"Name: {en.get('name') or ''}", left_x, y, size)
    _draw(page, regular, f"Type: {en.get('type') or ''}", left_x, y + leading, size)
    _draw(page, regular, f"Legal Representative: {en.get('legal_person') or ''}", left_x, y + leading * 2, size)
    _draw_lines(page, regular, scope_lines, left_x, y + leading * 3, scope_size, scope_leading)

    _draw(page, regular, f"Registered Capital: {en.get('capital') or ''}", right_x, y, size)
    _draw(page, regular, f"Date of Establishment: {en.get('established') or ''}", right_x, y + leading, size)
    right_y = y + leading * 2
    if en.get("term"):
        _draw(page, regular, f"Business Term: {en['term']}", right_x, right_y, size)
        right_y += leading
    _draw_lines(page, regular, addr_lines, right_x, right_y, size, leading)

    auth_y = layout["auth_y"]
    page_right = 803.4
    _draw(page, regular, "Registration Authority", 0, auth_y, size, align="right", right=page_right)
    _draw(page, regular, en.get("authority") or "", 0, auth_y + 20.0, size, align="right", right=page_right)
    _draw(page, regular, en.get("issue_date") or "", 0, auth_y + 40.0, size, align="right", right=page_right)

    footer_y = layout["footer_y"]
    _draw(
        page,
        regular,
        "Website of the National Enterprise Credit Information Publicity System: http://www.gsxt.gov.cn",
        38.4,
        footer_y,
        6.5,
    )
    _draw(
        page,
        regular,
        "Supervised by State Administration for Market Regulation",
        0,
        footer_y,
        6.5,
        align="right",
        right=page_right,
    )
    mid = (
        "The market entity shall submit its annual report through the National "
        "Enterprise Credit Information Publicity System from"
    )
    _draw(page, regular, mid, 38.4, footer_y + 13, 6.5)
    _draw(page, regular, "January 1 to June 30 each year.", 38.4, footer_y + 26, 6.5)

    t = trans
    y0 = layout["cert_y"]
    dy = y0 - DEFAULT_CERT_Y
    cert = (
        f"I, {t['name']}, translator of {t['organization']}, confirm this is a true "
        "and accurate translation of the original document."
    )
    _draw(page, regular, cert, 28.3, y0, 7.5)
    _draw(page, regular, f"CATTI Certificate No.: {t['catti_no']}", 28.3, 489.0 + dy, 7.5)
    _draw(page, regular, f"Organization: {t['organization']}", 196.3, 489.0 + dy, 7.5)
    _draw(page, regular, f"Tel: {t['tel']}  Email: {t['email']}", 28.3, 499.0 + dy, 7.5)
    _draw(page, regular, f"Organization Address: {t['address']}", 28.3, 509.0 + dy, 7.5)
    _draw(page, regular, "Signature:", 28.3, 518.0 + dy, 7.5)
    _draw(page, regular, f"Date of Translation: {date_text}", 188.0, 518.0 + dy, 7.5)

    stamp = ASSETS / "stamp.png"
    if stamp.exists():
        page.insert_image(fitz.Rect(335.8, 472.0 + dy, 446.0, 585.4 + dy), filename=str(stamp))

    signature = ASSETS / "signature.png"
    if signature.exists():
        page.insert_image(fitz.Rect(64.7, 515.5 + dy, 160.0, 539.0 + dy), filename=str(signature))

    return page


def _fit_image_page(doc: fitz.Document, image_path: str | Path, oriented_bgr=None) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    if oriented_bgr is not None:
        import cv2

        ok, buf = cv2.imencode(".jpg", oriented_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("Failed to encode original page")
        rect = _contain(PAGE_W, PAGE_H, oriented_bgr.shape[1], oriented_bgr.shape[0], margin=18)
        page.insert_image(rect, stream=buf.tobytes())
        return
    with Image.open(image_path) as im:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im) or im
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        rect = _contain(PAGE_W, PAGE_H, im.width, im.height, margin=18)
        page.insert_image(rect, stream=buf.getvalue())


def _contain(pw: float, ph: float, iw: float, ih: float, margin: float = 18) -> fitz.Rect:
    box_w, box_h = pw - margin * 2, ph - margin * 2
    scale = min(box_w / iw, box_h / ih)
    w, h = iw * scale, ih * scale
    x = (pw - w) / 2
    y = (ph - h) / 2
    return fitz.Rect(x, y, x + w, y + h)


def render_pdf(
    en: dict,
    config: dict,
    output_path: str | Path,
    original_image: str | Path | None = None,
    oriented_bgr=None,
    qr_bytes: bytes | None = None,
    translation_date: date | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    render_english_page(doc, en, config, qr_bytes=qr_bytes, translation_date=translation_date)
    if original_image is not None or oriented_bgr is not None:
        _fit_image_page(doc, original_image or "", oriented_bgr=oriented_bgr)
    doc.save(output_path, deflate=True, garbage=4)
    doc.close()
    return output_path
