"""Chinese national ID (居民身份证) front/back detection and field parse."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from .ocr import OcrLine

FRONT_MARKERS = ("公民身份号码", "公民身份证号")
BACK_MARKERS = ("居民身份证", "签发机关", "有效期限")
_ALREADY_RE = re.compile(r"[-_](正面|反面)$")
_ID18_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_NAME_RE = re.compile(
    r"姓名[:：\s]*([一-龥·]{2,15}?)(?:性别|民族|出生|住址|公民|$)"
)
_BIRTH_RE = re.compile(
    r"出生[:：\s]*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_EXP_RE = re.compile(
    r"(?:有效期限|有效期)[:：\s]*"
    r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?"
    r"\s*[-–—~至]+\s*"
    r"(长期|长期有效|(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?)"
)
_RANGE_RE = re.compile(
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*[-–—~至]+\s*"
    r"(长期|(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2}))"
)
_AUTH_RE = re.compile(r"签发机关[:：\s]*([一-龥A-Za-z0-9]{4,30}?)(?:有效期限|有效期|$)")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_MUNICIPALITIES = ("北京市", "天津市", "上海市", "重庆市")
_HINT_RE = re.compile(
    r"姓名|性别|民族|出生|住址|公民身份|签发机关|有效期|居民身份证|中华人民共和国"
)
# quick_detect downscales to ~1000–1600px; close-up ID text is tens of px,
# handheld ID in a selfie is typically <12px.
HANDHELD_FONT_PX = 12.0


@dataclass(frozen=True)
class IdQuality:
    score: float
    font_px: float
    hints: int
    lines: int
    parsed: int

    @property
    def handheld(self) -> bool:
        return self.font_px > 0 and self.font_px < HANDHELD_FONT_PX


def _ocr_boxes(lines: list[OcrLine] | list[str]) -> list[OcrLine]:
    return [ln for ln in (lines or []) if isinstance(ln, OcrLine)]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def score_id_photo(lines: list[OcrLine] | list[str]) -> IdQuality:
    """Quality score for an ID photo using OCR box height as font size.

    Close-up shots have large glyph boxes on the downscaled (~1600px) canvas.
    Handheld / selfie shots keep the card small, so median text height is low.
    """
    boxes = _ocr_boxes(lines)
    all_h: list[float] = []
    hint_h: list[float] = []
    hints = 0
    for ln in boxes:
        h = max(0.0, ln.y1 - ln.y0)
        w = max(0.0, ln.x1 - ln.x0)
        if h < 2 or w < 2:
            continue
        if w >= h * 0.6:
            all_h.append(h)
        compact = (ln.text or "").replace(" ", "").replace("　", "")
        if _HINT_RE.search(compact) or _ID18_RE.search(compact):
            hints += 1
            hint_h.append(h)
    font_px = _median(hint_h) if hint_h else _median(all_h)
    parsed = parse_id_fields(lines)
    parsed_n = sum(
        1 for key in ("name", "id_no", "birth", "id_exp", "id_long_term", "authority")
        if parsed.get(key)
    )
    score = font_px * 3.0 + hints * 10.0 + parsed_n * 15.0 + min(len(boxes), 24)
    if font_px >= HANDHELD_FONT_PX:
        score += 20.0
    return IdQuality(
        score=score, font_px=font_px, hints=hints, lines=len(boxes), parsed=parsed_n,
    )


def pick_best_ids(
    candidates: list[tuple[Path, list[OcrLine] | list[str], str]],
) -> dict[str, tuple[Path, list, IdQuality]]:
    """Keep the highest-scoring photo per side. Skip already-renamed copies."""
    best: dict[str, tuple[Path, list, IdQuality]] = {}
    for path, lines, side in candidates:
        if side not in ("正面", "反面"):
            continue
        if already_id_copy(path) and not path.stem.startswith(("id-", "id_")):
            continue
        quality = score_id_photo(lines)
        current = best.get(side)
        if current is None or quality.score > current[2].score:
            best[side] = (path, lines, quality)
    return best


def _blob(lines: list[OcrLine] | list[str]) -> str:
    parts: list[str] = []
    for item in lines or []:
        text = item.text if isinstance(item, OcrLine) else str(item or "")
        parts.append(text.replace(" ", "").replace("　", ""))
    return "".join(parts)


def classify_id_side(lines: list[OcrLine] | list[str]) -> str | None:
    """Return '正面' / '反面' / None.

    Front of a PRC ID always prints 公民身份号码. The back title is
    居民身份证 (and 签发机关 / 有效期限). Front also contains 居民身份证
    in the header, so the ID-number marker wins.
    """
    blob = _blob(lines)
    if not blob:
        return None
    if any(m in blob for m in FRONT_MARKERS) or _ID18_RE.search(blob):
        if "营业执照" in blob and "统一社会" in blob:
            return None
        return "正面"
    if any(m in blob for m in BACK_MARKERS) and "营业执照" not in blob:
        return "反面"
    return None


def named_id_side(path: Path) -> str | None:
    """Side implied by an already-renamed file stem (`*-正面` / `*-反面`)."""
    stem = Path(path).stem
    if stem.endswith("-正面") or stem.endswith("_正面"):
        return "正面"
    if stem.endswith("-反面") or stem.endswith("_反面"):
        return "反面"
    return None


def already_id_copy(path: Path) -> bool:
    return bool(_ALREADY_RE.search(path.stem))


def copy_id_named(src: Path, side: str) -> Path | None:
    """Copy `src` to `{stem}-正面|反面{suffix}` next to it. Skip if already named."""
    src = Path(src)
    if not src.is_file() or side not in ("正面", "反面"):
        return None
    if already_id_copy(src) and src.stem.endswith(side):
        return src
    dest = src.with_name(f"{src.stem}-{side}{src.suffix}")
    if dest.exists():
        return dest
    try:
        shutil.copy2(src, dest)
    except OSError:
        return None
    return dest


def copy_id_canonical(src: Path, side: str) -> Path | None:
    """Copy the winning photo to `id-正面|反面` and drop leftover role copies."""
    src = Path(src)
    if not src.is_file() or side not in ("正面", "反面"):
        return None
    dest = src.with_name(f"id-{side}{src.suffix}")
    if src.resolve() != dest.resolve():
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None
    _purge_stale_id_copies(src.parent, side, dest)
    return dest


def _purge_stale_id_copies(folder: Path, side: str, keep: Path) -> None:
    keep_res = keep.resolve()
    for item in folder.iterdir():
        if not item.is_file() or named_id_side(item) != side:
            continue
        if not already_id_copy(item):
            continue
        if item.resolve() == keep_res:
            continue
        try:
            item.unlink()
        except OSError:
            pass


def id18_checksum(body17: str) -> str:
    total = sum(int(d) * w for d, w in zip(body17, _ID_WEIGHTS))
    return _ID_CHECK[total % 11]


def valid_id18(value: str) -> bool:
    s = (value or "").strip().upper()
    if not re.fullmatch(r"\d{17}[\dX]", s):
        return False
    return s[-1] == id18_checksum(s[:17])


def birth_from_id18(value: str) -> str:
    """YYYY-MM-DD from a valid 18-digit ID, else ''."""
    s = (value or "").strip().upper()
    if not valid_id18(s):
        return ""
    y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def _iso_date(y, m, d) -> str:
    try:
        yi, mi, di = int(y), int(m), int(d)
    except (TypeError, ValueError):
        return ""
    if not (1900 <= yi <= 2100 and 1 <= mi <= 12 and 1 <= di <= 31):
        return ""
    return f"{yi:04d}-{mi:02d}-{di:02d}"


def parse_id_fields(lines: list[OcrLine] | list[str]) -> dict:
    """Pull name / id_no / birth / expiry from one side's OCR lines.

    Missing keys are omitted. `id_no` is stored only when the checksum
    passes. Birth prefers the ID number over the printed 出生 line.
    """
    blob = _blob(lines)
    out: dict = {}
    if not blob:
        return out

    m = _NAME_RE.search(blob)
    if m:
        out["name"] = m.group(1)

    id_no = ""
    labeled = re.search(r"(?:公民身份号码|公民身份证号)[:：\s]*(\d{17}[\dXx])", blob)
    if labeled:
        id_no = labeled.group(1).upper()
    else:
        found = _ID18_RE.search(blob)
        if found:
            id_no = found.group(1).upper()
    if id_no and valid_id18(id_no):
        out["id_no"] = id_no
        born = birth_from_id18(id_no)
        if born:
            out["birth"] = born

    if "birth" not in out:
        bm = _BIRTH_RE.search(blob)
        if bm:
            iso = _iso_date(*bm.groups())
            if iso:
                out["birth"] = iso

    em = _EXP_RE.search(blob)
    if not em and ("签发机关" in blob or "有效期限" in blob or "有效期" in blob):
        em = _RANGE_RE.search(blob)
    if em:
        tail = em.group(4) or ""
        if tail.startswith("长期"):
            out["id_long_term"] = True
            out["id_exp"] = ""
        else:
            iso = _iso_date(em.group(5), em.group(6), em.group(7))
            if iso:
                out["id_exp"] = iso
                out["id_long_term"] = False

    am = _AUTH_RE.search(blob)
    if am:
        out["authority"] = am.group(1)

    return out


def merge_id_fields(base: dict | None, extra: dict | None) -> dict:
    """Fill empty keys from another side; OR `id_long_term`."""
    out = dict(base or {})
    for key, value in (extra or {}).items():
        if key == "id_long_term":
            out[key] = bool(out.get(key) or value)
            if out[key]:
                out["id_exp"] = ""
            continue
        if value and not out.get(key):
            out[key] = value
    return out


def cn_admin_parts(address: str) -> tuple[str, str]:
    """(province, city) from a Chinese registered address. Empty on miss."""
    text = re.sub(r"\s+", "", address or "")
    if not text:
        return "", ""
    from .divisions import consume_admin

    admin, _rest = consume_admin(text)
    province = admin[0] if admin else ""
    city = admin[1] if len(admin) > 1 else ""
    if province in _MUNICIPALITIES and not city:
        city = province
    return province, city
