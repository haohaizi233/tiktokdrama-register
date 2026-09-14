"""Tests for QR USCI extraction and layout auto-detection."""
from license_trans.pipeline import _extract_usci_from_qr
from license_trans.parse import _detect_layout
from license_trans.ocr import OcrLine


def _line(text, score=0.95, x0=0, y0=0, x1=200, y1=30):
    return OcrLine(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


def test_extract_usci_from_gsxt_url():
    qr = "https://www.gsxt.gov.cn/corp-query-entprise-info-verify-91110108MA01ABCDEF.html"
    assert _extract_usci_from_qr(qr) == "91110108MA01ABCDEF"


def test_extract_usci_from_raw_code():
    qr = "91110108MA01ABCDEF"
    assert _extract_usci_from_qr(qr) == "91110108MA01ABCDEF"


def test_extract_usci_empty():
    assert _extract_usci_from_qr("") == ""


def test_extract_usci_no_match():
    assert _extract_usci_from_qr("hello world") == ""


def test_detect_layout_empty():
    layout = _detect_layout([])
    assert layout["line_h"] == 18.0
    assert layout["bucket"] == 18


def test_detect_layout_small_lines():
    lines = [
        _line("a", y0=0, y1=15),
        _line("b", y0=20, y1=35),
        _line("c", y0=40, y1=55),
    ]
    layout = _detect_layout(lines)
    assert layout["line_h"] == 15.0
    assert layout["bucket"] == 15
    assert layout["max_dy_scope"] == 15.0 * 12
    assert layout["max_dy_field"] == 15.0 * 5


def test_detect_layout_large_lines():
    lines = [
        _line("a", y0=0, y1=40),
        _line("b", y0=50, y1=90),
        _line("c", y0=100, y1=140),
    ]
    layout = _detect_layout(lines)
    assert layout["line_h"] == 40.0
    assert layout["bucket"] == 40
    assert layout["max_dy_scope"] == 40.0 * 12
    assert layout["max_dy_field"] == 40.0 * 5
