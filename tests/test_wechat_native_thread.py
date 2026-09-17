"""wxocr.dll must init and ocr on one live thread."""
from __future__ import annotations

import threading

from license_trans.ocr_wechat import (
    WcocrBackend,
    WeChatOcrPaths,
    _NativeThread,
)


def _paths() -> WeChatOcrPaths:
    return WeChatOcrPaths(
        ocr_binary="wxocr.dll",
        runtime_dir="runtime",
        family="4.x",
        source="test",
    )


def test_native_thread_reuses_one_ident():
    gate = _NativeThread()
    ids = []

    def rec():
        ids.append(threading.get_ident())
        return threading.get_ident()

    first = gate.call(rec)
    second = gate.call(rec)
    assert first == second == gate.thread_ident
    assert ids == [first, second]


def test_native_thread_reentrant_from_worker():
    gate = _NativeThread()

    def inner():
        return threading.get_ident()

    def outer():
        return gate.call(inner)

    assert gate.call(outer) == gate.thread_ident


def test_wcocr_init_false_is_failure(monkeypatch):
    class Fake:
        def init(self, *_a, **_k):
            return False

        def ocr(self, _path):
            raise RuntimeError("wechatocr not initialized")

    monkeypatch.setattr("license_trans.ocr_wechat._import_wcocr", lambda: Fake())
    backend = WcocrBackend(_paths())
    assert backend.init() is False
    assert "False" in (backend._init_error or "")


def test_ocr_reinit_when_native_says_uninitialized(monkeypatch):
    calls = {"init": 0, "ocr": 0, "threads": []}

    class Fake:
        def init(self, *_a, **_k):
            calls["init"] += 1
            calls["threads"].append(threading.current_thread().name)
            return True

        def ocr(self, _path):
            calls["ocr"] += 1
            calls["threads"].append(threading.current_thread().name)
            if calls["ocr"] == 1:
                raise RuntimeError("wechatocr not initialized")
            return {"ocr_response": []}

    monkeypatch.setattr("license_trans.ocr_wechat._import_wcocr", lambda: Fake())
    backend = WcocrBackend(_paths())
    assert backend.init() is True
    payload = backend.ocr_file("dummy.png")
    assert payload == {"ocr_response": []}
    assert calls["init"] == 2
    assert calls["ocr"] == 2
    assert all(name == "wechat-ocr-native" for name in calls["threads"])


def test_unlink_temp_retries_sharing_violation(monkeypatch):
    import errno
    from license_trans import ocr_wechat as wx

    calls = {"n": 0}

    def fake_unlink(_path):
        calls["n"] += 1
        if calls["n"] < 3:
            err = OSError(32, "used by another process")
            err.winerror = 32
            err.errno = errno.EACCES
            raise err

    monkeypatch.setattr(wx.os, "unlink", fake_unlink)
    monkeypatch.setattr(wx.time, "sleep", lambda _s: None)
    wx._unlink_temp("C:/WINDOWS/TEMP/tmp-ocr.png")
    assert calls["n"] == 3


def test_unlink_temp_defers_when_still_locked(monkeypatch):
    import errno
    from license_trans import ocr_wechat as wx

    def fake_unlink(_path):
        err = OSError(32, "used by another process")
        err.winerror = 32
        err.errno = errno.EACCES
        raise err

    monkeypatch.setattr(wx.os, "unlink", fake_unlink)
    monkeypatch.setattr(wx.time, "sleep", lambda _s: None)
    wx._PENDING_TEMP.clear()
    wx._unlink_temp("C:/WINDOWS/TEMP/tmp-locked.png", attempts=2)
    assert "C:/WINDOWS/TEMP/tmp-locked.png" in wx._PENDING_TEMP
    wx._PENDING_TEMP.clear()
