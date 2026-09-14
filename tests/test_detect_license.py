"""Tests for license detection (营业执照 keyword check)."""
from license_trans.ocr import OcrLine
from license_trans.pipeline import detect_license


def _line(text):
    return OcrLine(text=text, x0=0, y0=0, x1=1, y1=1, score=0.9)


def test_detect_license_has_keyword():
    lines = [_line("营业执照"), _line("统一社会信用代码")]
    assert detect_license(lines) is True


def test_detect_license_keyword_with_spaces():
    lines = [_line("营业 执照"), _line("名称: 某公司")]
    assert detect_license(lines) is True


def test_detect_license_no_keyword():
    lines = [_line("身份证"), _line("姓名 张三")]
    assert detect_license(lines) is False


def test_detect_license_empty():
    assert detect_license([]) is False


def test_detect_license_partial_keyword():
    lines = [_line("营业"), _line("执照")]
    # Split across lines but joined — should still detect
    assert detect_license(lines) is True
