"""Unified OCR entry: WeChat primary, PP-OCRv6 secondary, LicenseOCR preprocess."""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .ocr import LicenseOCR, RapidOCREngine, _create_engine
from .ocr_wechat import (
    WeChatOCREngine,
    WeChatOcrTimeout,
    is_valid_ocr_result,
    wechat_ocr_health_check,
)

LOG = logging.getLogger("license_trans.ocr")


class DualOcrEngine:
    """Inner engine used by LicenseOCR: WeChat first, PP-OCRv6 only if loaded.

    When PP-OCR is not loaded, WeChat empty results stay empty so LicenseOCR
    can try the next rotation. A single slow photo must not disable WeChat
    for the rest of a library scan. Only repeated timeouts, or a runtime
    crash when PP-OCR exists, stick the engine off.
    """

    def __init__(
        self,
        wechat: Any | None,
        ppocr: Any | None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._wechat = wechat
        self._ppocr = ppocr
        self._timeout = timeout
        self._disable_wechat = False
        self._wechat_timeouts = 0
        self.primary_name = "wechat" if wechat is not None else "ppocr_v6"
        self.last_actual = self.primary_name
        self.last_fallback_used = wechat is None
        self.last_reason: str | None = None if wechat is not None else "wechat_unavailable"
        self.wechat_calls = 0
        self.ppocr_calls = 0
        self.fallback_events: list[dict] = []

    @property
    def name(self) -> str:
        if self._wechat is not None and not self._disable_wechat:
            return "WeChat OCR"
        return getattr(self._ppocr, "name", "RapidOCR · PP-OCRv6")

    @property
    def provider(self) -> str:
        if self._wechat is not None and not self._disable_wechat:
            return getattr(self._wechat, "provider", "CPU")
        if self._ppocr is not None:
            return getattr(self._ppocr, "provider", "CPU")
        return getattr(self._wechat, "provider", "CPU")

    def reset(self) -> None:
        self.last_actual = self.primary_name
        self.last_fallback_used = self._wechat is None or self._disable_wechat
        if self._wechat is None:
            self.last_reason = "wechat_unavailable"
        elif self._disable_wechat:
            self.last_reason = self.last_reason or "wechat_disabled"
        else:
            self.last_reason = None
        self.wechat_calls = 0
        self.ppocr_calls = 0
        self.fallback_events = []

    def summary(self) -> dict:
        fallback_used = bool(self.fallback_events) or (
            self._wechat is None and self.ppocr_calls > 0
        )
        actual = self.last_actual
        if self.wechat_calls > 0 and not fallback_used:
            actual = "wechat"
        elif fallback_used:
            actual = "ppocr_v6"
        reason = self.last_reason
        if self.fallback_events:
            reason = self.fallback_events[-1].get("reason", reason)
        return {
            "stage": "ocr",
            "ocr_primary": self.primary_name,
            "primary": self.primary_name,
            "ocr_actual": actual,
            "fallback_used": fallback_used,
            "fallback": "ppocr_v6" if fallback_used else None,
            "reason": reason,
            "wechat_calls": self.wechat_calls,
            "ppocr_calls": self.ppocr_calls,
        }

    def _fallback(self, image: np.ndarray, reason: str) -> tuple[list, float]:
        LOG.warning("[OCR] WeChat failed (%s) — falling back to PP-OCRv6", reason)
        self.fallback_events.append({"reason": reason})
        self.last_reason = reason
        self.last_fallback_used = True
        self.last_actual = "ppocr_v6"
        if self._ppocr is None:
            raise RuntimeError(f"PP-OCRv6 unavailable after WeChat failure: {reason}")
        self.ppocr_calls += 1
        return self._ppocr(image)

    def __call__(self, image: np.ndarray) -> tuple[list, float]:
        if self._wechat is not None and not self._disable_wechat:
            try:
                self.wechat_calls += 1
                result, elapse = self._wechat(image)
                n = len(result or [])
                if is_valid_ocr_result(result):
                    LOG.info("[OCR] WeChat %d 行, %.2fs", n, float(elapse or 0))
                    self.last_actual = "wechat"
                    self.last_fallback_used = False
                    self.last_reason = None
                    self._wechat_timeouts = 0
                    return result, elapse
                LOG.warning("[OCR] WeChat 空结果, %.2fs", float(elapse or 0))
                if self._ppocr is None:
                    self.last_actual = "wechat"
                    self.last_reason = "wechat_empty"
                    return result or [], elapse
                return self._fallback(image, "wechat_empty")
            except WeChatOcrTimeout:
                LOG.exception("WeChat OCR timeout")
                self._wechat_timeouts += 1
                self.last_reason = "wechat_timeout"
                if self._ppocr is not None:
                    self._disable_wechat = True
                    return self._fallback(image, "wechat_timeout")
                # WeChat-only: one slow destain/photo must not kill the batch.
                if self._wechat_timeouts >= 3:
                    self._disable_wechat = True
                    raise RuntimeError("No OCR engine available") from None
                raise
            except Exception:
                LOG.exception("WeChat OCR runtime failed")
                self._disable_wechat = True
                if self._ppocr is None:
                    raise
                return self._fallback(image, "wechat_runtime_failed")
        if self._ppocr is None:
            raise RuntimeError("No OCR engine available")
        self.ppocr_calls += 1
        self.last_actual = "ppocr_v6"
        if self._wechat is None:
            self.last_fallback_used = True
            self.last_reason = self.last_reason or "wechat_unavailable"
        LOG.info("[OCR] PP-OCR pass %d", self.ppocr_calls)
        return self._ppocr(image)


def _init_ppocr(config: dict | None = None):
    model = str((config or {}).get("ocr_model") or "v6_medium")
    try:
        if model == "v3":
            from .ocr import LegacyRapidOCREngine
            return LegacyRapidOCREngine()
        return RapidOCREngine()
    except Exception:
        LOG.exception("PP-OCR init failed, trying LicenseOCR auto engine")
        return _create_engine("rapidocr", "v3" if model == "v3" else "v6_medium")


def _want_ppocr_fallback(config: dict | None, wechat) -> bool:
    """Load PP-OCR only when WeChat is missing, or fallback is explicitly on."""
    wx_cfg = (config or {}).get("wechat_ocr") or {}
    if wechat is None:
        return True
    return wx_cfg.get("fallback_ppocr") is True


class OcrService:
    """Public OCR entry used by the pipeline and GUI.

    Default: WeChat only. PP-OCRv6 is loaded only if WeChat is unavailable
    or ``wechat_ocr.fallback_ppocr`` is true.
    """

    def __init__(
        self,
        config: dict | None = None,
        *,
        wechat: Any | None = None,
        ppocr: Any | None = None,
        init_engines: bool = True,
    ) -> None:
        self._config = config or {}
        self._wechat = wechat
        self._ppocr = ppocr
        self._wechat_error: str | None = None
        wx_cfg = self._config.get("wechat_ocr") or {}
        wechat_enabled = wx_cfg.get("enabled", True) is not False
        timeout = float(wx_cfg.get("timeout", 60))

        if init_engines:
            if self._wechat is None and wechat_enabled:
                try:
                    self._wechat = WeChatOCREngine(self._config)
                    if not _want_ppocr_fallback(self._config, self._wechat):
                        LOG.info("[OCR] WeChat ready — skip PP-OCRv6")
                except Exception as exc:
                    LOG.exception("WeChat OCR engine not started")
                    self._wechat_error = str(exc)
                    self._wechat = None
            if self._ppocr is None and _want_ppocr_fallback(self._config, self._wechat):
                if self._wechat is not None:
                    LOG.info("[OCR] PP-OCRv6 fallback requested, loading…")
                self._ppocr = _init_ppocr(self._config)

        self._dual = DualOcrEngine(self._wechat, self._ppocr, timeout=timeout)
        self._legacy = LicenseOCR(engine=self._dual)
        self._ocr = self._dual
        self.gpu_provider = self._legacy.gpu_provider
        if self._wechat is not None:
            self.engine_name = "WeChat OCR"
        else:
            self.engine_name = getattr(self._legacy, "engine_name", "PP-OCRv6")
        self.last_meta: dict = {}

    @property
    def wechat_available(self) -> bool:
        return self._wechat is not None

    @property
    def ppocr_available(self) -> bool:
        return self._ppocr is not None

    @property
    def primary_name(self) -> str:
        return self._dual.primary_name

    @property
    def dual(self) -> DualOcrEngine:
        return self._dual

    def health_status(self) -> dict:
        wx = wechat_ocr_health_check(self._config, init=False)
        if self._wechat is not None:
            wx["available"] = True
            wx["initialized"] = True
            wx["ready"] = True
            wx["error"] = None
            wx["wrapper"] = getattr(self._wechat, "wrapper_name", wx.get("wrapper"))
        elif self._wechat_error:
            wx["initialized"] = False
            wx["ready"] = False
            wx["error"] = self._wechat_error
        return {
            "wechat": wx,
            "ppocr_v6": "READY" if self._ppocr is not None else "UNAVAILABLE",
            "ppocr_loaded": self._ppocr is not None,
            "primary": self.primary_name,
            "fallback_mode": self._wechat is None,
        }

    def run(self, image: np.ndarray) -> tuple[list, float]:
        """Single-shot OCR on a BGR array (no LicenseOCR preprocess)."""
        return self._dual(image)

    def recognize(
        self,
        path,
        *,
        max_side: int = 1600,
        meta: dict | None = None,
    ):
        self._dual.reset()
        if meta is None:
            meta = {}
        img, lines = self._legacy.recognize(path, max_side=max_side, meta=meta)
        ocr_meta = self._dual.summary()
        meta.update(ocr_meta)
        self.last_meta = dict(ocr_meta)
        return img, lines

    def quick_detect(self, path):
        self._dual.reset()
        return self._legacy.quick_detect(path)
