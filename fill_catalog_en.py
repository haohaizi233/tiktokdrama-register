# -*- coding: utf-8 -*-
"""Compose English for scraped SAMR catalog items and harvest leftover nouns."""
from __future__ import annotations

import json
import re
from pathlib import Path

from license_trans.translate import leftover_cjk, translate_scope_item

ROOT = Path(__file__).resolve().parent
CAT = ROOT / "glossary" / "scope_catalog.json"
COMM = ROOT / "glossary" / "commodities.json"
CJK = re.compile(r"[\u4e00-\u9fff]+")
CODE_RE = re.compile(r"^\d+")

SUFFIX = [
    ("专业及辅助性活动", "professional and supporting activities"),
    ("修理与维护", "repair and maintenance"),
    ("综合利用", "comprehensive utilization"),
    ("加工处理", "processing"),
    ("种植", "planting"),
    ("栽培", "cultivation"),
    ("养殖", "breeding"),
    ("饲养", "raising"),
    ("捕捞", "fishing"),
    ("开采", "mining"),
    ("制造", "manufacture"),
    ("生产", "production"),
    ("加工", "processing"),
    ("批发", "wholesale"),
    ("零售", "retail"),
    ("租赁", "leasing"),
    ("运输", "transportation"),
    ("仓储", "warehousing"),
    ("施工", "construction"),
    ("咨询", "consulting"),
    ("开发", "development"),
    ("服务", "services"),
    ("修理", "repair"),
    ("维修", "repair"),
    ("销售", "sales"),
    ("制品", "products"),
    ("设备", "equipment"),
    ("材料", "materials"),
    ("产品", "products"),
]


def guess_en(name: str, table: dict[str, str]) -> str:
    if name in table:
        return table[name]
    raw = name
    prefix = ""
    if raw.startswith("其他未列明"):
        prefix, raw = "other unspecified ", raw[5:]
    elif raw.startswith("其他"):
        prefix, raw = "other ", raw[2:]
    merged = {**dict(SUFFIX), **table}
    keys = sorted(merged, key=len, reverse=True)
    i = 0
    bits: list[str] = []
    buf = ""
    while i < len(raw):
        hit = next((k for k in keys if raw.startswith(k, i) and len(k) >= 2), None)
        if hit:
            if buf:
                return ""
            bits.append(merged[hit])
            i += len(hit)
        elif raw[i] in "、，,及和与；;":
            buf = ""
            i += 1
        else:
            return ""
    body = " ".join(bits).strip()
    if not body or CJK.search(body):
        return ""
    return (prefix + body).strip()


def main() -> None:
    catalog = json.loads(CAT.read_text(encoding="utf-8"))
    commodities = json.loads(COMM.read_text(encoding="utf-8"))
    added = 0
    for rec in catalog.values():
        gb = rec.get("gb") or ""
        for part in re.split(r"[；;]", gb):
            name = CODE_RE.sub("", part).replace(" ", "").strip()
            if len(name) < 2 or name in commodities:
                continue
            en = guess_en(name, commodities)
            if en:
                commodities[name] = en
                added += 1
    leftover_words: dict[str, int] = {}
    for item, rec in catalog.items():
        en = translate_scope_item(item)
        rec["en"] = en
        for word in leftover_cjk(en):
            leftover_words[word] = leftover_words.get(word, 0) + 1
            if word not in commodities and 2 <= len(word) <= 12:
                guessed = guess_en(word, commodities)
                if guessed:
                    commodities[word] = guessed
                    added += 1
    COMM.write_text(json.dumps(commodities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # second pass after new nouns
    from license_trans.translate import _load
    _load.cache_clear()
    still = 0
    clean = 0
    for item, rec in catalog.items():
        en = translate_scope_item(item)
        if leftover_cjk(en):
            rec.pop("en", None)
            still += 1
        else:
            rec["en"] = en
            clean += 1
    CAT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"catalog={len(catalog)} commodities+={added} clean_en={clean} still_cjk_items={still}")
    top = sorted(leftover_words.items(), key=lambda kv: -kv[1])[:25]
    for word, n in top:
        print(f"  {n:4d}  {word}")


if __name__ == "__main__":
    main()
