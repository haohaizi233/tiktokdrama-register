# -*- coding: utf-8 -*-
"""从 中国执照库 收割经营范围剩余中文, 便于补语库.

每家最多试 10 张图; 已有记录会先重译, 范围空/垃圾的会换图再 OCR.
用法:
  python harvest_glossary.py
  python harvest_glossary.py --limit 5
  python harvest_glossary.py --ocr-all
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "中国执照库"
OUT = ROOT / "license_work" / "harvest.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
JUNK = ("有限责任公司", "自然人独资", "自然人投资", "居民身份证")


def _is_license_blob(blob: str) -> bool:
    if "居民身份证" in blob and "营业执照" not in blob:
        return False
    has_id = "统一社会" in blob or "信用代码" in blob
    has_scope = "经营范围" in blob or "一般项目" in blob or "许可项目" in blob
    has_title = "营业执照" in blob
    return (has_title and has_id) or (has_id and has_scope) or (has_title and has_scope)


def _folder_images(folder: Path, skip_name: str = "") -> list[Path]:
    files = [
        child for child in folder.iterdir()
        if child.is_file() and child.suffix.lower() in IMAGE_EXTS and child.stat().st_size > 40_000
    ]
    files.sort(key=lambda p: (
        1 if skip_name and p.name == skip_name else 0,
        0 if p.suffix.lower() == ".png" else 1,
        -p.stat().st_size,
        p.name,
    ))
    return files[:10]


def _is_weak(rec: dict) -> bool:
    scope = rec.get("scope") or ""
    leftover = rec.get("leftover") or []
    if len(scope) < 24:
        return True
    blob = scope + "".join(leftover)
    if any(mark in blob for mark in JUNK) and "一般项目" not in scope and "许可项目" not in scope:
        return True
    if len(leftover) >= 6:
        return True
    return False


def _save(records: list[dict], leftover: Counter) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "files": len(records),
        "leftover": leftover.most_common(),
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _leftover_from_records(records: list[dict]) -> Counter:
    leftover: Counter[str] = Counter()
    for rec in records:
        leftover.update(rec.get("leftover") or [])
    return leftover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ocr-all", action="store_true", help="忽略已收记录, 全部重 OCR")
    args = parser.parse_args()

    from license_trans.ocr import LicenseOCR
    from license_trans.parse import parse_license
    from license_trans.translate import leftover_cjk, translate_company, translate_scope

    folders = sorted(p for p in CORPUS.iterdir() if p.is_dir())
    if args.limit:
        folders = folders[: args.limit]

    records: list[dict] = []
    skip_files: dict[str, str] = {}
    if OUT.exists() and not args.ocr_all:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        records = prev.get("records") or []
        print(f"已有 {len(records)} 家, 先用当前语库重译…", flush=True)
        kept: list[dict] = []
        for rec in records:
            rec["scope_en"] = translate_scope(rec.get("scope") or "")
            rec["name_en"] = translate_company(rec.get("name") or "")
            rec["leftover"] = leftover_cjk(rec["scope_en"])
            if _is_weak(rec):
                skip_files[rec["folder"]] = rec.get("file") or ""
                print(f"  弱记录待换图 {rec['folder']}", flush=True)
                continue
            kept.append(rec)
        records = kept
        leftover = _leftover_from_records(records)
        _save(records, leftover)
        print("重译剩余中文:", leftover.most_common(20), flush=True)

    done_folders = {r["folder"] for r in records}
    pending = [folder for folder in folders if folder.name not in done_folders]
    print(f"待 OCR {len(pending)} 家", flush=True)
    if not pending:
        leftover = _leftover_from_records(records)
        print(f"写出 {OUT}", flush=True)
        for word, n in leftover.most_common(50):
            print(f"  {n:3d}  {word}", flush=True)
        return 0

    print("加载 OCR…", flush=True)
    engine = LicenseOCR()
    leftover = _leftover_from_records(records)
    for folder in pending:
        picked = None
        fields = None
        for path in _folder_images(folder, skip_files.get(folder.name, "")):
            print(f"  OCR {folder.name}/{path.name}", flush=True)
            _oriented, lines = engine.recognize(path)
            blob = "".join(ln.text for ln in lines)
            if not _is_license_blob(blob):
                print(f"  skip 非执照 {folder.name}/{path.name}", flush=True)
                continue
            fields = parse_license(lines)
            if len(fields.scope or "") < 24 and "一般项目" not in (fields.scope or "") and "许可项目" not in (fields.scope or ""):
                print(f"  skip 范围空 {folder.name}/{path.name}", flush=True)
                continue
            picked = path
            break
        if picked is None or fields is None:
            print(f"  skip 无执照图 {folder.name}", flush=True)
            continue
        scope_en = translate_scope(fields.scope)
        name_en = translate_company(fields.name)
        bits = leftover_cjk(scope_en)
        leftover.update(bits)
        records.append({
            "folder": folder.name,
            "file": picked.name,
            "name": fields.name,
            "name_en": name_en,
            "scope": fields.scope,
            "scope_en": scope_en,
            "leftover": bits,
            "warnings": fields.warnings,
        })
        print(f"  ok {fields.name or folder.name} leftover={bits}", flush=True)
        leftover = _leftover_from_records(records)
        _save(records, leftover)

    leftover = _leftover_from_records(records)
    _save(records, leftover)
    print(f"写出 {OUT}", flush=True)
    print("剩余中文(需补语库):", flush=True)
    for word, n in leftover.most_common(50):
        print(f"  {n:3d}  {word}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
