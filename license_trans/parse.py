from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .ocr import OcrLine
from .divisions import places_from_usci, snap_text
from .usci import checksum_ok, repair_usci

OCR_FIXES = (
    ("衣副产品", "农副产品"),
    ("农副产品销售：", "农副产品销售、"),
    ("日用百货销售：", "日用百货销售、"),
    ("食品生产：", "食品生产、"),
    ("食品销售：", "食品销售、"),
    ("一股项目", "一般项目"),
    ("一股项日", "一般项目"),
    ("项日", "项目"),
    ("金质材料", "金属材料"),
    ("闵门", "阀门"),
    ("定客销售", "旋塞销售"),
    ("转承销售", "轴承销售"),
    ("租货", "租赁"),
    ("国林绿化", "园林绿化"),
    ("施上", "施工"),
    ("装即搬运", "装卸搬运"),
    ("碎眉", "碎屑"),
    ("非金所矿", "非金属矿"),
    ("别品销售", "制品销售"),
    ("不合诈可类", "不含许可类"),
    ("机核零件", "机械零件"),
    ("水环境污案", "水环境污染"),
    ("土污染治理", "土壤污染治理"),
    ("配电开S", "配电开关"),
    ("号般项目", "一般项目"),
    ("目用百货", "日用百货"),
    ("技术推产", "技术推广"),
    ("依法直主", "依法自主"),
    ("不食劳务派遣", "不含劳务派遣"),
    ("钟表销售自行车", "钟表销售、自行车"),
    ("工程施工日用", "工程施工、日用"),
    ("一胶项目", "一般项目"),
    ("展责展示", "展览展示"),
    ("服务物进出口", "服务、货物进出口"),
    ("金属门窗工程连", "金属门窗工程"),
    ("技术推广国内", "技术推广、国内"),
    ("主册", "注册"),
    ("机械影件", "机械零件"),
    ("零服件", "零部件"),
    ("销像", "销售"),
    ("颜经批准", "经批准"),
    ("免营业护鱼", "凭营业执照"),
    ("免营业执照", "凭营业执照"),
    ("食用衣产品", "食用农产品"),
    ("伍洽", "伍拾"),
    ("依洁", "依法"),
    ("项具", "项目"),
    ("螺旋奖", "螺旋桨"),
    ("闪门", "阀门"),
    ("闪林绿化", "园林绿化"),
    ("装部搬运", "装卸搬运"),
    ("机械设务", "机械设备"),
    ("化牧品", "化妆品"),
    ("机板设备", "机械设备"),
    ("非金质矿", "非金属矿"),
    ("诈可类", "许可类"),
    ("电子元器作", "电子元器件"),
    ("医疗器销售", "医疗器械销售"),
    ("鞋销批发", "鞋批发"),
)

NOISE_RE = re.compile(
    r"(SCJDGL|SGJDGL|SIDGL|SODGL|SOJDGL|SUJDGL|SCJDGI|SCJDG|JDGL|OGL|DGL|SJD)",
    re.I,
)

# Annual-report footer notice (市场主体应当于每年1月1日至6月30日通过…公示).
# '市场主体' alone is NOT a footer signal — the scope body itself contains
# '…市场主体自主选择经营。一般项目：…' (六盘水), and killing that row
# drops the 一般项目 marker and flips scope into the raw-blob fallback.
FOOTER_RE = re.compile(r"(公示|年度报告|报送|每年|gsxt|网址|监制|市场主体应当)")

# Seal-covered fragments of the registration-authority name (e.g. "场监务"
# of 市场监督管理局, "行政" of 江阴市行政审批局). Short runs built purely from
# authority-related characters never belong in scope or address values.
SEAL_FRAG_RE = re.compile(r"[市场监督管理务局印章政审登批之山区]{2,6}")

CREDIT_RE = re.compile(
    r"([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})(?:\s*[\(（]\s*(\d+\s*-\s*\d+)\s*[\)）])?"
)
# Boundary-guarded variant: the USCI must not sit inside a longer
# alphanumeric run. Vertical licenses print a 21-digit license number
# ("320281666202409060333") whose 18-char windows otherwise look like a
# USCI, and "SCJDGL" watermarks glue onto it in the joined blob.
CREDIT_GUARDED_RE = re.compile(
    r"(?<![0-9A-Z])"
    r"([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})"
    r"(?:\s*[\(（]\s*(\d+\s*-\s*\d+)\s*[\)）])?"
    r"(?![0-9A-Z])"
)
DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
COPY_RE = re.compile(r"[\(（]\s*(\d+\s*-\s*\d+)\s*[\)）]")

KEYS = [
    ("credit_label", ("统一社会信用代码", "社会信用代码", "信用代码")),
    ("name", ("名称", "名 称")),
    ("type", ("类型", "类 型")),
    ("legal_person", ("法定代表人",)),
    ("scope", ("经营范围", "经 营 范 围")),
    ("capital", ("注册资本", "注册资金", "实收资本")),
    ("established", ("成立日期", "注册日期")),
    ("address", ("住所", "经营场所", "营业场所")),
    ("term", ("营业期限", "经营期限")),
    ("authority", ("登记机关",)),
    ("issue_date", ("发证日期", "核准日期", "发照日期")),
]

REG_NO_RE = re.compile(r"(?<!\d)(\d{15})(?!\d)")


def _fix_ocr_text(text: str) -> str:
    for src, dst in OCR_FIXES:
        text = text.replace(src, dst)
    text = re.sub(r"(\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)销", r"\1铺", text)
    text = re.sub(r"销\d+号售", "销售", text)
    text = re.sub(r"号般项目", "一般项目", text)
    text = re.sub(r"机\d+号械", "机械", text)
    # A comma right after 一般项目/许可项目 is a misread of the colon.
    text = re.sub(r"(一般项目|许可项目|许可经营项目|一般经营项目)，", r"\1：", text)
    # Trailing-工/售 truncations at line boundaries.
    text = re.sub(r"工程施(?![工])", "工程施工", text)
    text = re.sub(r"设备销(?![售])", "设备销售", text)
    # "日" misread as 6C/6G with a watermark glued on (杭州: 2024年08月236CJDGL).
    text = re.sub(
        r"(20\d{2}年\d{1,2}月\d{1,2})日?[6G]?C(?:SCJDGL|SCJDGI|SJDGL|CJDGL|SCJD|CJDG|JDGL|DGL|SJD|OGL)(?![A-Za-z0-9])",
        r"\1日",
        text,
    )
    # "日" dropped entirely at line end (吉林: 2024年06月27).
    text = re.sub(r"(20\d{2}年\d{1,2}月\d{1,2})$", r"\1日", text)
    return text


def _is_noise(line: OcrLine) -> bool:
    compact = _norm(line.text)
    if NOISE_RE.fullmatch(compact) or NOISE_RE.fullmatch(line.text.strip()):
        return True
    if compact in {"市场监督", "市场监督管理"} and (line.x1 - line.x0) > 150:
        return True
    if "监制" in compact:
        return True
    if compact.startswith("http") or "gsxt" in compact.lower():
        return True
    if compact in {"营业执照", "电子营业执照", "国家市场监督管理总局监制"}:
        return True
    return False


def _norm(text: str) -> str:
    return (
        text.replace(" ", "")
        .replace("　", "")
        .replace("：", ":")
        .replace("（", "(")
        .replace("）", ")")
        .strip()
    )


def _clean_value(text: str) -> str:
    text = text.strip(" :：;；|-_")
    text = re.sub(r"^(名称|类型|法定代表人|经营范围|注册资本|注册资金|成立日期|住所|营业期限|经营期限|登记机关)[:：]?", "", text)
    text = re.sub(r"^称", "", text)
    # 类型 label that lost its leading char (vertical OCR) leaves 型.
    text = re.sub(r"^型(?=[\u4e00-\u9fff])", "", text)
    return text.strip()


def clip_scope_text(text: str) -> str:
    """Drop name/type/address glued in front of 一般项目 / 许可项目."""
    compact = (text or "").replace(" ", "")
    compact = _fix_ocr_text(compact)
    compact = re.sub(r"^[0-9A-HJ-NPQRTUWXY]{15,18}", "", compact)
    compact = re.sub(r"^.*?有限责任公司（自然人[^）]{0,24}）", "", compact)
    compact = re.sub(r"^.*?有限责任公司\(自然人[^)]{0,24}\)", "", compact)
    compact = re.sub(r"^(20\d{2}年\d{1,2}月\d{1,2}日至20\d{2}年\d{1,2}月\d{1,2}日)+", "", compact)
    markers = ("许可经营项目", "许可项目", "一般经营项目", "一般项目")
    hits = [compact.find(m) for m in markers if m in compact]
    if hits:
        compact = compact[min(hits) :]
    for stop in ("登记机关", "注册资本", "注册资金", "营业期限", "经营期限",
                 "成立日期", "注册日期", "住所", "住（所", "住(所",
                 "副本", "(副 本)", "(副本)", "（副本）"):
        idx = compact.find(stop)
        if idx > 24:
            compact = compact[:idx]
            break
    compact = re.sub(r"副本+$", "", compact)
    compact = re.sub(r"\(?\s*副\s*本\s*\)?$", "", compact)
    # Annual-report footer notice glued after the scope's legal-tail
    # disclaimer. Its text is OCR-corrupted beyond keyword rescue
    # (市场主体应当于每年… → 市场主体座尚于每平…, 宝鸡), so match the
    # structure: the last 依法 disclaimer paren is the end of every scope
    # section — only a new section marker may follow it.
    tails = list(re.finditer(r"（[^（）]*依法[^（）]*）", compact))
    if tails:
        after = compact[tails[-1].end():]
        if after and not re.match(r"^[。；;]?[：:]?(许可项目|一般项目|许可经营项目|一般经营项目)", after):
            compact = compact[:tails[-1].end()]
    # Photo watermark timestamps and trailing digit runs (32024/11/0821:02).
    compact = re.sub(r"\d?\d20\d{2}[./-]\d{1,2}[./-]\d{1,2}\d{0,2}[:：]\d{2}[)）]?$", "", compact)
    compact = re.sub(r"\d{6,}[)）]?$", "", compact)
    # Watermark tokens glued mid-text (…或许可证件为准）SCJDGL关部门…).
    compact = re.sub(r"(?<![A-Za-z0-9])(?:SCJDGL|SGJDGL|SJDGL|CJDGL|SCJDGI|SCJDG|SIDGL|SODGL|SOJDGL|SUJDGL|SCJD|CJDG|JDGL|SJD|OGL|DGL)(?![A-Za-z0-9])", "", compact)
    compact = re.sub(r"(?:SCJDGL|SJDGL|CJDG|JDGL)+[（(]?$", "", compact)
    compact = re.sub(r"[（(]+$", "", compact)
    return compact.strip(" :：;；,，、")


def _label_forms(labels: tuple[str, ...]) -> list[str]:
    """Normalized label forms for matching, longest first.

    Includes a first-char-dropped variant for labels of 3+ chars: vertical
    license labels frequently lose their leading character to OCR
    (营业期限 → 业期限, 名称 → 称). Two-char labels get no variant — a
    one-char remainder matches far too many ordinary lines.
    """
    exact = sorted({_norm(l) for l in labels if _norm(l)}, key=len, reverse=True)
    return exact + [f[1:] for f in exact if len(f) >= 3]


def _find_key_line(lines: list[OcrLine], labels: tuple[str, ...]) -> OcrLine | None:
    exact = sorted({_norm(l) for l in labels if _norm(l)}, key=len, reverse=True)
    dropped = [f[1:] for f in exact if len(f) >= 3]
    for forms in (exact, dropped):
        for line in lines:
            compact = _norm(line.text)
            for label in forms:
                if compact.startswith(label) or label == compact:
                    return line
        for line in lines:
            compact = _norm(line.text)
            for label in forms:
                if label in compact[:12]:
                    return line
    return None


def _value_after_key(line: OcrLine, labels: tuple[str, ...]) -> str:
    raw = re.sub(r"\s+", "", line.text)
    for form in _label_forms(labels):
        idx = raw.find(form)
        if idx >= 0:
            leftover = raw[idx + len(form):].lstrip(":：")
            if leftover:
                return leftover
    parts = re.split(r"[:：]", line.text, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return ""


def _detect_layout(lines: list[OcrLine]) -> dict:
    """Auto-detect line height, sort bucket, and layout mode from OCR lines."""
    if not lines:
        return {"line_h": 18.0, "bucket": 18, "max_dy_scope": 220.0, "max_dy_field": 90.0,
                "stop_fallback": 400.0, "rotated": False}
    heights = [ln.y1 - ln.y0 for ln in lines if ln.y1 > ln.y0]
    median_h = sorted(heights)[len(heights) // 2] if heights else 18.0
    median_h = max(10.0, median_h)
    widths = [ln.x1 - ln.x0 for ln in lines if ln.x1 > ln.x0]
    median_w = sorted(widths)[len(widths) // 2] if widths else 0.0
    rotated = median_h > 2 * max(median_w, 1.0)
    bucket = max(8, int(median_h * 1.0))
    max_dy_scope = median_h * 12
    max_dy_field = median_h * 5
    stop_fallback = median_h * 22
    return {
        "line_h": median_h,
        "bucket": bucket,
        "max_dy_scope": max_dy_scope,
        "max_dy_field": max_dy_field,
        "stop_fallback": stop_fallback,
        "rotated": rotated,
    }


def _reading_order(chosen: list[OcrLine], bucket: int) -> list[OcrLine]:
    """Order value lines by visual row: lines whose y0 sits within half a row
    pitch of the row's first line share one row (sorted left-to-right inside
    the row), rows run top-to-bottom. Sorting by (round(y0/bucket), x0) lets
    two adjacent value rows fall into one bucket, and x0 then flips their
    reading order."""
    tol = max(6.0, 0.5 * bucket)
    rows: list[list[OcrLine]] = []
    for ln in sorted(chosen, key=lambda l: l.y0):
        if rows and ln.y0 - rows[-1][0].y0 <= tol:
            rows[-1].append(ln)
        else:
            rows.append([ln])
    ordered: list[OcrLine] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda l: l.x0))
    return ordered


def _drop_duplicate_lines(chosen: list[OcrLine]) -> list[OcrLine]:
    """Drop double-detected lines: overlapping boxes whose texts contain
    each other (RapidOCR sometimes emits the same row twice, once glued with
    adjacent watermark noise). Punctuation is stripped before the
    containment test — the glued copy often reads ： where the fragment
    read ， (吉林: '网络技术服务，' fragment vs '…网络技术服务：…' row)."""
    if len(chosen) < 2:
        return chosen

    def _sig(t: str) -> str:
        return re.sub(r"[，,、;；:：.。()（）\s]", "", t)

    kept: list[OcrLine] = []
    for ln in chosen:
        dup = False
        for other in kept:
            y_ov = min(ln.y1, other.y1) - max(ln.y0, other.y0)
            x_ov = min(ln.x1, other.x1) - max(ln.x0, other.x0)
            small_h = min(ln.y1 - ln.y0, other.y1 - other.y0)
            small_w = min(ln.x1 - ln.x0, other.x1 - other.x0)
            if small_h > 0 and small_w > 0 and y_ov >= 0.7 * small_h and x_ov >= 0.6 * small_w:
                a, b = _sig(ln.text), _sig(other.text)
                if a == b or (a and a in b) or (b and b in a):
                    dup = True
                    break
        if not dup:
            kept.append(ln)
    return kept


def _lines_right_or_below(lines: list[OcrLine], key: OcrLine, stop: list[OcrLine],
                           max_dy: float = 80, bucket: int = 12) -> list[OcrLine]:
    stop_y = min((s.y0 for s in stop if s.y0 > key.y0 + 8), default=key.y0 + 400)
    height = max(12.0, key.y1 - key.y0)
    chosen: list[OcrLine] = []
    for line in lines:
        if line is key:
            continue
        if line.y0 >= stop_y - 4:
            continue
        same_row = abs(line.cy - key.cy) < height * 1.2 and line.x0 >= key.x1 - max(12.0, 0.6 * height) \
            and line.y0 >= key.y0 - 0.3 * height
        below = key.y0 - 4 < line.y0 < stop_y and line.x0 >= key.x0 - 20 and line.cx > key.cx
        if same_row or below:
            if line.y0 <= key.y0 + max_dy or same_row:
                chosen.append(line)
    return _drop_duplicate_lines(_reading_order(chosen, bucket))


def _column_filter(nearby: list[OcrLine], key: OcrLine) -> list[OcrLine]:
    """Drop value lines that belong to a different field column.

    Old-format licenses place 住所 (right column) at the same height as the
    经营范围 label (left column), so the address lines pass the
    row/below tests and pollute the scope text. Seed the scope value box
    from the line nearest the label, then keep only lines starting within
    (or just beyond) that box's horizontal extent.
    """
    if len(nearby) < 2:
        return nearby
    seed = min(nearby, key=lambda ln: (max(0.0, ln.x0 - key.x1), abs(ln.cy - key.cy)))
    cluster = [ln for ln in nearby if seed.x0 - 80 <= ln.x0 <= seed.x0 + 150]
    if not cluster:
        return nearby
    col_right = max(ln.x1 for ln in cluster)
    limit = col_right + max(60.0, 0.12 * max(0.0, col_right - seed.x0))
    return [ln for ln in nearby if ln.x0 <= limit]


def _address_with_continuation(lines: list[OcrLine], base: OcrLine) -> str:
    """Base address line plus its same-column continuation lines.

    Handheld/old-format licenses wrap the address over 2-3 lines inside the
    same field box; the continuation rows start near the base row's left
    edge. Authority labels, seal text and footer lines sit further down or
    in other columns and are rejected.
    """
    h = max(12.0, base.y1 - base.y0)
    # Key labels in this page: a continuation candidate that sits right of
    # a row-aligned label within a small gap is that label's value (old-format
    # licenses stack 法定代表人/注册资本/成立日期 below 住所 in the same
    # value column), never an address continuation (北京's address picked up
    # 王生武40万元2014年03月28日 this way).
    label_lines: list[OcrLine] = []
    for l in lines:
        compact = _norm(l.text)
        if any(compact == _norm(lab) or compact.startswith(_norm(lab))
               for _, labs in KEYS for lab in labs if _norm(lab)):
            label_lines.append(l)
    parts = [base.text]
    for ln in sorted(lines, key=lambda l: (l.y0, l.x0)):
        if ln is base:
            continue
        if ln.y0 <= base.y0 or ln.y0 > base.y0 + 4.0 * h:
            continue
        if not (base.x0 - 100 <= ln.x0 <= base.x0 + 150):
            continue
        guarded = False
        for lab in label_lines:
            y_ov = min(ln.y1, lab.y1) - max(ln.y0, lab.y0)
            min_h = max(1.0, min(ln.y1 - ln.y0, lab.y1 - lab.y0))
            if y_ov >= 0.5 * min_h and lab.x1 <= ln.x0 \
               and ln.x0 - lab.x1 <= max(60.0, 1.5 * h):
                guarded = True
                break
        if guarded:
            continue
        compact = _norm(ln.text)
        if any(_norm(lab) == compact for _, labs in KEYS for lab in labs):
            continue
        if re.search(r"(公示|监督|监制|经营|营业执照)", compact):
            continue
        if SEAL_FRAG_RE.fullmatch(compact):
            continue
        parts.append(ln.text)
    return "".join(parts)


def _join(parts: list[str]) -> str:
    out: list[str] = []
    for part in parts:
        part = _clean_value(part)
        if not part:
            continue
        if out and out[-1].endswith(("，", ",", "、", ";", "；")):
            out.append(part)
        elif out and part.startswith(("，", ",", "、")):
            out[-1] += part
        else:
            out.append(part)
    return "".join(out) if any("许可" in p or "一般" in p or "项目" in p for p in out) else " ".join(out) if False else "".join(out)


@dataclass
class LicenseFields:
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

    def to_dict(self) -> dict:
        return asdict(self)


def parse_license(lines: list[OcrLine]) -> LicenseFields:
    fields = LicenseFields()
    for line in lines:
        line.text = _fix_ocr_text(line.text)
    raw_blob = "".join(line.text for line in lines)
    if "副本" in raw_blob:
        fields.duplicate = True
    elif "正本" in raw_blob:
        fields.duplicate = False
    lines = [line for line in lines if not _is_noise(line)]
    blob = "".join(line.text for line in lines)

    compact_blob = blob.replace(" ", "")
    credit_matches = list(CREDIT_GUARDED_RE.finditer(compact_blob))
    if not credit_matches:
        credit_matches = list(CREDIT_RE.finditer(compact_blob))

    def _credit_rank(m) -> tuple[int, int]:
        code = m.group(1)
        if checksum_ok(code):
            return (0, 0)
        if checksum_ok(repair_usci(code)):
            return (0, 1)
        if code.startswith("91"):
            return (1, 0)
        return (2, 0)

    if credit_matches:
        best = min(credit_matches, key=_credit_rank)
        fields.credit_code = repair_usci(best.group(1))
        if not checksum_ok(fields.credit_code):
            fields.warnings.append("credit_code_checksum")
        if best.group(2):
            fields.copy_no = re.sub(r"\s+", "", best.group(2))
    else:
        reg_no = REG_NO_RE.search(blob.replace(" ", ""))
        if reg_no:
            fields.warnings.append("old_format_license")
        fields.warnings.append("credit_code")

    copy = COPY_RE.search(blob)
    if copy:
        fields.copy_no = re.sub(r"\s+", "", copy.group(1))

    parsed = [(int(y), int(m), int(d)) for y, m, d in DATE_RE.findall(blob)]
    if parsed:
        parsed.sort()
        fields.established = f"{parsed[0][0]:04d}年{parsed[0][1]:02d}月{parsed[0][2]:02d}日"
        later = [item for item in parsed if item != parsed[0]]
        if later:
            fields.issue_date = f"{later[-1][0]:04d}年{later[-1][1]:02d}月{later[-1][2]:02d}日"

    key_lines: dict[str, OcrLine] = {}
    for name, labels in KEYS:
        found = _find_key_line(lines, labels)
        if found:
            key_lines[name] = found

    ordered_keys = [key_lines[name] for name, _ in KEYS if name in key_lines]

    layout = _detect_layout(lines)
    field_scores: dict[str, float] = {}

    def grab(name: str, labels: tuple[str, ...], extra_stop: tuple[str, ...] = ()) -> str:
        key = key_lines.get(name)
        if not key:
            fields.warnings.append(name)
            return ""
        inline = _value_after_key(key, labels)
        stops_raw: list[OcrLine] = []
        # Column-aware stops: a label living outside the key's column band
        # (old-format two-column licenses stack the other field's label in
        # the far-left or far-right column, often well below this key's row)
        # cannot bound this key's value box — the value column legitimately
        # runs past it (田洪's scope extends below the right-column
        # 登记机关; IMG_6336's 住所 value continues below the left-column
        # 法定代表人). extra_stop labels get the same band test: 俊煜's
        # right-column 登记机关 label (y741) otherwise truncates a scope
        # that still has six rows left in the left value column.
        key_h = max(12.0, key.y1 - key.y0)
        col_tol = max(150.0, 1.5 * (key.x1 - key.x0))
        for other in extra_stop:
            s = key_lines.get(other)
            if s is None:
                continue
            if s.x0 > key.x1 + col_tol or s.x1 < key.x0 - col_tol:
                continue
            stops_raw.append(s)
        stops = stops_raw
        for k in ordered_keys:
            if k is key or k.y0 <= key.y0:
                continue
            if k.x0 > key.x1 + col_tol or k.x1 < key.x0 - col_tol:
                continue
            stops.append(k)
        max_dy = layout["max_dy_scope"] if name == "scope" else layout["max_dy_field"]
        if name == "scope" and not stops:
            # No in-column stop label survived the band test: the value box
            # runs until the page footer, well past max_dy_scope (12 rows).
            max_dy = layout["stop_fallback"]
        nearby = _lines_right_or_below(lines, key, stops, max_dy=max_dy, bucket=layout["bucket"])
        if name in {"scope", "address"}:
            # Vertical seal fragments (华控's 智管理, h=219 vs ~45px rows) sit
            # in the value column below the field and pass every positional
            # test; their box height betrays them.
            tall = 2.5 * max(1.0, layout["line_h"])
            nearby = [ln for ln in nearby if (ln.y1 - ln.y0) <= tall]
        if name in {"scope", "address"} and nearby and not layout.get("rotated"):
            nearby = _column_filter(nearby, key)
        if name not in {"scope", "address"}:
            # Short-value fields: rank lines that are row-aligned with the
            # label first, then by horizontal gap. The bucket index
            # (round(y0/bucket)) mis-orders a far-away line that happens to
            # fall a few px above the true value line, and a same-row line
            # from a neighbouring field's box also competes — row alignment
            # + proximity resolves both.
            key_h = max(12.0, key.y1 - key.y0)

            def _rank(ln: OcrLine):
                same_row = abs(ln.y0 - key.y0) < 0.6 * key_h
                return (0 if same_row else 1, max(0.0, ln.x0 - key.x1), abs(ln.y0 - key.y0))

            nearby.sort(key=_rank)
            if layout.get("rotated"):
                # Vertical (rotated) licenses: value lines share the label's
                # column instead of sitting to its right.
                seen = {id(ln) for ln in nearby}
                kw = key.x1 - key.x0
                for ln in lines:
                    if ln is key or id(ln) in seen:
                        continue
                    overlap = min(ln.x1, key.x1) - max(ln.x0, key.x0)
                    if overlap >= 0.5 * max(1.0, min(ln.x1 - ln.x0, kw)):
                        nearby.append(ln)
                nearby.sort(key=lambda ln: (abs(ln.y0 - key.y0), ln.x0))
        parts = [inline] + [item.text for item in nearby]
        contributing = [key] + nearby
        if contributing:
            known = [ln.score for ln in contributing if ln.score is not None]
            if known:
                field_scores[name] = min(known)
        filtered: list[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            compact = _norm(part)
            if any(_norm(lab) == compact for _, labs in KEYS for lab in labs):
                continue
            # A key label glued to a watermark (开平: 'SCJDGL登记机关') reads
            # as a value row once the label leaves the stop set — strip the
            # watermark before the label test.
            if any(_norm(lab) == NOISE_RE.sub("", compact)
                   for _, labs in KEYS for lab in labs if NOISE_RE.search(compact)):
                continue
            if NOISE_RE.fullmatch(compact):
                continue
            if name in {"scope", "address"} and SEAL_FRAG_RE.fullmatch(compact):
                continue
            # Page-footer annual-report notice lines sit inside the widened
            # scope reach; they never belong to the value box.
            if name == "scope" and FOOTER_RE.search(compact):
                continue
            filtered.append(part)
        value = _clean_value("".join(filtered) if name in {"scope", "address"} else (filtered[0] if filtered else ""))
        if name in {"scope", "address"}:
            value = _clean_value("".join(_clean_value(p) for p in filtered))
        return value

    prefer = places_from_usci(fields.credit_code)
    fields.name = grab("name", ("名称", "名 称"), ("type", "legal_person"))
    fields.type = grab("type", ("类型", "类 型"), ("legal_person", "scope"))
    fields.legal_person = grab("legal_person", ("法定代表人",), ("scope", "capital"))
    fields.scope = grab("scope", ("经营范围",), ("capital", "authority", "term"))
    fields.capital = grab("capital", ("注册资本", "注册资金", "实收资本"), ("established", "address"))
    established = grab("established", ("成立日期", "注册日期"), ("address", "authority"))
    if DATE_RE.search(established or ""):
        m = DATE_RE.search(established)
        fields.established = f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    fields.address = snap_text(grab("address", ("住所", "经营场所", "营业场所"), ("authority",)), prefer)
    fields.term = grab("term", ("营业期限", "经营期限"), ("authority",))
    issue_from_label = grab("issue_date", ("发证日期", "核准日期", "发照日期"), ())
    if issue_from_label and DATE_RE.search(issue_from_label):
        m = DATE_RE.search(issue_from_label)
        fields.issue_date = f"{int(m.group(1)):04d}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
        if "issue_date" in fields.warnings:
            fields.warnings.remove("issue_date")
    else:
        term_dates = set(DATE_RE.findall(fields.term or ""))
        non_term = [d for d in parsed if (d[0], d[1], d[2]) not in {(int(y), int(m), int(da)) for y, m, da in term_dates}]
        if len(non_term) >= 2:
            non_term.sort()
            fields.established = f"{non_term[0][0]:04d}年{non_term[0][1]:02d}月{non_term[0][2]:02d}日"
            fields.issue_date = f"{non_term[-1][0]:04d}年{non_term[-1][1]:02d}月{non_term[-1][2]:02d}日"
        elif len(non_term) == 1:
            fields.issue_date = f"{non_term[0][0]:04d}年{non_term[0][1]:02d}月{non_term[0][2]:02d}日"
    fields.name = snap_text(fields.name, prefer, keep_len=True)
    authority = grab("authority", ("登记机关",))
    if authority and "局" in authority and "监制" not in authority:
        fields.authority = authority
    else:
        fields.authority = ""
        fields.warnings.append("authority")

    # Fallbacks when a label was seen but the value box was empty / noisy.
    type_value_re = re.compile(r"^(型)?(其他|自然人|国有|外商|私营|一人|股份)?(有限责任公司|股份有限公司|合伙企业)")
    if not fields.name:
        for line in lines:
            compact = _norm(line.text)
            if re.search(r"(有限责任公司|股份有限公司|有限公司|合伙企业)$", compact) \
               and "责任公司（" not in compact and "责任公司(" not in compact \
               and not type_value_re.match(compact):
                fields.name = snap_text(_clean_value(line.text), places_from_usci(fields.credit_code), keep_len=True)
                if "name" in fields.warnings:
                    fields.warnings.remove("name")
                break
    # Type fallback: if type label was missed, detect from pattern.
    # The type value typically looks like "有限责任公司（自然人投资或控股）"
    if not fields.type:
        for line in lines:
            compact = _norm(line.text)
            if re.search(r"^(型)?(有限责任公司|股份有限公司|一人有限责任公司)", compact) \
               and "法定代表人" not in compact and "名称" not in compact:
                fields.type = _clean_value(line.text)
                if "type" in fields.warnings:
                    fields.warnings.remove("type")
                break
    # Legal person validation: if it contains company-type text, it's the type not a person.
    if fields.legal_person and re.search(r"(有限责任公司|股份有限公司|个人独资|合伙企业)", _norm(fields.legal_person)):
        if not fields.type:
            fields.type = fields.legal_person
            if "type" in fields.warnings:
                fields.warnings.remove("type")
        fields.legal_person = ""
        # Try to recover the real legal person from nearby lines
        for line in lines:
            compact = _norm(line.text)
            # Person names are 2-4 Chinese characters, no company-type keywords
            if 2 <= len(compact) <= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", compact) \
               and not any(kw in compact for kw in ("公司", "企业", "执照", "登记", "监督",
                   "经营", "注册", "类型", "名称", "住所", "期限", "日期", "机关")):
                fields.legal_person = line.text
                if "legal_person" in fields.warnings:
                    fields.warnings.remove("legal_person")
                break
    # Legal person with date pinyin or credit code fragments — clear it
    if fields.legal_person:
        lp = fields.legal_person
        if re.match(r"^[0-9\s]+nian", lp) or re.match(r"^[0-9A-Z]{8,}", lp) \
           or (len(lp) > 10 and not re.search(r"[\u4e00-\u9fff]", lp)):
            fields.legal_person = ""
            # Try to recover from lines near the 法定代表人 label
            for line in lines:
                compact = _norm(line.text)
                if 2 <= len(compact) <= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", compact) \
                   and not any(kw in compact for kw in ("公司", "企业", "执照", "登记", "监督",
                       "经营", "注册", "类型", "名称", "住所", "期限", "日期", "机关")):
                    fields.legal_person = line.text
                    if "legal_person" in fields.warnings:
                        fields.warnings.remove("legal_person")
                    break
    # Type field with capital amount — move to capital
    if fields.type and re.search(r"(万元|yuan|Million|Thousand|Only)", fields.type, re.I):
        if not fields.capital:
            fields.capital = fields.type
            if "capital" in fields.warnings:
                fields.warnings.remove("capital")
        fields.type = ""
        if "type" not in fields.warnings:
            fields.warnings.append("type")
    if not fields.address:
        addr_line = None
        for line in lines:
            compact = _norm(line.text)
            if re.search(r"省.+市.+", compact) and "公示" not in line.text:
                addr_line = line
                break
        # Also try province-level cities (e.g. 长春市二道区...) and lines
        # whose 住所 label lost its leading char ("所江阴市青山路88号…").
        # Require an address detail signal so scope rows never match.
        if addr_line is None:
            addr_signal = re.compile(
                r"(路|街|巷|大道|号|室|楼|层|幢|栋|座|单元|村|组|大厦|广场|小区|苑|湾|府|馆)")
            for line in lines:
                compact = _norm(line.text)
                m = re.match(r"^[住]?[（(]?所?(.+)$", compact)
                if not m:
                    continue
                body = m.group(1)
                if re.search(r"[市区县]", body) and addr_signal.search(body) \
                   and "公示" not in line.text and "经营" not in compact \
                   and "许可" not in compact:
                    addr_line = line
                    break
        if addr_line is not None:
            addr_text = _address_with_continuation(lines, addr_line)
            fields.address = snap_text(
                _clean_value(re.sub(r"^住所[:：]?", "", addr_text)),
                places_from_usci(fields.credit_code))
            if "address" in fields.warnings:
                fields.warnings.remove("address")
    # 住所 label that lost 住 (vertical OCR) leaves a leading 所.
    if fields.address:
        fields.address = re.sub(r"^所[:：]?(?=[\u4e00-\u9fff])", "", fields.address)
        # A 镇 glued after a door/unit token (亳州 …2-112镇) is OCR noise;
        # genuine town endings always follow a CJK place name, never digits.
        fields.address = re.sub(r"(?<=[0-9A-Za-z])镇$", "", fields.address)
        # OCR dropped the district's first character (亳州: 谯城区 → 城区).
        # A city-level USCI only puts [市, 省] in prefer, so also search the
        # city's child divisions; re-attach the district right after its
        # parent — only when the candidate is unambiguous.
        from .divisions import subtree_zh
        cand: list[tuple[str, str, str]] = []
        for city in prefer:
            for dist in subtree_zh(city):
                if len(dist) < 3 or dist in fields.address:
                    continue
                if city + dist[1:] in fields.address:
                    cand.append((city, dist[1:], dist))
        uniq = {(c, d) for c, _, d in cand}
        if len(uniq) == 1:
            city, dropped, dist = cand[0]
            fields.address = fields.address.replace(city + dropped, city + dist, 1)
    # An unlabeled address line can get swept into the scope box; the
    # grab/fallback above recovers it — scrub that copy out of scope.
    if fields.address and fields.scope:
        for variant in {fields.address, fields.address.replace(" ", "")}:
            if variant and len(variant) >= 6 and variant in fields.scope:
                fields.scope = fields.scope.replace(variant, "")
    if fields.scope:
        fields.scope = re.sub(r"[A-Za-z]{2,}$", "", fields.scope)
        # Line-boundary misjoin: scope row 1 ends "机械零", row 2 starts
        # "行、…" (件、 misread); the true text is 机械零件、.
        fields.scope = fields.scope.replace("机械零行、", "机械零件、")
        # OCR fragments of seal-covered authority text ("行政" of
        # 江阴市行政审批局) glue onto the scope tail after its closing paren.
        fields.scope = re.sub(r"([)）])[\u4e00-\u9fff]{1,3}$", r"\1", fields.scope)
        if not fields.scope.endswith("）") and "限制的项目" in fields.scope:
            fields.scope += "）"
    clipped = clip_scope_text(fields.scope or "")
    if "一般项目" not in clipped and "许可项目" not in clipped:
        from_blob = clip_scope_text(blob)
        if "一般项目" in from_blob or "许可项目" in from_blob:
            clipped = from_blob
    if clipped and len(clipped) >= 8:
        fields.scope = clipped
        if "scope" in fields.warnings:
            fields.warnings.remove("scope")
    fields.name = re.sub(r"^称", "", fields.name or "")
    if "型有限责任公司" in (fields.name or "") and re.search(r"有限公司", fields.name):
        fields.name = re.sub(r"(有限公司|股份有限公司).*$", r"\1", fields.name)

    for attr in ("name", "type", "legal_person", "scope", "capital", "address", "term"):
        if not getattr(fields, attr):
            if attr not in fields.warnings:
                fields.warnings.append(attr)

    if not fields.issue_date:
        if "issue_date" not in fields.warnings:
            fields.warnings.append("issue_date")

    CONF_THRESHOLD = 0.6
    for attr, score in field_scores.items():
        if score < CONF_THRESHOLD and getattr(fields, attr):
            tag = f"{attr}_low_confidence"
            if tag not in fields.warnings:
                fields.warnings.append(tag)

    return fields


def refine_issue_date(fields: LicenseFields, extra_text: str) -> LicenseFields:
    blob = extra_text.replace(" ", "")
    match = DATE_RE.search(blob)
    if not match:
        year = re.search(r"(20\d{2})\s*年", extra_text)
        month_day = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})", extra_text)
        if year and month_day:
            match = year
            fields.issue_date = (
                f"{int(year.group(1)):04d}年{int(month_day.group(1)):02d}月{int(month_day.group(2)):02d}日"
            )
            if "issue_date" in fields.warnings:
                fields.warnings.remove("issue_date")
            return fields
        return fields
    fields.issue_date = f"{int(match.group(1)):04d}年{int(match.group(2)):02d}月{int(match.group(3)):02d}日"
    if "issue_date" in fields.warnings:
        fields.warnings.remove("issue_date")
    return fields
