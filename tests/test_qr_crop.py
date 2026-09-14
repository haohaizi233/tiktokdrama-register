"""Tests for multi-strategy QR code detection."""
import numpy as np
from unittest.mock import patch, MagicMock

from license_trans.pipeline import (
    _detect_qr_points, _crop_from_points, _enhance_for_qr,
    _find_qr_by_contour, _crop_qr,
)


def _fake_img(h=600, w=800):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_detect_qr_points_none():
    img = _fake_img()
    with patch("cv2.QRCodeDetector") as mock_cls:
        mock_det = MagicMock()
        mock_cls.return_value = mock_det
        mock_det.detectAndDecode.return_value = ("", None, None)
        result = _detect_qr_points(img)
        assert result is None


def test_detect_qr_points_found():
    img = _fake_img()
    pts = np.array([[100, 100], [200, 100], [200, 200], [100, 200]], dtype=np.float32)
    with patch("cv2.QRCodeDetector") as mock_cls:
        mock_det = MagicMock()
        mock_cls.return_value = mock_det
        mock_det.detectAndDecode.return_value = ("data", pts.reshape(1, 4, 2), None)
        result = _detect_qr_points(img)
        assert result is not None
        assert result.shape == (4, 2)


def test_crop_from_points_valid():
    img = _fake_img(600, 800)
    pts = np.array([[100, 100], [200, 100], [200, 200], [100, 200]], dtype=np.float32)
    result = _crop_from_points(img, pts, 600, 800)
    assert result is not None
    assert len(result) > 0


def test_crop_from_points_too_small():
    img = _fake_img(600, 800)
    pts = np.array([[10, 10], [15, 10], [15, 15], [10, 15]], dtype=np.float32)
    result = _crop_from_points(img, pts, 600, 800)
    assert result is None


def test_crop_from_points_too_large():
    img = _fake_img(600, 800)
    pts = np.array([[0, 0], [600, 0], [600, 600], [0, 600]], dtype=np.float32)
    result = _crop_from_points(img, pts, 600, 800)
    assert result is None


def test_enhance_for_qr_returns_bgr():
    img = _fake_img(100, 100)
    result = _enhance_for_qr(img)
    assert result.shape == (100, 100, 3)


def test_find_qr_by_contour_empty_image():
    img = _fake_img(600, 800)
    result = _find_qr_by_contour(img)
    assert result is None  # No contours in blank image


def test_crop_qr_multi_strategy():
    img = _fake_img(600, 800)
    pts = np.array([[100, 100], [200, 100], [200, 200], [100, 200]], dtype=np.float32)
    with patch("license_trans.pipeline._detect_qr_points", return_value=pts):
        result = _crop_qr(img, [])
        assert result is not None
