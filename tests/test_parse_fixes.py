"""Regression tests from real mis-parsed sidecars (2026-09-13 batch).

Cases: 北京鑫翠 (label-less layout, row-association drift),
兰州忠洁 (vertical layout, OCR label typo), 亳州青美 (bucket-sort drift).
"""
import json
from pathlib import Path

import pytest

from license_trans.ocr import OcrLine
from license_trans.parse import parse_license
from license_trans.divisions import snap_text

SIDE_DIR = Path(r"D:\desktop\注册\license_work")

CASES = {
    "mmexport1731142085696英文版.json": {  # 北京鑫翠
        "name": "北京鑫翠奇缘珠宝有限责任公司",
        "legal_person": "王生武",
        "capital": "40万元",
        "type": "有限责任公司（自然人独资）",
    },
    "mmexport1731117103734英文版.json": {  # 兰州忠洁
        "name": "兰州忠洁卫士家政服务有限公司",
        "legal_person": "王忠太",
        "capital": "伍拾万元整",
    },
    "mmexport1731138234111英文版.json": {  # 亳州青美
        "name": "亳州市青美酒业有限公司",
        "legal_person": "孟瑞青",
        "capital": "壹仟万圆整",
        "type": "有限责任公司（自然人独资）",
    },
    "Image_531862255261427英文版.json": {  # 横峰育民: 竖排标签丢首字 营业期限→业期限
        "term": "2021年03月24日至长期",
    },
}


def _load_lines(fn: str) -> list[OcrLine]:
    p = SIDE_DIR / fn
    if not p.exists():
        pytest.skip(f"sidecar not present: {fn}")
    d = json.loads(p.read_text(encoding="utf-8"))
    return [OcrLine(**{k: L[k] for k in ("text", "score", "x0", "y0", "x1", "y1")}) for L in d["ocr"]]


@pytest.mark.parametrize("fn", sorted(CASES), ids=["bozhou", "hengfeng", "lanzhou", "beijing"])
def test_real_sidecar_fields(fn):
    expect = CASES[fn]
    fields = parse_license(_load_lines(fn))
    for attr, want in expect.items():
        got = getattr(fields, attr)
        assert got == want, f"{fn} {attr}: got {got!r}, want {want!r}"


def test_name_fallback_regex_llc():
    """LLC names end with 责任公司, not 有限公司 — fallback must match."""
    import re
    from license_trans.parse import _norm
    pat = re.compile(r"(有限责任公司|股份有限公司|有限公司|合伙企业)$")
    assert pat.search(_norm("北京鑫翠奇缘珠宝有限责任公司"))
    assert pat.search(_norm("XX股份有限公司"))
    assert pat.search(_norm("XX有限公司"))
    assert not pat.search(_norm("有限责任公司（自然人独资）"))


def test_snap_text_keep_len():
    """Name snapping must fix OCR typos (equal-length) but never insert chars."""
    assert snap_text("兰州忠洁卫士家政服务有限公司", ["甘肃省", "兰州市", "安宁区"], keep_len=True) == "兰州忠洁卫士家政服务有限公司"
    assert snap_text("亮州市青美酒业有限公司", ["安徽省", "亳州市"], keep_len=True) == "亳州市青美酒业有限公司"
    assert snap_text("北京鑫翠奇缘珠宝有限责任公司", ["北京市", "北京市东城区"], keep_len=True) == "北京鑫翠奇缘珠宝有限责任公司"


def test_parse_recovers_type_when_label_split_as_xing():
    lines = [
        OcrLine("营业执照", 0.9, 500, 40, 800, 90),
        OcrLine("统一社会信用代码", 0.9, 50, 100, 220, 130),
        OcrLine("91440783MA53XQRYXQ", 0.95, 50, 140, 280, 170),
        OcrLine("名称", 0.9, 50, 200, 120, 230),
        OcrLine("开平市森泉贸易有限公司", 0.9, 180, 200, 450, 230),
        OcrLine("类", 0.9, 50, 250, 80, 280),
        OcrLine("类型", 0.9, 50, 250, 160, 280),
        OcrLine("型", 0.99, 130, 250, 160, 280),
        OcrLine("有限责任公司(自然人独资)", 0.99, 180, 250, 480, 280),
        OcrLine("法定代表人", 0.9, 50, 300, 160, 330),
        OcrLine("陈海松", 0.9, 180, 300, 260, 330),
    ]
    fields = parse_license(lines)
    assert fields.type == "有限责任公司(自然人独资)"
    assert fields.name == "开平市森泉贸易有限公司"


def test_capital_label_ocr_typo():
    """主册资本 (OCR of 注册资本) must be recognized as capital label."""
    lines = [
        OcrLine(text="主册 资本伍拾万元整", score=0.74, x0=535, y0=477, x1=571, y1=507),
        OcrLine(text="法定代表人", score=0.83, x0=628, y0=1235, x1=661, y1=1265),
    ]
    f = parse_license(lines)
    assert f.capital == "伍拾万元整"


def test_label_first_char_dropped():
    """Vertical OCR often drops the first char of a label; matching must
    tolerate one missing leading char for 3+ char labels."""
    from license_trans.parse import _find_key_line, _value_after_key
    lines = [
        OcrLine(text="业期限 2021年03月24日至长期", score=0.89, x0=493, y0=936, x1=531, y1=1255),
        OcrLine(text="名称  横峰县育民建筑劳务服务有限公司", score=0.9, x0=100, y0=100, x1=300, y1=120),
    ]
    key = _find_key_line(lines, ("营业期限", "经营期限"))
    assert key is not None
    assert _value_after_key(key, ("营业期限", "经营期限")) == "2021年03月24日至长期"
    assert parse_license(lines).term == "2021年03月24日至长期"


def test_label_two_char_no_variant():
    """2-char labels must NOT get a dropped-char variant: '称XXX' must not
    become the name key line (one leftover char matches too much)."""
    from license_trans.parse import _find_key_line
    lines = [OcrLine(text="称横峰县育民建筑劳务服务有限公司", score=0.9, x0=100, y0=100, x1=300, y1=120)]
    assert _find_key_line(lines, ("名称", "名 称")) is None
    # exact still matches
    assert _find_key_line(lines[:0] or [OcrLine(text="名称 ABC", score=0.9, x0=0, y0=0, x1=1, y1=1)], ("名称",)) is not None
