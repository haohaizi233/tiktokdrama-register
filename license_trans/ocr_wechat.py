"""WeChat OCR backends for WeChat / Weixin 3.x and 4.x.

Primary 4.x path loads ``wxocr.dll`` plus the matching Weixin runtime via
the pinned ``wcocr`` extension (shipped by ``py-wechat-ocr``). WeChat 3.x
uses the same ``wcocr.init(ocr_binary, runtime_dir)`` contract with
``WeChatOCR.exe``.

``WeChatOCR.exe -img`` is intentionally not used: that CLI is unverified
and is not how WeChat 3.x/4.x OCR actually works.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("license_trans.ocr")

_WXOCR_DLL = "wxocr.dll"
_WXOCR_EXE_NAMES = ("WeChatOCR.exe", "WeChatOcr.exe")
_VERSION_DIR_RE = re.compile(r"^\d+\.\d+")
_DEFAULT_TIMEOUT = 60.0
_INIT_TIMEOUT = 30.0


class _NativeThread:
    """Keep wxocr.dll on one live thread for the process lifetime.

    ``wcocr.init`` / ``wcocr.ocr`` are thread-affine. A throwaway
    ThreadPoolExecutor for init, then another for ocr, lets the init
    worker die — the next ``ocr()`` then raises ``wechatocr not initialized``.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self.thread_ident: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="wechat-ocr-native", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _loop(self) -> None:
        self.thread_ident = threading.get_ident()
        self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, args, kwargs, fut = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                fut.set_exception(exc)

    def call(self, fn, *args, timeout: float | None = None, **kwargs):
        if self.thread_ident is not None and threading.get_ident() == self.thread_ident:
            return fn(*args, **kwargs)
        fut: Future = Future()
        self._jobs.put((fn, args, kwargs, fut))
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout as exc:
            raise WeChatOcrTimeout(
                f"WeChat OCR timed out after {timeout}s"
            ) from exc


_NATIVE: _NativeThread | None = None
_NATIVE_GUARD = threading.Lock()


def _native() -> _NativeThread:
    global _NATIVE
    with _NATIVE_GUARD:
        if _NATIVE is None:
            _NATIVE = _NativeThread()
        return _NATIVE


_PENDING_TEMP: set[str] = set()
_PENDING_GUARD = threading.Lock()


def _is_sharing_violation(exc: OSError) -> bool:
    if getattr(exc, "winerror", None) == 32:
        return True
    return exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY)


def _unlink_temp(path: str, *, attempts: int = 8) -> None:
    """Take the temp PNG back after wxocr.dll drops it, then delete.

    The DLL often keeps a sharing lock for a short time after ocr() returns.
    Forcing CloseHandle into WeChat is unsafe; retry on this process's native
    thread until the file is ours again.
    """
    if not path:
        return
    delay = 0.03
    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            os.unlink(path)
            with _PENDING_GUARD:
                _PENDING_TEMP.discard(path)
            return
        except FileNotFoundError:
            with _PENDING_GUARD:
                _PENDING_TEMP.discard(path)
            return
        except OSError as exc:
            last = exc
            if not _is_sharing_violation(exc):
                LOG.warning("temp OCR image cleanup failed: %s", exc)
                return
            time.sleep(delay)
            delay = min(0.25, delay * 2)
    with _PENDING_GUARD:
        _PENDING_TEMP.add(path)
    LOG.debug("temp OCR image still locked, deferred: %s (%s)", path, last)


def _sweep_pending_temps() -> None:
    with _PENDING_GUARD:
        leftover = list(_PENDING_TEMP)
        _PENDING_TEMP.clear()
    for path in leftover:
        _unlink_temp(path, attempts=3)

_INSTALL_ROOTS = [
    Path(r"C:\Program Files\Tencent\Weixin"),
    Path(r"C:\Program Files (x86)\Tencent\Weixin"),
    Path(r"C:\Program Files\Tencent\WeChat"),
    Path(r"C:\Program Files (x86)\Tencent\WeChat"),
    Path(r"D:\Program Files\Tencent\Weixin"),
    Path(r"D:\Program Files\Tencent\WeChat"),
]


class WeChatOcrTimeout(TimeoutError):
    """WeChat OCR timed out."""


@dataclass
class WeChatOcrPaths:
    ocr_binary: str
    runtime_dir: str
    family: str  # "4.x" | "3.x"
    source: str

    @property
    def ocr_name(self) -> str:
        return Path(self.ocr_binary).name


def _existing_dir(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(os.path.expandvars(str(path))).expanduser()
    return p if p.exists() else None


def _newest(paths: list[Path]) -> Path | None:
    files = [p for p in paths if p.is_file()]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def _iter_version_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    try:
        for child in root.iterdir():
            if child.is_dir() and _VERSION_DIR_RE.match(child.name):
                found.append(child)
    except OSError as exc:
        LOG.exception("Cannot list %s: %s", root, exc)
    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return found


# Shallow, known WeChat plugin layouts. Never recursive-glob Program Files
# (a `** / wxocr.dll` walk of Weixin can hang for minutes).
_WXOCR_DLL_RELATIVE = (
    "wxocr.dll",
    "extracted/wxocr.dll",
    "XPlugin/plugins/WeChatOcr/*/extracted/wxocr.dll",
    "XPlugin/Plugins/WeChatOcr/*/extracted/wxocr.dll",
    "XPlugin/plugins/WeChatOCR/*/extracted/wxocr.dll",
    "XPlugin/Plugins/WeChatOCR/*/extracted/wxocr.dll",
    "*/extracted/wxocr.dll",
)
_WXOCR_EXE_RELATIVE = (
    "WeChatOCR.exe",
    "WeChatOcr.exe",
    "extracted/WeChatOCR.exe",
    "XPlugin/Plugins/WeChatOCR/*/extracted/WeChatOCR.exe",
    "XPlugin/plugins/WeChatOCR/*/extracted/WeChatOCR.exe",
    "XPlugin/Plugins/WeChatOcr/*/extracted/WeChatOCR.exe",
    "*/extracted/WeChatOCR.exe",
)


def _glob_ocr_binaries(root: Path, names: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    patterns: list[str] = []
    lowered = {n.lower() for n in names}
    if _WXOCR_DLL.lower() in lowered:
        patterns.extend(_WXOCR_DLL_RELATIVE)
    if any(n.lower() in lowered for n in _WXOCR_EXE_NAMES):
        patterns.extend(_WXOCR_EXE_RELATIVE)
    try:
        for pat in patterns:
            found.extend(root.glob(pat))
        for ver in _iter_version_dirs(root):
            for name in names:
                p = ver / name
                if p.is_file():
                    found.append(p)
            for pat in patterns:
                found.extend(ver.glob(pat))
    except OSError as exc:
        LOG.exception("OCR glob failed under %s: %s", root, exc)
    uniq: dict[str, Path] = {}
    for p in found:
        if not p.is_file():
            continue
        try:
            uniq[str(p.resolve())] = p
        except OSError:
            uniq[str(p)] = p
    return list(uniq.values())


def _plugin_roots() -> list[Path]:
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    roots = []
    for base in (appdata, local):
        if not base:
            continue
        roots.extend(
            [
                Path(base) / "Tencent" / "xwechat",
                Path(base) / "Tencent" / "WeChat",
                Path(base) / "Tencent" / "Weixin",
            ]
        )
    return roots


def _registry_weixin_runtime() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    keys = (
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Weixin",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Weixin",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\WeChat",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\WeChat",
        r"SOFTWARE\WOW6432Node\Tencent\WeChat",
        r"SOFTWARE\Tencent\WeChat",
        r"SOFTWARE\WOW6432Node\Tencent\Weixin",
        r"SOFTWARE\Tencent\Weixin",
    )
    for hive in (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE", None),
                 getattr(__import__("winreg"), "HKEY_CURRENT_USER", None)):
        if hive is None:
            continue
        for key_path in keys:
            try:
                key = winreg.OpenKey(hive, key_path)
            except OSError:
                continue
            try:
                def _q(name: str) -> str:
                    try:
                        val, _ = winreg.QueryValueEx(key, name)
                        return str(val).strip().strip('"')
                    except OSError:
                        return ""

                install = _q("InstallLocation") or _q("InstallPath") or _q("DisplayIcon")
                version = _q("DisplayVersion")
            finally:
                winreg.CloseKey(key)
            if install:
                inst = Path(install)
                if inst.is_file():
                    inst = inst.parent
                if version:
                    cand = inst / version
                    if cand.is_dir():
                        return cand
                if inst.is_dir():
                    versions = _iter_version_dirs(inst)
                    if versions:
                        return versions[0]
                    return inst
    return None


def _pick_runtime(explicit: Path | None, family: str) -> Path | None:
    if explicit:
        if explicit.is_file():
            return explicit.parent
        versions = _iter_version_dirs(explicit)
        if versions:
            return versions[0]
        if explicit.is_dir():
            return explicit
    if family == "4.x":
        roots = [
            Path(r"C:\Program Files\Tencent\Weixin"),
            Path(r"C:\Program Files (x86)\Tencent\Weixin"),
            Path(r"D:\Program Files\Tencent\Weixin"),
        ]
    else:
        roots = [
            Path(r"C:\Program Files\Tencent\WeChat"),
            Path(r"C:\Program Files (x86)\Tencent\WeChat"),
            Path(r"D:\Program Files\Tencent\WeChat"),
        ]
    for root in roots:
        versions = _iter_version_dirs(root)
        if versions:
            return versions[0]
        if root.is_dir():
            return root
    reg = _registry_weixin_runtime()
    if reg:
        return reg
    return None


def _from_explicit(config: dict | None) -> WeChatOcrPaths | None:
    cfg = (config or {}).get("wechat_ocr") or {}
    ocr_path = (
        cfg.get("ocr_path")
        or os.environ.get("WECHAT_OCR_PATH")
        or cfg.get("wechat_ocr_path")
    )
    runtime = (
        cfg.get("wechat_path")
        or os.environ.get("WECHAT_PATH")
        or cfg.get("runtime_dir")
    )
    ocr = _existing_dir(ocr_path)
    rt = _existing_dir(runtime)
    if ocr and ocr.is_file():
        family = "4.x" if ocr.name.lower() == _WXOCR_DLL.lower() else "3.x"
        runtime_dir = _pick_runtime(rt, family)
        if runtime_dir is None:
            runtime_dir = ocr.parent
        return WeChatOcrPaths(
            ocr_binary=str(ocr),
            runtime_dir=str(runtime_dir),
            family=family,
            source="config",
        )
    if rt and rt.is_dir():
        dlls = _glob_ocr_binaries(rt, (_WXOCR_DLL,))
        picked = _newest(dlls)
        if picked:
            runtime_dir = _pick_runtime(rt, "4.x") or rt
            return WeChatOcrPaths(
                ocr_binary=str(picked),
                runtime_dir=str(runtime_dir),
                family="4.x",
                source="config",
            )
        exes = _glob_ocr_binaries(rt, _WXOCR_EXE_NAMES)
        picked = _newest(exes)
        if picked:
            runtime_dir = _pick_runtime(rt, "3.x") or rt
            return WeChatOcrPaths(
                ocr_binary=str(picked),
                runtime_dir=str(runtime_dir),
                family="3.x",
                source="config",
            )
    return None


_DISCOVERY_CACHE: dict[str, WeChatOcrPaths | None] = {}


def discover_wechat_ocr_paths(config: dict | None = None) -> WeChatOcrPaths | None:
    """Locate wxocr.dll (4.x) or WeChatOCR.exe (3.x) plus a runtime folder."""
    explicit = _from_explicit(config)
    if explicit:
        return explicit
    if "auto" in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE["auto"]

    dlls: list[Path] = []
    exes: list[Path] = []
    # Binaries live under AppData plugin trees. Install roots are runtime only.
    for root in _plugin_roots():
        if not root.exists():
            continue
        dlls.extend(_glob_ocr_binaries(root, (_WXOCR_DLL,)))
        exes.extend(_glob_ocr_binaries(root, _WXOCR_EXE_NAMES))

    dll = _newest(dlls)
    result: WeChatOcrPaths | None = None
    if dll:
        runtime = _pick_runtime(None, "4.x")
        if runtime is None:
            runtime = dll.parent
        result = WeChatOcrPaths(
            ocr_binary=str(dll),
            runtime_dir=str(runtime),
            family="4.x",
            source="discovery",
        )
    else:
        exe = _newest(exes)
        if exe:
            runtime = _pick_runtime(None, "3.x")
            if runtime is None:
                runtime = exe.parent
            result = WeChatOcrPaths(
                ocr_binary=str(exe),
                runtime_dir=str(runtime),
                family="3.x",
                source="discovery",
            )
    _DISCOVERY_CACHE["auto"] = result
    return result


def discover_wechat_ocr(config: dict | None = None) -> str | None:
    """Back-compat: return the OCR binary path if discovered."""
    paths = discover_wechat_ocr_paths(config)
    return paths.ocr_binary if paths else None


def _import_wcocr():
    try:
        import wcocr  # type: ignore
        return wcocr
    except ImportError:
        pass
    try:
        import wechat_ocr as pkg
        wcocr = getattr(pkg, "wcocr", None)
        if wcocr is not None:
            return wcocr
        # py-wechat-ocr may ship wcocr next to the package
        pkg_dir = Path(pkg.__file__).resolve().parent
        import sys
        if str(pkg_dir) not in sys.path:
            sys.path.insert(0, str(pkg_dir))
        import wcocr  # type: ignore
        return wcocr
    except Exception as exc:
        LOG.debug("wcocr import failed: %s", exc)
        return None


class WcocrBackend:
    """WeChat 4.x / 3.x backend via swigger wcocr (from py-wechat-ocr)."""

    name = "wcocr"

    def __init__(self, paths: WeChatOcrPaths) -> None:
        self.paths = paths
        self._wcocr = None
        self._initialized = False
        self._init_error: str | None = None

    def init(self, timeout: float | None = None) -> bool:
        return _native().call(self._init_on_native, timeout=timeout or _INIT_TIMEOUT)

    def _init_on_native(self) -> bool:
        if self._initialized and self._wcocr is not None:
            return True
        wcocr = _import_wcocr()
        if wcocr is None:
            self._init_error = (
                "wcocr/py-wechat-ocr not importable "
                "(install py-wechat-ocr==0.1.1 on Windows)"
            )
            return False
        try:
            ok = wcocr.init(self.paths.ocr_binary, self.paths.runtime_dir)
            if ok is False:
                self._init_error = "wcocr.init returned False"
                LOG.warning(
                    "[OCR] wcocr.init returned False binary=%s runtime=%s",
                    self.paths.ocr_binary, self.paths.runtime_dir,
                )
                return False
            self._wcocr = wcocr
            self._initialized = True
            self._init_error = None
            LOG.info(
                "[OCR] wcocr init ok: binary=%s runtime=%s family=%s thread=%s",
                self.paths.ocr_binary, self.paths.runtime_dir, self.paths.family,
                threading.current_thread().name,
            )
            return True
        except Exception as exc:
            LOG.exception("wcocr.init failed")
            self._init_error = f"wcocr.init failed: {exc}"
            return False

    def ocr_file(self, image_path: str, timeout: float | None = None) -> Any:
        return _native().call(
            self._ocr_on_native, image_path, timeout=timeout or _DEFAULT_TIMEOUT)

    def _ocr_on_native(self, image_path: str) -> Any:
        if not self._init_on_native() or self._wcocr is None:
            raise RuntimeError(self._init_error or "wcocr not initialized")
        try:
            return self._wcocr.ocr(image_path)
        except Exception as exc:
            if "not initialized" not in str(exc).lower():
                raise
            LOG.warning("[OCR] wcocr.ocr said not initialized — re-init on native thread")
            self._initialized = False
            self._wcocr = None
            if not self._init_on_native() or self._wcocr is None:
                raise RuntimeError(self._init_error or "wcocr not initialized") from exc
            return self._wcocr.ocr(image_path)


class PyWeChatOcrBackend:
    """Fallback backend using py-wechat-ocr's WeChatOCR helper."""

    name = "py-wechat-ocr"

    def __init__(self, paths: WeChatOcrPaths) -> None:
        self.paths = paths
        self._engine = None
        self._initialized = False
        self._init_error: str | None = None

    def init(self, timeout: float | None = None) -> bool:
        return _native().call(self._init_on_native, timeout=timeout or _INIT_TIMEOUT)

    def _init_on_native(self) -> bool:
        if self._initialized and self._engine is not None:
            return True
        try:
            from wechat_ocr import WeChatOCR
        except Exception as exc:
            self._init_error = f"wechat_ocr.WeChatOCR import failed: {exc}"
            return False
        runtime = Path(self.paths.runtime_dir)
        wx_dir = runtime.parent if _VERSION_DIR_RE.match(runtime.name) else runtime
        try:
            self._engine = WeChatOCR(str(wx_dir))
            self._initialized = True
            self._init_error = None
            LOG.info("[OCR] py-wechat-ocr init ok: %s thread=%s",
                     wx_dir, threading.current_thread().name)
            return True
        except Exception as exc:
            LOG.exception("WeChatOCR() init failed")
            self._init_error = f"WeChatOCR init failed: {exc}"
            return False

    def ocr_file(self, image_path: str, timeout: float | None = None) -> Any:
        return _native().call(
            self._ocr_on_native, image_path, timeout=timeout or _DEFAULT_TIMEOUT)

    def _ocr_on_native(self, image_path: str) -> Any:
        if not self._init_on_native() or self._engine is None:
            raise RuntimeError(self._init_error or "py-wechat-ocr not initialized")
        return self._engine.ocr_img(image_path)


def _parse_wechat_payload(payload: Any) -> list[tuple]:
    """Convert WeChat OCR JSON into [(box, text, score_or_none), ...].

    Score is None when the engine does not provide confidence. Never 1.0.
    """
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return []
            try:
                payload = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                LOG.warning("WeChat OCR returned non-JSON text")
                return []

    items: Any = payload
    if isinstance(payload, dict):
        items = (
            payload.get("ocr_response")
            or payload.get("ocrResult")
            or payload.get("data")
            or payload.get("result")
            or payload.get("items")
            or []
        )
        if isinstance(items, dict):
            items = items.get("items") or items.get("ocr_response") or []
    if not isinstance(items, list):
        return []

    results: list[tuple] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("words") or "").strip()
        if not text:
            continue
        box = _box_from_item(item)
        if box is None:
            continue
        score = item.get("rate", item.get("confidence", item.get("score", item.get("prob"))))
        if score is None:
            conf = None
        else:
            try:
                conf = float(score)
            except (TypeError, ValueError):
                conf = None
        results.append((box, text, conf))
    return results


def _box_from_item(item: dict) -> list[list[float]] | None:
    pos = item.get("pos") or item.get("box") or item.get("position") or item.get("rect")
    if isinstance(pos, list) and len(pos) == 4:
        if all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in pos):
            return [[float(p[0]), float(p[1])] for p in pos]
        if all(isinstance(p, dict) for p in pos):
            return [[float(p.get("x", 0)), float(p.get("y", 0))] for p in pos]
    if isinstance(pos, list) and len(pos) >= 8 and all(isinstance(x, (int, float)) for x in pos[:8]):
        return [[float(pos[i]), float(pos[i + 1])] for i in range(0, 8, 2)]
    if all(k in item for k in ("left", "top", "right", "bottom")):
        l, t, r, b = float(item["left"]), float(item["top"]), float(item["right"]), float(item["bottom"])
        return [[l, t], [r, t], [r, b], [l, b]]
    return None


def is_valid_ocr_result(result: list | None) -> bool:
    if not result:
        return False
    texts = []
    for item in result:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            return False
        text = str(item[1] or "").strip()
        if text:
            texts.append(text)
    return bool(texts)


def wechat_ocr_health_check(config: dict | None = None, *, init: bool = True) -> dict:
    """Real health check: files + runtime init. READY only when initialized.

    ``available``  — wxocr.dll / WeChatOCR.exe and runtime path exist
    ``initialized`` — wcocr/py-wechat-ocr loaded the runtime
    ``ready`` — alias of initialized (a full probe OCR is not run at startup)
    """
    paths = discover_wechat_ocr_paths(config)
    if paths is None:
        return {
            "available": False,
            "initialized": False,
            "ready": False,
            "path": None,
            "runtime_dir": None,
            "family": None,
            "wrapper": None,
            "error": "wxocr.dll / WeChatOCR.exe not found",
        }
    info = {
        "available": True,
        "initialized": False,
        "ready": False,
        "path": paths.ocr_binary,
        "runtime_dir": paths.runtime_dir,
        "family": paths.family,
        "wrapper": None,
        "error": None,
        "source": paths.source,
    }
    if not init:
        info["error"] = "not initialized"
        return info
    try:
        engine = WeChatOCREngine(config, paths=paths, initialize=True)
        info["initialized"] = engine.initialized
        info["ready"] = engine.initialized
        info["wrapper"] = engine.wrapper_name
        info["error"] = None if engine.initialized else engine.init_error
        return info
    except Exception as exc:
        LOG.exception("WeChat OCR health check failed")
        info["error"] = str(exc)
        return info


class WeChatOCREngine:
    """Callable OCR engine matching RapidOCREngine: ([(box, text, score)], elapse)."""

    name = "WeChat OCR"

    def __init__(
        self,
        config: dict | None = None,
        *,
        paths: WeChatOcrPaths | None = None,
        initialize: bool = True,
        timeout: float | None = None,
    ) -> None:
        self._config = config or {}
        wx_cfg = self._config.get("wechat_ocr") or {}
        self._timeout = float(timeout if timeout is not None else wx_cfg.get("timeout", _DEFAULT_TIMEOUT))
        self._paths = paths or discover_wechat_ocr_paths(self._config)
        self._backend: WcocrBackend | PyWeChatOcrBackend | None = None
        self._initialized = False
        self._init_error: str | None = None
        self._provider = "CPU"
        if self._paths is None:
            raise RuntimeError("WeChat OCR binary not found")
        if initialize:
            self._initialize()
            if not self._initialized:
                raise RuntimeError(self._init_error or "WeChat OCR init failed")

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def init_error(self) -> str | None:
        return self._init_error

    @property
    def wrapper_name(self) -> str | None:
        return None if self._backend is None else self._backend.name

    @property
    def paths(self) -> WeChatOcrPaths | None:
        return self._paths

    def _initialize(self) -> None:
        assert self._paths is not None
        backends: list[WcocrBackend | PyWeChatOcrBackend] = [
            WcocrBackend(self._paths),
            PyWeChatOcrBackend(self._paths),
        ]
        errors: list[str] = []
        init_timeout = max(_INIT_TIMEOUT, self._timeout)
        for backend in backends:
            try:
                ok = backend.init(timeout=init_timeout)
                if ok:
                    self._backend = backend
                    self._initialized = True
                    self._init_error = None
                    return
                if getattr(backend, "_init_error", None):
                    errors.append(str(backend._init_error))
            except WeChatOcrTimeout:
                LOG.exception("WeChat backend %s init timed out", backend.name)
                errors.append(f"{backend.name}: init timed out after {init_timeout}s")
            except Exception as exc:
                LOG.exception("WeChat backend %s failed", backend.name)
                errors.append(f"{backend.name}: {exc}")
        self._init_error = "; ".join(errors) or "no WeChat OCR backend initialized"
        self._initialized = False

    def __call__(self, image: np.ndarray) -> tuple[list, float]:
        if not self._initialized or self._backend is None:
            raise RuntimeError(self._init_error or "WeChat OCR not initialized")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        t0 = time.time()
        try:
            _sweep_pending_temps()
            if not cv2.imwrite(tmp_path, image):
                raise RuntimeError("failed to write temp image for WeChat OCR")
            payload = self._ocr_with_timeout(tmp_path)
            parsed = _parse_wechat_payload(payload)
            elapsed = time.time() - t0
            if not parsed:
                keys = list(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__
                LOG.warning("[OCR] WeChat parsed 0 boxes in %.2fs (%s)", elapsed, keys)
            return parsed, elapsed
        finally:
            try:
                _native().call(_unlink_temp, tmp_path, timeout=8)
            except Exception:
                _unlink_temp(tmp_path)

    def _ocr_with_timeout(self, image_path: str) -> Any:
        assert self._backend is not None
        return self._backend.ocr_file(image_path, timeout=self._timeout)
