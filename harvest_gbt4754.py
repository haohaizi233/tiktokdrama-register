# -*- coding: utf-8 -*-
"""Parse GB/T 4754-2017 names and merge into commodities.json."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SQL = ROOT / "license_work" / "gbt4754.sql"
COMM = ROOT / "glossary" / "commodities.json"

NAME_RE = re.compile(
    r"values\s*\(\s*\d+\s*,\s*'[^']*'\s*,\s*'((?:\\'|[^'])*)'",
    re.I,
)

# Longer suffixes first. Used only for leftover CJK after existing glossary match.
SUFFIXES = [
    ("专业及辅助性活动", "professional and supporting activities"),
    ("管理活动", "management activities"),
    ("综合利用", "comprehensive utilization"),
    ("加工处理", "processing"),
    ("修理与维护", "repair and maintenance"),
    ("修理", "repair"),
    ("维护", "maintenance"),
    ("种植", "planting"),
    ("栽培", "cultivation"),
    ("养殖", "breeding"),
    ("饲养", "raising"),
    ("捕捞", "fishing"),
    ("采运", "logging and transport"),
    ("开采", "mining"),
    ("采选", "mining and dressing"),
    ("洗选", "washing and dressing"),
    ("冶炼", "smelting"),
    ("压延加工", "rolling and processing"),
    ("制造", "manufacture"),
    ("生产", "production"),
    ("加工", "processing"),
    ("批发", "wholesale"),
    ("零售", "retail"),
    ("租赁", "leasing"),
    ("运输", "transportation"),
    ("仓储", "warehousing"),
    ("装卸", "loading and unloading"),
    ("施工", "construction"),
    ("工程", "engineering"),
    ("设计", "design"),
    ("咨询", "consulting"),
    ("研发", "R&D"),
    ("研究", "research"),
    ("开发", "development"),
    ("服务", "services"),
    ("活动", "activities"),
    ("制品", "products"),
    ("设备", "equipment"),
    ("器材", "equipment"),
    ("材料", "materials"),
    ("产品", "products"),
    ("行业", "industry"),
    ("产业", "industry"),
]


def parse_names(sql: str) -> list[str]:
    names = []
    for match in NAME_RE.finditer(sql):
        name = match.group(1).replace("\\'", "'").replace(" ", "")
        if name:
            names.append(name)
    return names


def english_for(name: str, table: dict[str, str]) -> str:
    if name in table:
        return table[name]
    raw = name
    prefix = ""
    if raw.startswith("其他未列明"):
        prefix, raw = "other unspecified ", raw[5:]
    elif raw.startswith("未列明"):
        prefix, raw = "unspecified ", raw[3:]
    elif raw.startswith("其他"):
        prefix, raw = "other ", raw[2:]
    if raw in table:
        return (prefix + table[raw]).strip()
    # longest-match with current table + suffixes
    merged = {**dict(SUFFIXES), **table}
    keys = sorted(merged, key=len, reverse=True)
    i = 0
    bits: list[str] = []
    leftover = ""
    while i < len(raw):
        hit = next((k for k in keys if raw.startswith(k, i) and len(k) >= 2), None)
        if hit:
            if leftover:
                bits.append(leftover)
                leftover = ""
            bits.append(merged[hit])
            i += len(hit)
        elif raw[i] in "、，,及和与":
            if leftover:
                bits.append(leftover)
                leftover = ""
            i += 1
        else:
            leftover += raw[i]
            i += 1
    if leftover:
        bits.append(leftover)
    body = " ".join(bits).strip()
    body = re.sub(r"\s+", " ", body)
    return (prefix + body).strip() or name


def main() -> None:
    sql = SQL.read_text(encoding="utf-8")
    names = parse_names(sql)
    table = json.loads(COMM.read_text(encoding="utf-8"))
    added = 0
    still = []
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for name in names:
        if name in table:
            continue
        en = english_for(name, table)
        table[name] = en
        added += 1
        if cjk.search(en):
            still.append((name, en))
    COMM.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"industry names={len(names)} added={added} still_cjk={len(still)}")
    for name, en in still[:40]:
        print(f"  {name} => {en}")


if __name__ == "__main__":
    main()
