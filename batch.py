"""Convert Chinese business-license photos into a fixed English PDF template."""

from __future__ import annotations

import argparse
from pathlib import Path

from license_trans.classify import NotFormatA
from license_trans.ocr import LicenseOCR
from license_trans.pipeline import convert_image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _iter_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files: list[Path] = []
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix.lower() in IMAGE_EXTS:
            files.append(child)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="中国营业执照照片 → 英文固定模板 PDF")
    parser.add_argument("input", help="图片文件或目录")
    parser.add_argument("-o", "--out", default="out", help="输出 PDF 目录（输入为文件时可直接给 pdf 路径）")
    parser.add_argument("--work", default="work", help="抽取 JSON 与纠偏预览目录")
    parser.add_argument("--config", default=None, help="config.json 路径")
    parser.add_argument("--issue-date", default=None, help="发证日期，如 2022-12-09，用于印章日期 OCR 失败时")
    args = parser.parse_args()

    src = Path(args.input)
    inputs = _iter_inputs(src)
    if not inputs:
        raise SystemExit(f"没有找到图片: {src}")

    out_arg = Path(args.out)
    failed = 0
    engine = LicenseOCR()
    for image in inputs:
        if out_arg.suffix.lower() == ".pdf" and len(inputs) == 1:
            output = out_arg
        else:
            output = out_arg / (image.stem + ".pdf")
        print(f"→ {image.name}")
        try:
            info = convert_image(
                image,
                output,
                config_path=args.config,
                work_dir=args.work,
                ocr=engine,
                issue_date=args.issue_date,
            )
        except NotFormatA as exc:
            failed += 1
            print(f"  不是 A 格式公司执照: {exc}")
            continue
        warnings = info["fields"].get("warnings") or []
        extra = info["english"].get("warnings") or []
        print(f"  PDF  {output}")
        shown = list(dict.fromkeys(warnings + extra))
        if shown:
            print(f"  需核对字段: {', '.join(shown)}")
        else:
            print("  字段完整")
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
