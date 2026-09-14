from __future__ import annotations

import json
import logging
import re
import threading
from functools import lru_cache
from pathlib import Path

import cn2an

from .divisions import consume_admin, snap_text, zh_to_en

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = ROOT / "glossary"

LOG = logging.getLogger("license_trans")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# ---------- API translation fallback ----------
_API_CACHE_FILE = GLOSSARY / "api_cache.json"
_api_cache: dict[str, str] = {}
_api_cache_lock = threading.Lock()
_api_cache_loaded = False


def _load_api_cache():
    global _api_cache_loaded
    if _api_cache_loaded:
        return
    with _api_cache_lock:
        if _api_cache_loaded:
            return
        try:
            if _API_CACHE_FILE.exists():
                _api_cache.update(json.loads(_API_CACHE_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
        _api_cache_loaded = True


def _save_api_cache():
    try:
        GLOSSARY.mkdir(parents=True, exist_ok=True)
        with _api_cache_lock:
            _API_CACHE_FILE.write_text(
                json.dumps(_api_cache, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except Exception as exc:
        LOG.warning("保存翻译缓存失败: %s", exc)


def _api_translate(text: str) -> str:
    """Neural MT fallback using Bing (via translators package).

    Caches results to glossary/api_cache.json so they persist offline.
    Returns the translation, or empty string on failure.
    """
    if not text or not _CJK_RE.search(text):
        return ""
    text = text.strip()
    if not text:
        return ""
    _load_api_cache()
    if text in _api_cache:
        return _api_cache[text]
    try:
        import translators as ts
        result = ts.translate_text(
            text, from_language="zh", to_language="en",
            translator="bing", if_show_original_text=False)
        result = (result or "").strip()
        if result and not _CJK_RE.search(result):
            with _api_cache_lock:
                _api_cache[text] = result
            _save_api_cache()
            return result
    except Exception as exc:
        LOG.debug("API翻译失败(%s): %s", text[:30], exc)
    return ""

MONTHS = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
TEENS = [
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
NUM_DATE_RE = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")


@lru_cache(maxsize=None)
def _load(name: str) -> dict[str, str]:
    path = GLOSSARY / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _try_pinyin(text: str, heteronym: bool = False) -> str:
    try:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(text, style=Style.NORMAL)
        return "".join(p.capitalize() if i == 0 else p for i, p in enumerate(parts)) if len(text) > 1 and " " not in text else " ".join(p.capitalize() for p in parts)
    except Exception:
        return text


def pinyin_word(text: str) -> str:
    """Join pinyin syllables into one word, only the first letter capitalized
    (跃坤 -> Yuekun), matching standard company/road-name romanization."""
    compact = (text or "").strip()
    if not compact:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(compact, style=Style.NORMAL)
        joined = "".join(p for p in parts if p)
        return joined[:1].upper() + joined[1:] if joined else compact
    except Exception:
        return compact


def person_pinyin(name: str) -> str:
    name = name.strip()
    if not name:
        return ""
    people = _load("people.json")
    if name in people:
        return people[name]
    try:
        from pypinyin import Style, lazy_pinyin

        if len(name) == 1:
            return lazy_pinyin(name, style=Style.NORMAL)[0].capitalize()
        doubles = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "司徒")
        if name.startswith(doubles):
            family, given = name[:2], name[2:]
        else:
            family, given = name[0], name[1:]
        if family in people:
            fam = people[family]
        else:
            fam = "".join(lazy_pinyin(family, style=Style.NORMAL)).capitalize()
        giv = "".join(lazy_pinyin(given, style=Style.NORMAL)).capitalize()
        return f"{fam} {giv}".strip()
    except Exception:
        return name


def _replace_longest(text: str, table: dict[str, str]) -> str:
    if not text:
        return text
    keys = sorted(table, key=len, reverse=True)
    i = 0
    out: list[str] = []
    while i < len(text):
        matched = False
        for key in keys:
            if text.startswith(key, i):
                piece = table[key]
                if out:
                    prev = out[-1]
                    if prev.endswith("-") or piece.startswith("-"):
                        pass
                    elif prev[-1].isalnum() and piece[:1].isalnum():
                        out.append(" ")
                out.append(piece)
                i += len(key)
                matched = True
                break
        if not matched:
            out.append(text[i])
            i += 1
    return "".join(out)


def translate_date(text: str) -> str:
    if not text:
        return ""
    match = DATE_RE.search(text) or NUM_DATE_RE.search(text)
    if not match:
        return text
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if not (1 <= month <= 12):
        return text
    return f"{MONTHS[month]} {day}, {year}"


def _under_thousand(n: int) -> str:
    hundred, rest = divmod(n, 100)
    pieces: list[str] = []
    if hundred:
        pieces.append(f"{ONES[hundred]} Hundred")
    if rest >= 20:
        ten, one = divmod(rest, 10)
        pieces.append(TENS[ten] + (f"-{ONES[one]}" if one else ""))
    elif rest >= 10:
        pieces.append(TEENS[rest - 10])
    elif rest:
        pieces.append(ONES[rest])
    return " ".join(pieces)


def integer_to_words(n: int) -> str:
    if n == 0:
        return "Zero"
    scales = [
        (1_000_000_000, "Billion"),
        (1_000_000, "Million"),
        (1_000, "Thousand"),
    ]
    parts: list[str] = []
    for value, name in scales:
        qty, n = divmod(n, value)
        if qty:
            parts.append(f"{_under_thousand(qty)} {name}".strip())
    if n:
        parts.append(_under_thousand(n))
    return " ".join(parts)


def translate_capital(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    compact = raw.replace(" ", "").replace("圆", "元").replace("人民幣", "人民币")
    currency = "CNY"
    currency_map = [
        (("美元", "USD"), "USD"),
        (("港币", "港元", "HKD"), "HKD"),
        (("欧元", "EUR"), "EUR"),
        (("日元", "JPY"), "JPY"),
        (("英镑", "GBP"), "GBP"),
        (("澳门元", "澳元", "MOP"), "MOP"),
        (("人民币", "CNY", "RMB"), "CNY"),
    ]
    for keys, code in currency_map:
        if any(k in compact for k in keys):
            currency = code
            break
    number_part = compact
    # Convert 圆 to 元 first, then strip all unit tokens
    number_part = number_part.replace("圆", "元")
    for token in ("人民币", "CNY", "RMB", "美元", "USD", "港币", "港元", "HKD", "欧元", "EUR", "日元", "JPY", "英镑", "GBP", "整", "元", "正", "认缴"):
        number_part = number_part.replace(token, "")
    number_part = number_part.replace("万元", "万")
    # Strip leading non-numeric characters (OCR noise like 登/斌/查/·)
    number_part = re.sub(r"^[^\d零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟参两]+", "", number_part)
    # Pre-normalize financial numerals to simple forms before cn2an
    # (洽 is a recurring OCR error for 拾: 伍洽万元 = 伍拾万元)
    fin_to_simple = str.maketrans(
        "零壹贰叁肆伍陆柒捌玖拾佰仟万亿参两洽",
        "零一二三四五六七八九十百千万亿三两十",
    )
    number_part = number_part.translate(fin_to_simple)
    # Add implicit 一 before 百/千/万 when missing (百万 = 一百万)
    number_part = re.sub(r"^(百|千|万)", r"一\1", number_part)
    amount = None
    try:
        if re.fullmatch(r"[\d.,]+万?", number_part):
            if number_part.endswith("万"):
                amount = int(float(number_part[:-1]) * 10000)
            else:
                amount = int(float(number_part.replace(",", "")))
        else:
            amount = int(cn2an.cn2an(number_part, "smart"))
    except Exception:
        try:
            amount = int(cn2an.transform(number_part + "元", "cn2an").replace("元", "") or 0)
        except Exception:
            amount = None
    if amount is None:
        return f"{currency} {raw}"
    return f"{currency} {integer_to_words(amount)} Only"


def translate_type(text: str) -> str:
    compact = (text or "").replace(" ", "").replace("（", "(").replace("）", ")")
    table = _load("types.json")
    if compact in table:
        return table[compact]
    atoms = _load("type_atoms.json")
    match = re.match(r"(一人有限责任公司|有限责任公司|股份有限公司)(?:\((.+)\))?$", compact)
    if match:
        head = atoms.get(match.group(1), table.get(match.group(1), match.group(1)))
        if not match.group(2):
            return head
        mods = [part.strip() for part in re.split(r"[、,;；]", match.group(2)) if part.strip()]
        en_mods = []
        for part in mods:
            if part in atoms:
                en_mods.append(atoms[part])
            else:
                en_mods.append(_replace_longest(part, atoms))
        return f"{head} ({'; '.join(en_mods)})"
    merged = {**atoms, **table}
    result = _replace_longest(compact, merged) or text
    # API fallback: if glossary left untranslated CJK, try neural MT
    if _CJK_RE.search(result):
        api = _api_translate(compact)
        if api:
            return api
    return result


def translate_person(text: str) -> str:
    name = (text or "").strip()
    people = _load("people.json")
    if name in people:
        return people[name]
    return person_pinyin(name)


def translate_company(text: str) -> str:
    name = snap_text(re.sub(r"^称", "", (text or "").strip()))
    name = name.replace("（", "(").replace("）", ")")
    companies = _load("companies.json")
    if name in companies:
        return companies[name]
    places = zh_to_en()
    words = _load("words.json")
    work = name
    out: list[str] = []
    keys = sorted({**places, **words}, key=len, reverse=True)
    i = 0
    unknown: list[str] = []

    def flush_unknown() -> None:
        chunk = "".join(unknown)
        unknown.clear()
        if not chunk:
            return
        if chunk.endswith("市") and chunk in places:
            out.append(places[chunk].replace(" City", ""))
            return
        if chunk in places:
            token = places[chunk]
            for extra in (" Province", " City", " District", " County"):
                token = token.replace(extra, "")
            out.append(token)
            return
        out.append(pinyin_word(chunk))

    while i < len(work):
        matched = None
        for key in keys:
            if work.startswith(key, i):
                matched = key
                break
        if matched:
            flush_unknown()
            table = words if matched in words else places
            token = table[matched]
            if matched in places:
                token = token.replace(" Province", "").replace(" City", "").replace(" District", "")
            out.append(token)
            i += len(matched)
        else:
            unknown.append(work[i])
            i += 1
    flush_unknown()
    # ensure Co., Ltd. if original had 公司 and we missed it
    joined = " ".join(part for part in out if part)
    if "公司" in name and "Co." not in joined:
        joined = f"{joined} Co., Ltd."
    # Remove duplicate pinyin of 公司 (Gongsi/GongSi) when Co., Ltd. is present
    joined = re.sub(r"\s*Gongsi\s*$", "", joined, flags=re.IGNORECASE)
    joined = re.sub(r"\s*Gong\s+Si\s*$", "", joined, flags=re.IGNORECASE)
    joined = re.sub(r"\(\s+", "(", joined)
    joined = re.sub(r"\s+\)", ")", joined)
    return re.sub(r"\s+", " ", joined).strip(" ,")


_SCOPE_HEADERS = {
    "许可经营项目": "Licensed items",
    "一般经营项目": "General items",
    "许可项目": "Licensed items",
    "一般项目": "General items",
}
_EXCEPT_RE = re.compile(r"[（(]除([^）)]+)[)）]")
_EXCEPT_TAIL_RE = re.compile(r"[（(]([^）)]+除外)[)）]")
_WITHOUT_RE = re.compile(r"[（(]不含([^）)]+)[)）]")


def _scope_tables() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    phrases = {**_load("scope_extra.json"), **_load("phrases.json")}
    verbs = _load("scope_verbs.json")
    commodities = {**_load("scope_atoms.json"), **_load("commodities.json")}
    commodities = {k: v for k, v in commodities.items() if k not in verbs}
    return phrases, verbs, commodities


def leftover_cjk(text: str) -> list[str]:
    return _CJK_RE.findall(text or "")


def translate_noun(text: str) -> str:
    raw = (text or "").strip().strip(" :：;；,，、")
    if not raw:
        return ""
    raw = raw.replace("的", "")
    phrases, verbs, commodities = _scope_tables()
    table = {**commodities, **phrases}
    for verb, tmpl in verbs.items():
        table.setdefault(verb, tmpl if "{n}" not in tmpl else _strip_of_noun(tmpl))
    if raw in table:
        return table[raw]
    parts = [p for p in re.split(r"[及和与、]", raw) if p]
    if len(parts) <= 1:
        return _replace_longest(raw, table)
    out: list[str] = []
    for part in parts:
        if part in table:
            out.append(table[part])
            continue
        rendered = _replace_longest(part, table)
        out.append(rendered)
    joined = " and ".join(out)
    joined = re.sub(r"\s+", " ", joined).strip(" ,;")
    return joined


def _strip_of_noun(template: str) -> str:
    return (template.replace(" of {n}", "")
            .replace(" for {n}", "")
            .replace(" in {n}", "")
            .replace("{n}", "")
            .strip())


def _catalog_en(item: str) -> str:
    rec = _load("scope_catalog.json").get(item)
    if isinstance(rec, str):
        text = rec.strip()
        return text if text and not leftover_cjk(text) else ""
    if isinstance(rec, dict):
        text = (rec.get("en") or "").strip()
        return text if text and not leftover_cjk(text) else ""
    return ""


def translate_scope_item(item: str) -> str:
    item = (item or "").strip().strip(" :：;；,，、。.")
    if not item:
        return ""
    if not _CJK_RE.search(item):
        return item
    if item in _SCOPE_HEADERS:
        return _SCOPE_HEADERS[item]
    phrases, verbs, commodities = _scope_tables()
    if item in phrases:
        return phrases[item]
    if item in verbs and "{n}" not in verbs[item]:
        return verbs[item]
    catalog = _catalog_en(item)
    if catalog:
        return catalog
    if item in commodities:
        return commodities[item]
    excepts = []
    def _except(match: re.Match) -> str:
        inner = translate_noun(match.group(1))
        excepts.append(f"(excluding {inner})" if inner else match.group(0))
        return ""
    def _except_tail(match: re.Match) -> str:
        inner = translate_noun(match.group(1).replace("除外", ""))
        excepts.append(f"(except {inner})" if inner else match.group(0))
        return ""
    def _without(match: re.Match) -> str:
        inner = translate_noun(match.group(1))
        excepts.append(f"(excluding {inner})" if inner else match.group(0))
        return ""
    core = _EXCEPT_RE.sub(_except, item)
    core = _EXCEPT_TAIL_RE.sub(_except_tail, core)
    core = _WITHOUT_RE.sub(_without, core)
    def _note(match: re.Match) -> str:
        inner = match.group(1)
        if inner in ("地区", "市", "县", "区"):
            return match.group(0)
        rendered = translate_scope_item(inner) or translate_noun(inner)
        excepts.append(f"({rendered})" if rendered else match.group(0))
        return ""
    core = re.sub(r"[（(]([^）)]+)[)）]", _note, core).strip(" :：;；,，、。（(）)")
    if core in phrases:
        body = phrases[core]
    elif core in commodities:
        body = commodities[core]
    else:
        body = ""
        matches: list[tuple[str, str, str]] = []
        for verb in sorted(verbs, key=len, reverse=True):
            tmpl = verbs[verb]
            if core == verb:
                matches.append((verb, "", tmpl))
                break
            if core.endswith(verb) and len(core) > len(verb):
                matches.append((verb, core[: -len(verb)].rstrip("的及和与"), tmpl))
        table_hits = [
            item for item in matches
            if item[1] and (item[1] in commodities or item[1] in phrases)
        ]
        chosen = (
            max(table_hits, key=lambda item: len(item[1]))
            if table_hits
            else (matches[0] if matches else None)
        )
        if chosen:
            verb, noun, tmpl = chosen
            if not noun:
                body = _strip_of_noun(tmpl)
            else:
                noun_en = translate_noun(noun)
                if "{n}" in tmpl:
                    body = tmpl.replace("{n}", noun_en or noun)
                elif noun_en:
                    body = f"{tmpl} for {noun_en}"
                else:
                    body = tmpl
        if not body:
            body = translate_noun(core) or core
    if excepts:
        body = f"{body} {' '.join(excepts)}".strip()
    # API fallback: if glossary left untranslated CJK, try neural MT.
    # The API sees the full item with parentheses intact, so its output
    # already covers the extracted excepts — re-appending them duplicates
    # (瓶（罐）装饮用水（食品生产） → …(food production) (Can) (Food production)).
    if _CJK_RE.search(body):
        api = _api_translate(item)
        if api:
            return api
    return body


_GLUE_VERBS = (
    "批发兼零售",
    "零售兼批发",
    "批发零售",
    "生产销售",
    "互联网销售",
    "网上销售",
    "销售",
    "批发",
    "零售",
    "生产",
    "制造",
    "加工",
    "回收",
    "收购",
    "租赁",
    "进出口",
)


def _split_glued_items(text: str) -> str:
    """Insert顿号 when OCR glued two items: 销售新能源汽车 → 销售、新能源汽车."""
    if not text:
        return text
    _phrases, _verbs, commodities = _scope_tables()
    starters = sorted(
        [k for k in list(commodities) + list(_SCOPE_HEADERS) if len(k) >= 2],
        key=len,
        reverse=True,
    )
    verb_keys = sorted(_GLUE_VERBS, key=len, reverse=True)
    i = 0
    out: list[str] = []
    while i < len(text):
        verb = next((v for v in verb_keys if text.startswith(v, i)), None)
        if verb:
            out.append(verb)
            i += len(verb)
            nxt = next((k for k in starters if text.startswith(k, i)), None)
            if nxt:
                out.append("、")
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _split_scope_pieces(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "（(":
            depth += 1
            buf.append(ch)
        elif ch in "）)":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in "、，;；" and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        else:
            buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def _replace_colons_outside_parens(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch in "（(":
            depth += 1
            out.append(ch)
        elif ch in "）)":
            depth = max(0, depth - 1)
            out.append(ch)
        elif ch in "：" and depth == 0:
            out.append("、")
        else:
            out.append(ch)
    return "".join(out)


def translate_scope(text: str) -> str:
    from .parse import clip_scope_text

    compact = clip_scope_text(text or "")
    phrases, _verbs, _commodities = _scope_tables()
    for key in sorted(phrases, key=len, reverse=True):
        if len(key) >= 10:
            # Consume the wrapping parens and the full stop together with the
            # key: a bare-key replace leaves orphan （ / 。） behind that glue
            # onto the neighbouring item (开平: 瓶（罐）…（食品生产）(、¤items¤、。）
            pat = re.compile(r"[（(]\s*" + re.escape(key) + r"\s*[。.]?\s*[）)]")
            if pat.search(compact):
                compact = pat.sub(f"、¤{phrases[key]}¤、", compact)
            else:
                compact = compact.replace(key, f"、¤{phrases[key]}¤、")
    compact = _split_glued_items(compact)
    compact = re.sub(
        r"(许可经营项目|一般经营项目|许可项目|一般项目)[:：]?",
        r"¤H¤\1¤C¤",
        compact,
    )
    # OCR often uses ： as an item separator instead of 、
    compact = _replace_colons_outside_parens(compact)
    compact = compact.replace("¤C¤", "：").replace("¤H¤", "、")
    pieces = _split_scope_pieces(compact)
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip().strip(":")
        if not piece:
            continue
        if "¤" in piece:
            for chunk in piece.split("¤"):
                chunk = chunk.strip(" 、")
                if chunk:
                    out.append(chunk if not _CJK_RE.search(chunk)
                               else translate_scope_item(chunk))
            continue
        sep = "：" if "：" in piece else (":" if ":" in piece else "")
        if sep:
            head, tail = piece.split(sep, 1)
            if head in _SCOPE_HEADERS:
                tail_en = translate_scope_item(tail) if tail else ""
                out.append(f"{_SCOPE_HEADERS[head]}: {tail_en}".rstrip())
                continue
            if tail:
                out.append(translate_scope_item(head))
                out.append(translate_scope_item(tail))
                continue
        out.append(translate_scope_item(piece))
    rendered = "; ".join(p for p in out if p)
    rendered = rendered.replace("¤", "")
    rendered = re.sub(r"\s+", " ", rendered)
    rendered = re.sub(r" ;", ";", rendered)
    rendered = re.sub(r";{2,}", ";", rendered)
    rendered = re.sub(r"([a-z])\(", r"\1 (", rendered)
    rendered = re.sub(r"\)([A-Za-z])", r") \1", rendered)
    rendered = rendered.replace("items:", "items:")
    # API fallback for any remaining untranslated CJK chunks in scope
    leftover = _CJK_RE.findall(rendered)
    if leftover:
        for chunk in set(leftover):
            if len(chunk) >= 2:
                api = _api_translate(chunk)
                if api:
                    rendered = rendered.replace(chunk, api)
    return rendered.strip(" ;")



STREET_SUFFIXES = (
    ("高新技术产业开发区", "High-tech Industrial Development Zone"),
    ("经济技术开发区", "Economic and Technological Development Zone"),
    ("经济开发区", "Economic Development Zone"),
    ("高新技术产业园", "High-tech Industrial Park"),
    ("工业园区", "Industrial Park"),
    ("科技园区", "Science Park"),
    ("高新区", "High-tech Zone"),
    ("开发区", "Development Zone"),
    ("工业园", "Industrial Park"),
    ("产业园", "Industrial Park"),
    ("科技园", "Science Park"),
    ("大学城", "University Town"),
    ("商务区", "Business District"),
    ("保税区", "Bonded Zone"),
    ("度假区", "Resort Area"),
    ("风景区", "Scenic Area"),
    ("步行街", "Pedestrian Street"),
    ("街道办事处", "Subdistrict Office"),
    ("街道", "Subdistrict"),
    ("大街", "Street"),
    ("大道", "Avenue"),
    ("南路", "South Road"),
    ("北路", "North Road"),
    ("东路", "East Road"),
    ("西路", "West Road"),
    ("中路", "Middle Road"),
    ("路", "Road"),
    ("街坊", "Block"),
    ("街", "Street"),
    ("巷", "Alley"),
    ("道", "Road"),
    ("弄", "Lane"),
    ("里", "Li"),
    ("胡同", "Hutong"),
    ("社区", "Community"),
    ("村委会", "Village Committee"),
    ("小学", "Primary School"),
    ("办公楼", "Office Building"),
    ("旧址", "Former Site"),
    ("小区", "Residential Community"),
    ("购物广场", "Shopping Plaza"),
    ("购物中心", "Shopping Center"),
    ("广场", "Plaza"),
    ("大厦", "Mansion"),
    ("市场", "Market"),
    ("商城", "Mall"),
    ("商铺", "Shop"),
    ("镇", "Town"),
    ("乡", "Township"),
    ("村", "Village"),
    ("庄", "Village"),
)

UNIT_SUFFIXES = {
    "号铺": "Shop {num}",
    "号楼": "Building {num}",
    "号院": "Yard {num}",
    "号栋": "Building {num}",
    "号幢": "Building {num}",
    "号房": "Room {num}",
    "号组": "Group {num}",
    "号": "No. {num}",
    "铺": "Shop {num}",
    "室": "Room {num}",
    "房": "Room {num}",
    "组": "Group {num}",
    "层": "{num}F",
    "幢": "Building {num}",
    "栋": "Building {num}",
    "座": "Block {num}",
    "单元": "Unit {num}",
    "段": "Section {num}",
    "期": "Phase {num}",
    "F": "{num}F",
    "楼": "{num}F",
}


def place_pinyin(text: str) -> str:
    return pinyin_word(text)


def _street_glossary() -> dict[str, str]:
    extra = _load("places.json")
    admin = zh_to_en()
    return {key: value for key, value in extra.items() if key not in admin}


def translate_address(text: str, prefer: list[str] | None = None) -> str:
    raw = snap_text((text or "").replace(" ", ""), prefer)
    if not raw:
        return ""
    places = zh_to_en()
    admin, rest = consume_admin(raw, prefer)
    admin_en = [places.get(token, token) for token in admin]
    from license_trans.divisions import all_rows, subtree_zh
    # Division glossary hits beyond the consumed admin chain are a different
    # region's namesake and must not steal mid-name matches: 三台县老马村's
    # 马村 (焦作), 北滨河西路's 河西 (天津), 和平北街's 和平 (天津/沈阳).
    div_zh = frozenset(r["zh"] for r in all_rows())
    scope = subtree_zh(admin[-1]) if admin else None
    unit_kinds = "号铺|号楼|号院|号栋|号幢|号房|号组|号|铺|室|房|组|层|幢|栋|座|单元|段|期|楼"
    unit_num = r"[0-9A-Za-z]+"
    units = re.findall(rf"({unit_num}(?:-{unit_num})*)({unit_kinds})", rest)
    cn_num = "[一二三四五六七八九十百千万]+"
    cn_units = re.findall(rf"({cn_num})({unit_kinds})", rest)
    unit_en = []
    for num, kind in units:
        unit_en.append(UNIT_SUFFIXES.get(kind, "{num}").format(num=num))
        rest = rest.replace(f"{num}{kind}", "", 1)
    cn_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    for num_cn, kind in cn_units:
        if num_cn in cn_map:
            num = cn_map[num_cn]
        elif num_cn.startswith("十"):
            num = str(10 + cn_map.get(num_cn[1], 0)) if len(num_cn) > 1 else "10"
        elif "十" in num_cn:
            parts = num_cn.split("十")
            tens = cn_map.get(parts[0], "1") if parts[0] else "1"
            ones = cn_map.get(parts[1], "0") if len(parts) > 1 and parts[1] else "0"
            num = str(int(tens) * 10 + int(ones))
        else:
            num = num_cn
        unit_en.append(UNIT_SUFFIXES.get(kind, "{num}").format(num=num))
        rest = rest.replace(f"{num_cn}{kind}", "", 1)
    # 第X幢/第X层 ordinals lose their numeral to the unit extraction above,
    # leaving a dangling 第 glued to the following name (立达汽配城第商铺);
    # strip it only when no numeral follows (第一中学 stays intact).
    rest = re.sub(r"第(?![0-9零一二三四五六七八九十百])", "", rest)
    overlay = {**{r["zh"]: r["en"] for r in all_rows() if r["kind"] != "town_core"}, **_street_glossary()}
    keys = sorted(overlay, key=len, reverse=True)
    rest_bits: list[str] = []
    i = 0
    buf = ""
    alnum_run = re.compile(r"[0-9A-Za-z](?:[0-9A-Za-z.\-]*[0-9A-Za-z])?")

    def flush() -> None:
        nonlocal buf
        if buf:
            rest_bits.append(place_pinyin(buf))
            buf = ""

    while i < len(rest):
        hit = next(
            (
                key for key in keys
                if rest.startswith(key, i) and len(key) >= 2
                and (scope is None or key not in div_zh or key in scope)
            ),
            None,
        )
        suffix = next((pair for pair in STREET_SUFFIXES if rest.startswith(pair[0], i)), None)
        if suffix and (not hit or len(suffix[0]) >= len(hit)):
            prefix = buf
            buf = ""
            if not prefix and rest_bits:
                prev = rest_bits[-1]
                if prev and re.fullmatch(r"[A-Za-z]+", prev) and prev[0].isupper():
                    prefix = rest_bits.pop()
                elif re.fullmatch(r"[0-9A-Za-z.\-]+", prev or ""):
                    # a bare street suffix glued right after a number is an
                    # OCR stray (2-112镇), not a real place suffix
                    i += len(suffix[0])
                    continue
            token = f"{place_pinyin(prefix)} {suffix[1]}".strip() if prefix else suffix[1]
            rest_bits.append(token)
            i += len(suffix[0])
            continue
        if hit:
            flush()
            rest_bits.append(overlay[hit])
            i += len(hit)
        elif "\u4e00" <= rest[i] <= "\u9fff":
            buf += rest[i]
            i += 1
        else:
            flush()
            run = alnum_run.match(rest, i)
            if run:
                rest_bits.append(run.group(0))
                i = run.end()
            elif rest[i] not in " ,，、·•（）()":
                rest_bits.append(rest[i])
                i += 1
            else:
                i += 1
    flush()
    rest_bits = [bit.strip(" ,") for bit in reversed(rest_bits) if bit.strip(" ,")]
    ordered = unit_en + rest_bits + list(reversed(admin_en))
    return ", ".join(part for part in ordered if part)


def translate_term(text: str) -> str:
    raw = (text or '').replace(' ', '')
    if not raw:
        return ''
    if raw in {'长期', '永久', '无固定期限'}:
        return 'Long term'
    if '至长期' in raw or '到长期' in raw:
        start = re.search(r'(20\d{2}年\d{1,2}月\d{1,2}日)', raw)
        if start:
            return f'{translate_date(start.group(1))} to Long term'
        return 'Long term'
    # End date lost to OCR/template: the license prints …至 (or …至____年__月__日)
    # and the blank never survived scanning (放杖).
    if raw.endswith(('至', '到')) or '____' in raw or re.search(r'_{2,}', raw):
        start = re.search(r'(20\d{2}年\d{1,2}月\d{1,2}日)', raw)
        if start:
            return f'{translate_date(start.group(1))} to (blank)'
    span = re.findall(r'20\d{2}年\d{1,2}月\d{1,2}日', raw)
    if len(span) >= 2:
        return f'{translate_date(span[0])} to {translate_date(span[1])}'
    return translate_date(raw) or raw


def translate_authority(text: str, address: str = '', name: str = '') -> str:
    raw = snap_text((text or '').replace(' ', ''))
    places = zh_to_en()
    m = re.search(r'(.+?)(市场监督管理局|市场监督管理分局|工商行政管理局)', raw)
    if m and '监制' not in raw:
        place = m.group(1)
        bureau = 'Municipal Market Supervision and Administration Bureau'
        if '分局' in m.group(2):
            bureau = 'Market Supervision and Administration Branch'
        elif '工商' in m.group(2):
            bureau = 'Administration for Industry and Commerce'
        en = places.get(place) or places.get(place + '市') or places.get(place[:-1] if place.endswith('市') else place)
        if not en:
            en = person_pinyin(place.replace('市', '')).replace(' ', '')
        en = str(en).replace(' City', '').replace(' Province', '').replace(' District', '').strip()
        return f'{en} {bureau} (Sealed)'
    hint = snap_text((address or "") + (name or ""))
    for city in sorted((key for key in places if key.endswith("市") and len(key) >= 3), key=len, reverse=True):
        if city in hint:
            return translate_authority(city + "市场监督管理局")
    return ""


def translate_fields(fields) -> dict:
    data = fields.to_dict() if hasattr(fields, 'to_dict') else dict(fields)
    from .divisions import places_from_usci

    prefer = places_from_usci(data.get("credit_code") or "")
    address_en = translate_address(data.get("address") or "", prefer)
    scope = translate_scope(data.get('scope') or '')
    leftover = re.findall(r"[\u4e00-\u9fff]+", scope)
    warnings = list(data.get("warnings") or [])
    if leftover:
        warnings.append("scope_untranslated")
    authority = translate_authority(data.get("authority") or "", data.get("address") or "", data.get("name") or "")
    if authority and "authority" in warnings:
        warnings.remove("authority")
    elif not authority and "authority" not in warnings:
        warnings.append("authority")
    result = {
        "credit_code": data.get("credit_code") or "",
        "copy_no": data.get("copy_no") or "1-1",
        "duplicate": bool(data.get("duplicate", False)),
        "name": translate_company(data.get("name") or ""),
        "type": translate_type(data.get("type") or ""),
        "legal_person": translate_person(data.get("legal_person") or ""),
        "scope": scope,
        "capital": translate_capital(data.get("capital") or ""),
        "established": translate_date(data.get("established") or ""),
        "address": address_en,
        "term": translate_term(data.get("term") or ""),
        "authority": authority,
        "issue_date": translate_date(data.get("issue_date") or ""),
        "warnings": warnings,
        "source": {k: data.get(k) for k in ("credit_code", "name", "type", "legal_person", "scope", "capital", "established", "address", "authority", "issue_date", "term")},
    }
    for attr in ("name", "type", "legal_person", "scope", "address"):
        if not result.get(attr):
            tag = f"en_{attr}_empty"
            if tag not in warnings:
                warnings.append(tag)
    return result
