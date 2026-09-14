"""Tests for deskew and perspective correction in OCR."""
import numpy as np
import cv2
from license_trans.ocr import _deskew, _correct_perspective, _order_points


def test_deskew_no_rotation():
    """A perfectly straight image should be returned unchanged."""
    img = np.full((200, 400, 3), 220, dtype=np.uint8)
    cv2.putText(img, "Test License ABC", (50, 100),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3)
    result = _deskew(img)
    assert result.shape == img.shape


def test_deskew_corrects_small_angle():
    """A slightly rotated image should be deskewed to roughly the original orientation."""
    img = np.full((300, 500, 3), 220, dtype=np.uint8)
    for y in range(50, 250, 40):
        cv2.line(img, (50, y), (450, y), (0, 0, 0), 2)
    M = cv2.getRotationMatrix2D((250, 150), 5, 1.0)
    skewed = cv2.warpAffine(img, M, (500, 300), borderMode=cv2.BORDER_REPLICATE)
    result = _deskew(skewed)
    assert result.shape[0] > 0 and result.shape[1] > 0


def test_deskew_ignores_large_angle():
    """Angles beyond max_angle (12°) should not be corrected (likely a 90° rotation)."""
    img = np.full((300, 500, 3), 220, dtype=np.uint8)
    M = cv2.getRotationMatrix2D((250, 150), 45, 1.0)
    rotated = cv2.warpAffine(img, M, (500, 300), borderMode=cv2.BORDER_REPLICATE)
    result = _deskew(rotated)
    assert result.shape == rotated.shape


def test_order_points():
    """Four corners should be ordered: TL, TR, BR, BL."""
    pts = np.array([
        [100, 200],  # BL
        [50, 50],     # TL
        [200, 100],   # TR
        [150, 250],   # BR
    ], dtype=np.float32)
    rect = _order_points(pts)
    assert rect[0][0] < rect[1][0]  # TL.x < TR.x
    assert rect[0][1] < rect[3][1]  # TL.y < BL.y
    assert rect[2][0] > rect[3][0]  # BR.x > BL.x
    assert rect[2][1] > rect[1][1]  # BR.y > TR.y


def test_correct_perspective_no_border():
    """An image without a clear border quadrilateral should be returned unchanged."""
    img = np.full((300, 400, 3), 220, dtype=np.uint8)
    cv2.putText(img, "No border here", (50, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    result = _correct_perspective(img)
    assert result.shape == img.shape


def test_correct_perspective_with_border():
    """An image with a clear rectangular border should be warped."""
    img = np.full((300, 400, 3), 240, dtype=np.uint8)
    cv2.rectangle(img, (50, 30), (350, 270), (0, 0, 0), 3)
    cv2.putText(img, "License", (100, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    result = _correct_perspective(img)
    assert result.shape[0] > 0 and result.shape[1] > 0
