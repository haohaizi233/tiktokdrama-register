# -*- coding: utf-8 -*-
"""Download china_area CSV and extract township-level (街道/镇/乡) divisions."""
from __future__ import annotations

import csv
import gzip
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "glossary" / "raw_town.json"
RAW = ROOT / "license_work" / "area_code_2024.csv.gz"

URLS = [
    "https://raw.githubusercontent.com/adyliu/china_area/master/area_code_2024.csv.gz",
    "https://gh-proxy.org/https://raw.githubusercontent.com/adyliu/china_area/master/area_code_2024.csv.gz",
    "https://ghproxy.net/https://raw.githubusercontent.com/adyliu/china_area/master/area_code_2024.csv.gz",
]

EXCEPTIONS = {
    "呼和浩特": "Hohhot",
    "乌鲁木齐": "Urumqi",
    "拉萨": "Lhasa",
}


def pinyin(text: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
        parts = lazy_pinyin(text, style=Style.NORMAL)
        joined = "".join(parts)
        return joined[:1].upper() + joined[1:] if joined else text
    except Exception:
        return text


def english(name: str) -> str:
    if name.endswith("街道办事处"):
        core = name[:-5]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Subdistrict"
    if name.endswith("街道"):
        core = name[:-2]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Subdistrict"
    if name.endswith("民族乡"):
        core = name[:-3]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Ethnic Township"
    if name.endswith("乡"):
        core = name[:-1]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Township"
    if name.endswith("镇"):
        core = name[:-1]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Town"
    if name.endswith("苏木"):
        core = name[:-2]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Sum"
    if name.endswith("民族苏木"):
        core = name[:-4]
        return f"{EXCEPTIONS.get(core, pinyin(core))} Ethnic Sum"
    return pinyin(name)


def core_of(name: str) -> str | None:
    for suffix in ("街道办事处", "街道", "民族苏木", "民族乡", "苏木", "乡", "镇"):
        if name.endswith(suffix) and len(name) > len(suffix):
            c = name[:-len(suffix)]
            if len(c) >= 2:
                return c
    return None


def main() -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    if not RAW.exists() or RAW.stat().st_size < 1000:
        for url in URLS:
            try:
                print(f"downloading {url}", flush=True)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    RAW.write_bytes(resp.read())
                print(f"  got {RAW.stat().st_size} bytes", flush=True)
                break
            except Exception as exc:
                print(f"  fail {exc}", flush=True)
    if not RAW.exists() or RAW.stat().st_size < 1000:
        raise SystemExit("download failed")

    rows: list[dict] = []
    seen: set[str] = set()
    with gzip.open(RAW, "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for rec in reader:
            if len(rec) < 5:
                continue
            level = int(rec[2] or 0)
            if level != 4:
                continue
            name = rec[1].strip()
            code = rec[0].strip()
            pcode = rec[3].strip()
            if not name or not code or name in seen:
                continue
            seen.add(name)
            rows.append({"name": name, "code": code, "pcode": pcode, "en": english(name)})

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"wrote {OUT} towns={len(rows)}")
    kinds = {}
    for r in rows:
        k = "other"
        for s in ("街道办事处", "街道", "民族苏木", "民族乡", "苏木", "乡", "镇"):
            if r["name"].endswith(s):
                k = s
                break
        kinds[k] = kinds.get(k, 0) + 1
    print(kinds)


if __name__ == "__main__":
    main()
