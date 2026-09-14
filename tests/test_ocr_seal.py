"""Tests for red seal removal and line merging in OCR."""
import numpy as np
import cv2
from license_trans.ocr import OcrLine, _remove_red_seal, _iou, _merge_lines


def _line(text, score, x0, y0, x1, y1):
    return OcrLine(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


def test_remove_red_seal_replaces_red_with_white():
    """Red pixels should be replaced with white; non-red pixels unchanged."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:] = (200, 200, 200)  # gray background
    img[20:40, 30:50] = (50, 50, 200)  # red patch (BGR)
    img[60:80, 60:80] = (200, 200, 50)  # blue patch (BGR)
    cleaned = _remove_red_seal(img)
    assert cleaned[30, 40, 0] == 255 and cleaned[30, 40, 1] == 255 and cleaned[30, 40, 2] == 255
    assert cleaned[70, 70, 0] == 200 and cleaned[70, 70, 1] == 200 and cleaned[70, 70, 2] == 50


def test_iou_identical_lines():
    a = _line("test", 0.9, 0, 0, 100, 30)
    assert _iou(a, a) == 1.0


def test_iou_non_overlapping():
    a = _line("a", 0.9, 0, 0, 100, 30)
    b = _line("b", 0.9, 200, 200, 300, 230)
    assert _iou(a, b) == 0.0


def test_merge_replaces_lower_score():
    primary = [_line("某公司", 0.3, 0, 0, 200, 30)]
    secondary = [_line("某公司有限公司", 0.8, 0, 0, 200, 30)]
    merged = _merge_lines(primary, secondary)
    assert len(merged) == 1
    assert merged[0].text == "某公司有限公司"
    assert merged[0].score == 0.8


def test_merge_keeps_better_primary():
    primary = [_line("某公司有限公司", 0.9, 0, 0, 200, 30)]
    secondary = [_line("某公司", 0.3, 0, 0, 200, 30)]
    merged = _merge_lines(primary, secondary)
    assert len(merged) == 1
    assert merged[0].text == "某公司有限公司"
    assert merged[0].score == 0.9


def test_merge_adds_new_lines():
    primary = [_line("a", 0.9, 0, 0, 100, 30)]
    secondary = [_line("b", 0.9, 200, 200, 300, 230)]
    merged = _merge_lines(primary, secondary)
    assert len(merged) == 2
    texts = {m.text for m in merged}
    assert texts == {"a", "b"}


def test_merge_keeps_shorter_higher_score():
    """If secondary has higher score but shorter text, keep primary (don't replace with garbage)."""
    primary = [_line("北京测试科技有限公司", 0.5, 0, 0, 200, 30)]
    secondary = [_line("北", 0.9, 0, 0, 200, 30)]
    merged = _merge_lines(primary, secondary)
    assert merged[0].text == "北京测试科技有限公司"
