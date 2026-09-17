"""Tests for WeChat OCR discovery and ensemble fallback."""
from unittest.mock import patch
import os
import tempfile

from license_trans.ocr_wechat import (
    discover_wechat_ocr, wechat_ocr_health_check, WeChatOCREngine,
)
from license_trans.ocr_ensemble import (
    OcrEnsemble, OcrCandidateFusion, OcrCandidate, FieldOcrCandidate,
)
from license_trans.ocr_service import DualOcrEngine


class FakeEngine:
    name = "fake"
    provider = "CPU"

    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result, 0.01


def test_health_check_not_found():
    with patch("license_trans.ocr_wechat.discover_wechat_ocr_paths", return_value=None):
        result = wechat_ocr_health_check(init=False)
    assert result["available"] is False
    assert result["ready"] is False
    assert result["initialized"] is False
    assert "not found" in (result["error"] or "")


def test_health_check_path_exists_is_not_ready():
    """Path.exists is not READY — initialized must be false without runtime init."""
    fake_path = tempfile.NamedTemporaryFile(suffix=".dll", delete=False)
    fake_path.close()
    try:
        cfg = {"wechat_ocr": {"ocr_path": fake_path.name, "wechat_path": os.path.dirname(fake_path.name)}}
        result = wechat_ocr_health_check(cfg, init=False)
        assert result["available"] is True
        assert result["ready"] is False
        assert result["initialized"] is False
    finally:
        os.unlink(fake_path.name)


def test_discover_with_explicit_config():
    with tempfile.NamedTemporaryFile(suffix=".dll", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cfg = {"wechat_ocr": {"ocr_path": tmp_path, "wechat_path": os.path.dirname(tmp_path)}}
        result = discover_wechat_ocr(cfg)
        assert result == tmp_path
    finally:
        os.unlink(tmp_path)


def test_wechat_engine_init_fails_when_missing():
    with patch("license_trans.ocr_wechat.discover_wechat_ocr_paths", return_value=None):
        raised = False
        try:
            WeChatOCREngine()
        except RuntimeError:
            raised = True
        assert raised


def test_ensemble_fallback_to_ppocr():
    pp = FakeEngine(result=[])
    ensemble = OcrEnsemble(wechat=None, ppocr=pp, init_engines=False)
    assert ensemble.primary_name == "ppocr_v6"
    assert ensemble.is_fallback_mode is True
    assert ensemble.wechat_available is False
    assert ensemble.ppocr_available is True
    status = ensemble.health_status()
    assert status["wechat"] == "UNAVAILABLE"
    assert status["primary"] == "ppocr_v6"
    assert status["fallback_mode"] is True


def test_fusion_no_candidates():
    fusion = OcrCandidateFusion()
    assert fusion.fuse([]) is None
    assert fusion.fuse_field("scope", []) is None


def test_fusion_highest_score_fallback():
    fusion = OcrCandidateFusion()
    candidates = [
        OcrCandidate(text="ABC", score=0.7, engine="ppocr_v6"),
        OcrCandidate(text="ABD", score=0.9, engine="wechat"),
    ]
    result = fusion.fuse(candidates)
    assert result is not None
    assert result.text == "ABD"
    assert result.score == 0.9


def test_dual_records_fallback_reason():
    wx = FakeEngine(result=[])
    pp = FakeEngine(result=[([[0, 0], [1, 0], [1, 1], [0, 1]], "ok", 0.8)])
    dual = DualOcrEngine(wx, pp)
    dual(None)
    summary = dual.summary()
    assert summary["fallback_used"] is True
    assert summary["reason"] == "wechat_empty"
    assert summary["fallback"] == "ppocr_v6"
