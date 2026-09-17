"""Tests for OcrService WeChat → PP-OCR fallback (no real RapidOCR required)."""
from __future__ import annotations

import numpy as np

from license_trans.ocr_service import DualOcrEngine, OcrService
from license_trans.ocr_wechat import WeChatOcrTimeout, is_valid_ocr_result

BOX = [[0, 0], [10, 0], [10, 10], [0, 10]]
VALID = [(BOX, "营业执照", 0.91)]
PPOCR = [(BOX, "统一社会信用代码", 0.88)]


class FakeEngine:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.name = "fake"
        self.provider = "CPU"

    def __call__(self, image):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result, 0.01


def _img():
    return np.zeros((32, 32, 3), dtype=np.uint8)


def test_wechat_success_does_not_call_ppocr():
    wx = FakeEngine(result=VALID)
    pp = FakeEngine(result=PPOCR)
    svc = OcrService(wechat=wx, ppocr=pp, init_engines=False)
    result, _ = svc.run(_img())
    assert result == VALID
    assert wx.calls == 1
    assert pp.calls == 0
    assert svc.dual.summary()["ocr_actual"] == "wechat"
    assert svc.dual.summary()["fallback_used"] is False


def test_wechat_init_failed_uses_ppocr():
    pp = FakeEngine(result=PPOCR)
    svc = OcrService(wechat=None, ppocr=pp, init_engines=False)
    result, _ = svc.run(_img())
    assert result == PPOCR
    assert pp.calls == 1
    summary = svc.dual.summary()
    assert summary["ocr_actual"] == "ppocr_v6"
    assert summary["fallback_used"] is True
    assert summary["reason"] == "wechat_unavailable"


def test_wechat_runtime_error_falls_back():
    wx = FakeEngine(error=RuntimeError("dll crash"))
    pp = FakeEngine(result=PPOCR)
    svc = OcrService(wechat=wx, ppocr=pp, init_engines=False)
    result, _ = svc.run(_img())
    assert result == PPOCR
    assert wx.calls == 1
    assert pp.calls == 1
    assert svc.dual.summary()["reason"] == "wechat_runtime_failed"
    # sticky: second call should not hit wechat
    svc.run(_img())
    assert wx.calls == 1
    assert pp.calls == 2


def test_wechat_empty_falls_back():
    wx = FakeEngine(result=[])
    pp = FakeEngine(result=PPOCR)
    svc = OcrService(wechat=wx, ppocr=pp, init_engines=False)
    result, _ = svc.run(_img())
    assert result == PPOCR
    assert svc.dual.summary()["reason"] == "wechat_empty"
    assert svc.dual.summary()["fallback_used"] is True


def test_wechat_timeout_falls_back():
    wx = FakeEngine(error=WeChatOcrTimeout("timed out"))
    pp = FakeEngine(result=PPOCR)
    svc = OcrService(wechat=wx, ppocr=pp, init_engines=False)
    result, _ = svc.run(_img())
    assert result == PPOCR
    assert svc.dual.summary()["reason"] == "wechat_timeout"


def test_invalid_wechat_payload_falls_back():
    wx = FakeEngine(result=[(BOX, "   ", None)])
    pp = FakeEngine(result=PPOCR)
    dual = DualOcrEngine(wx, pp)
    result, _ = dual(_img())
    assert result == PPOCR
    assert not is_valid_ocr_result([(BOX, "", None)])


def test_gui_ocr_choice_includes_wechat_and_roundtrips(tmp_path, monkeypatch):
    import json
    import license_gui as gui

    monkeypatch.setattr(gui, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    labels = [lab for _, lab in gui.OCR_ENGINE_CHOICES]
    assert any("微信" in lab for lab in labels)
    gui._save_ocr_choice("wechat_v6")
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["wechat_ocr"]["enabled"] is True
    assert cfg["wechat_ocr"]["fallback_ppocr"] is False
    assert "微信" in gui._read_ocr_choice_label()
    gui._save_ocr_choice("v3")
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["wechat_ocr"]["enabled"] is False
    assert cfg["ocr_model"] == "v3"
    assert "PP-OCRv3" in gui._read_ocr_choice_label()


def test_gui_api_key_roundtrips(tmp_path, monkeypatch):
    import json
    import license_gui as gui

    monkeypatch.setattr(gui, "ROOT", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"ai": {"enabled": False, "base_url": "https://api.deepseek.com/v1",
                           "api_key": "", "model": "deepseek-chat"}}),
        encoding="utf-8",
    )
    assert gui._read_ai_api_key() == ""
    gui._save_ai_api_key("  sk-test-key  ")
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["ai"]["api_key"] == "sk-test-key"
    assert cfg["ai"]["enabled"] is True
    assert cfg["ai"]["model"] == "deepseek-chat"
    assert gui._read_ai_api_key() == "sk-test-key"
    gui._save_ai_api_key("")
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["ai"]["api_key"] == ""
    assert gui._read_ai_api_key() == ""


def test_ensure_license_deps_installs_wechat_wrapper(monkeypatch):
    import license_gui as gui

    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui, "LICENSE_PKGS", [])
    monkeypatch.setattr(gui, "_wechat_wrapper_importable", lambda: False)
    calls: list = []

    class _R:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    logs: list[str] = []
    assert gui.ensure_license_deps(lambda msg, *_a, **_k: logs.append(msg))
    assert calls, "expected pip install"
    assert any("py-wechat-ocr==0.1.1" in str(part) for part in calls[0])
    assert any("py-wechat-ocr" in msg for msg in logs)


def test_ensure_license_deps_translators_geo_failure_does_not_abort(monkeypatch):
    import builtins
    import os
    import license_gui as gui

    monkeypatch.setattr(gui.sys, "platform", "linux")
    monkeypatch.setattr(gui, "LICENSE_PKGS", [("translators", "translators")])
    monkeypatch.setattr(gui, "_wechat_wrapper_importable", lambda: True)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "translators":
            raise RuntimeError("Unable to find server backend.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    calls: list = []
    monkeypatch.setattr(gui.subprocess, "run", lambda *a, **k: calls.append(a))
    logs: list[str] = []
    assert gui.ensure_license_deps(lambda msg, *_a, **_k: logs.append(msg))
    assert calls == []
    assert os.environ.get("translators_default_region") == "CN"


def test_ensure_license_deps_skips_wechat_when_present(monkeypatch):
    import license_gui as gui

    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui, "LICENSE_PKGS", [])
    monkeypatch.setattr(gui, "_wechat_wrapper_importable", lambda: True)
    calls: list = []
    monkeypatch.setattr(
        gui.subprocess, "run",
        lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )
    assert gui.ensure_license_deps(lambda *_a, **_k: None)
    assert calls == []


class _FakeWxEngine:
    name = "WeChat OCR"
    provider = "CPU"
    wrapper_name = "wcocr"

    def __init__(self, config=None, **kwargs):
        pass

    def __call__(self, image):
        return VALID, 0.01


def test_wechat_ready_skips_ppocr_init(monkeypatch):
    monkeypatch.setattr("license_trans.ocr_service.WeChatOCREngine", _FakeWxEngine)

    def boom(cfg=None):
        raise AssertionError("PP-OCR should not load when WeChat is ready")

    monkeypatch.setattr("license_trans.ocr_service._init_ppocr", boom)
    svc = OcrService({"wechat_ocr": {"enabled": True}})
    assert svc._ppocr is None
    assert svc.health_status()["ppocr_loaded"] is False
    result, _ = svc.run(_img())
    assert result == VALID
    assert svc.dual.summary()["ocr_actual"] == "wechat"


def test_wechat_init_fail_loads_ppocr(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no dll")

    monkeypatch.setattr("license_trans.ocr_service.WeChatOCREngine", boom)
    monkeypatch.setattr(
        "license_trans.ocr_service._init_ppocr",
        lambda cfg: FakeEngine(result=PPOCR),
    )
    svc = OcrService({"wechat_ocr": {"enabled": True}})
    assert svc._wechat is None
    assert svc._ppocr is not None
    result, _ = svc.run(_img())
    assert result == PPOCR


def test_explicit_fallback_still_loads_ppocr(monkeypatch):
    monkeypatch.setattr("license_trans.ocr_service.WeChatOCREngine", _FakeWxEngine)
    monkeypatch.setattr(
        "license_trans.ocr_service._init_ppocr",
        lambda cfg: FakeEngine(result=PPOCR),
    )
    svc = OcrService({"wechat_ocr": {"enabled": True, "fallback_ppocr": True}})
    assert svc._wechat is not None
    assert svc._ppocr is not None


def test_wechat_only_empty_returns_empty():
    wx = FakeEngine(result=[])
    dual = DualOcrEngine(wx, None)
    result, _ = dual(_img())
    assert result == []
    assert dual.summary()["reason"] == "wechat_empty"
    assert dual.summary()["fallback_used"] is False
    assert wx.calls == 1


def test_wechat_only_timeout_is_sticky():
    wx = FakeEngine(error=WeChatOcrTimeout("timed out"))
    dual = DualOcrEngine(wx, None)
    try:
        dual(_img())
        raise AssertionError("expected WeChatOcrTimeout")
    except WeChatOcrTimeout:
        pass
    dual.reset()
    try:
        dual(_img())
        raise AssertionError("expected RuntimeError after sticky disable")
    except RuntimeError as exc:
        assert "No OCR engine" in str(exc)
    assert wx.calls == 1
