"""Tests for quick_detect, _pick_best_license, and _ensure_min_size."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np

from license_trans.ocr import OcrLine, _ensure_min_size
from license_trans.pipeline import _pick_best_license


def _line(text, score=0.9):
    return OcrLine(text=text, score=score, x0=0, y0=0, x1=1, y1=1)


def _tmp_file(size: int = 100) -> Path:
    fd, path = tempfile.mkstemp(suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(b"\xff" * size)
    return Path(path)


def test_pick_best_license_most_lines():
    img_a = _tmp_file(100)
    img_b = _tmp_file(50)
    try:
        candidates = [(img_a, [_line("x")]), (img_b, [_line("x"), _line("y"), _line("z")])]
        best, lines = _pick_best_license(candidates)
        assert best == img_b
        assert len(lines) == 3
    finally:
        img_a.unlink(missing_ok=True)
        img_b.unlink(missing_ok=True)


def test_pick_best_license_tiebreak_filesize():
    img_a = _tmp_file(100)
    img_b = _tmp_file(500)
    try:
        candidates = [(img_a, [_line("x"), _line("y")]), (img_b, [_line("x"), _line("y")])]
        best, _ = _pick_best_license(candidates)
        assert best == img_b  # same line count, larger file wins
    finally:
        img_a.unlink(missing_ok=True)
        img_b.unlink(missing_ok=True)


def test_quick_detect_license_found():
    from license_trans.ocr import LicenseOCR
    import threading
    engine = LicenseOCR.__new__(LicenseOCR)
    engine._ocr = MagicMock()
    engine._quick_cache = {}
    engine._cache_lock = threading.Lock()
    tmp = _tmp_file()
    try:
        with patch("license_trans.ocr._read_image", return_value=MagicMock(shape=(100, 100, 3))):
            with patch("license_trans.ocr._downscale", return_value=MagicMock(shape=(100, 100, 3))):
                with patch("license_trans.ocr._ensure_min_size", return_value=MagicMock(shape=(100, 100, 3))):
                    with patch("license_trans.ocr._run_ocr", return_value=[_line("营业执照")]):
                        is_lic, lines = engine.quick_detect(tmp)
                        assert is_lic is True
                        assert len(lines) == 1
    finally:
        tmp.unlink(missing_ok=True)


def test_quick_detect_not_license():
    from license_trans.ocr import LicenseOCR
    import threading
    engine = LicenseOCR.__new__(LicenseOCR)
    engine._ocr = MagicMock()
    engine._quick_cache = {}
    engine._cache_lock = threading.Lock()
    tmp = _tmp_file()
    try:
        with patch("license_trans.ocr._read_image", return_value=MagicMock(shape=(100, 100, 3))):
            with patch("license_trans.ocr._downscale", return_value=MagicMock(shape=(100, 100, 3))):
                with patch("license_trans.ocr._ensure_min_size", return_value=MagicMock(shape=(100, 100, 3))):
                    with patch("license_trans.ocr._run_ocr", return_value=[_line("身份证")]):
                        is_lic, lines = engine.quick_detect(tmp)
                        assert is_lic is False
    finally:
        tmp.unlink(missing_ok=True)


def test_ensure_min_size_upscales():
    small = np.zeros((400, 300, 3), dtype=np.uint8)
    result = _ensure_min_size(small, min_side=1000)
    assert max(result.shape[:2]) >= 1000


def test_ensure_min_size_no_upscale():
    large = np.zeros((1200, 900, 3), dtype=np.uint8)
    result = _ensure_min_size(large, min_side=1000)
    assert result.shape == large.shape


def test_quick_detect_caches_lines():
    """quick_detect should cache lines so recognize() can reuse them."""
    from license_trans.ocr import LicenseOCR
    import threading
    engine = LicenseOCR.__new__(LicenseOCR)
    engine._ocr = MagicMock()
    engine._quick_cache = {}
    engine._cache_lock = threading.Lock()
    tmp = _tmp_file()
    try:
        with patch("license_trans.ocr._read_image", return_value=MagicMock(shape=(100, 100, 3))):
            with patch("license_trans.ocr._downscale", return_value=MagicMock(shape=(100, 100, 3))):
                with patch("license_trans.ocr._ensure_min_size", return_value=MagicMock(shape=(100, 100, 3))):
                    with patch("license_trans.ocr._run_ocr", return_value=[_line("营业执照")]):
                        engine.quick_detect(tmp)
                        key = str(tmp.resolve())
                        assert key in engine._quick_cache
                        assert len(engine._quick_cache[key]) == 1
    finally:
        tmp.unlink(missing_ok=True)


def test_recognize_reuses_cached_lines():
    """recognize() should pop cached lines and skip k=0 OCR pass."""
    from license_trans.ocr import LicenseOCR
    import threading
    engine = LicenseOCR.__new__(LicenseOCR)
    engine._ocr = MagicMock()
    engine._quick_cache = {}
    engine._cache_lock = threading.Lock()
    tmp = _tmp_file()
    try:
        cached = [_line("营业执照"), _line("统一社会信用代码")]
        engine._quick_cache[str(tmp.resolve())] = cached
        call_count = {"n": 0}
        real_img = np.zeros((100, 100, 3), dtype=np.uint8)

        def mock_run_ocr(ocr, image):
            call_count["n"] += 1
            return [_line("营业执照"), _line("经营范围")]

        with patch("license_trans.ocr._read_image", return_value=real_img):
            with patch("license_trans.ocr._downscale", side_effect=lambda x, **kw: x):
                with patch("license_trans.ocr._ensure_min_size", side_effect=lambda x, **kw: x):
                    with patch("license_trans.ocr._run_ocr", side_effect=mock_run_ocr):
                        with patch("license_trans.ocr._deskew", side_effect=lambda x: x):
                            with patch("license_trans.ocr._correct_perspective", side_effect=lambda x: x):
                                with patch("license_trans.ocr._enhance_and_clean", side_effect=lambda x: x):
                                    oriented, lines = engine.recognize(tmp)
                                    # Cached lines had hits >= 5? No (only 2 keywords).
                                    # So rotations 1-3 are tried (3 OCR calls)
                                    # + 1 for enhance_and_clean = 4 total
                                    # If no cache, it would be 4 (rotations) + 1 = 5
                                    assert call_count["n"] <= 4
    finally:
        tmp.unlink(missing_ok=True)


def test_recognize_skips_destain_when_page_already_readable():
    from license_trans.ocr import KEYWORDS, LicenseOCR, OcrLine
    import threading

    engine = LicenseOCR.__new__(LicenseOCR)
    engine._ocr = MagicMock()
    engine._quick_cache = {}
    engine._cache_lock = threading.Lock()
    tmp = _tmp_file()
    lines = [
        OcrLine(text=k, score=0.9, x0=10, y0=20 * i, x1=200, y1=20 * i + 16)
        for i, k in enumerate(KEYWORDS)
    ]
    lines.extend(
        OcrLine(text=f"销售项目{i}", score=0.9, x0=10, y0=200 + i * 16, x1=180, y1=214 + i * 16)
        for i in range(14)
    )
    engine._quick_cache[str(tmp.resolve())] = lines
    destain_calls = {"n": 0}
    real_img = np.zeros((400, 600, 3), dtype=np.uint8)

    def mock_run_ocr(ocr, image):
        destain_calls["n"] += 1
        raise AssertionError("destain OCR should be skipped")

    try:
        with patch("license_trans.ocr._read_image", return_value=real_img):
            with patch("license_trans.ocr._downscale", side_effect=lambda x, **kw: x):
                with patch("license_trans.ocr._ensure_min_size", side_effect=lambda x, **kw: x):
                    with patch("license_trans.ocr._run_ocr", side_effect=mock_run_ocr):
                        with patch("license_trans.ocr._deskew", side_effect=lambda x: x):
                            with patch("license_trans.ocr._correct_perspective", side_effect=lambda x: x):
                                with patch("license_trans.ocr._zoom_crop_pass", return_value=None):
                                    oriented, got = engine.recognize(tmp)
        assert destain_calls["n"] == 0
        assert len(got) >= 18
    finally:
        tmp.unlink(missing_ok=True)
