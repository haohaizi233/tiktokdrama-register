# -*- coding: utf-8 -*-
"""执照翻译页: 中文营业执照照片 → 固定英文模板 PDF。

独立页面(由 gui_app 的 Notebook 挂载)。OCR/渲染在工作线程执行,
主线程只收 queue 事件更新表格/日志/预览, 避免卡住短剧注册页。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import tkinter as tk

from paths import APP_ROOT as ROOT
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
STATUS_WAIT, STATUS_RUN, STATUS_DONE, STATUS_SKIP, STATUS_ERR, STATUS_REVIEW = (
    "等待", "转换中", "完成", "非A格式", "失败", "需复核",
)
STATUS_COLOR = {
    STATUS_WAIT: "#555555",
    STATUS_RUN: "#1565c0",
    STATUS_DONE: "#0a7d32",
    STATUS_SKIP: "#b26a00",
    STATUS_ERR: "#c62828",
    STATUS_REVIEW: "#c62828",
}
LOG_TAGS = {
    "info": "#222222",
    "ok": "#0a7d32",
    "err": "#c62828",
    "warn": "#b26a00",
    "dim": "#888888",
}
WARN_LABELS = {
    "credit_code": "信用代码缺失",
    "credit_code_checksum": "信用代码校验失败",
    "name": "名称缺失",
    "type": "类型缺失",
    "legal_person": "法人缺失",
    "scope": "经营范围缺失",
    "scope_untranslated": "经营范围含未译中文",
    "capital": "资本缺失",
    "address": "地址缺失",
    "term": "营业期限缺失",
    "authority": "机关缺失",
    "issue_date": "发证日期缺失",
    "qr_mismatch": "QR与信用代码不一致",
    "old_format_license": "老版执照(无信用代码)",
    "need_review": "需人工确认",
}
EN_WARN_LABELS = {
    "en_name_empty": "英译名称空",
    "en_type_empty": "英译类型空",
    "en_legal_person_empty": "英译法人空",
    "en_scope_empty": "英译经营范围空",
    "en_address_empty": "英译地址空",
}
CONF_THRESHOLD = 0.6
_GPU_PROBE_TIMEOUT_S = 4.0
_GPU_UI_TIMEOUT_MS = 4500
_GPU_PROBE_SCRIPT = (
    "import json,sys\n"
    "try:\n"
    "    import onnxruntime as ort\n"
    "    json.dump({'ok': True, 'providers': list(ort.get_available_providers())}, sys.stdout)\n"
    "except Exception as e:\n"
    "    json.dump({'ok': False, 'providers': ['CPUExecutionProvider'], 'error': str(e)}, sys.stdout)\n"
)


_GPU_PROBE_CACHE: dict | None = None


def _probe_onnx_inline() -> dict:
    """进程内探测 ONNX providers(打包模式用, 替代 subprocess)。"""
    try:
        import onnxruntime as ort
        providers = list(ort.get_available_providers())
        return {"ok": True, "providers": providers, "error": ""}
    except Exception as exc:
        return {"ok": False, "providers": ["CPUExecutionProvider"], "error": str(exc)}


def probe_onnx_providers(
    timeout: float = _GPU_PROBE_TIMEOUT_S,
    *,
    use_cache: bool = False,
) -> dict:
    """Probe ONNX Runtime in a child process so CUDA DLL load cannot freeze Tk.

    Must not import license_trans: that package init pulls pipeline/WeChat/OCR
    and can hang longer than any in-thread timeout.
    """
    global _GPU_PROBE_CACHE
    if use_cache and _GPU_PROBE_CACHE is not None:
        return _GPU_PROBE_CACHE
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 是 exe 本身, 不能用 exe -c 起子进程(会再开GUI);
        # 改为进程内探测, CUDA DLL 加载由 try/except 兜底
        result = _probe_onnx_inline()
        _GPU_PROBE_CACHE = result
        return result
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _GPU_PROBE_SCRIPT],
            capture_output=True, text=True, timeout=timeout,
            creationflags=flags,
        )
        line = (proc.stdout or "").strip().splitlines()
        data = json.loads(line[-1]) if line else {}
        providers = list(data.get("providers") or ["CPUExecutionProvider"])
        result = {
            "ok": bool(data.get("ok", True)),
            "providers": providers,
            "error": data.get("error") or (proc.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        result = {
            "ok": False,
            "providers": ["CPUExecutionProvider"],
            "error": f"onnxruntime probe timed out after {timeout:.0f}s",
        }
    except Exception as exc:
        result = {
            "ok": False,
            "providers": ["CPUExecutionProvider"],
            "error": str(exc),
        }
    _GPU_PROBE_CACHE = result
    return result


def gpu_label_from_providers(providers: list[str]) -> tuple[str, str, str]:
    """Return (label, color, detail) for the GPU status strip."""
    if "CUDAExecutionProvider" in providers:
        prov, color = "CUDA", "#0a7d32"
        text = "GPU: CUDA ✓"
    elif "DmlExecutionProvider" in providers:
        prov, color = "DirectML", "#0a7d32"
        text = "GPU: DirectML ✓"
    else:
        prov, color = "CPU", "#b26a00"
        text = "GPU: CPU（ORT 无 CUDA/DML）"
    detail = f"ONNX Runtime providers: {', '.join(providers) or 'unavailable'} | Selected: {prov}"
    if "CUDAExecutionProvider" not in providers and "DmlExecutionProvider" not in providers:
        detail += " | " + (
            "当前是 CPU 版 onnxruntime。笔记本 RTX 请安装 onnxruntime-directml 或 onnxruntime-gpu。"
        )
    return text, color, detail


def _warn_label(code: str) -> str:
    if code in WARN_LABELS:
        return WARN_LABELS[code]
    if code in EN_WARN_LABELS:
        return EN_WARN_LABELS[code]
    if code.endswith("_low_confidence"):
        field = code[: -len("_low_confidence")]
        names = {
            "name": "名称", "type": "类型", "legal_person": "法人",
            "scope": "经营范围", "capital": "资本", "address": "地址",
            "term": "期限", "authority": "机关",
        }
        return f"{names.get(field, field)}识别不准"
    return code
LICENSE_PKGS = [
    ("rapidocr", "rapidocr>=3.9.0"),
    ("onnxruntime", "onnxruntime-directml"),
    ("cv2", "opencv-python"),
    ("fitz", "pymupdf"),
    ("PIL", "pillow"),
    ("cn2an", "cn2an"),
    ("qrcode", "qrcode[pil]"),
    ("pypinyin", "pypinyin"),
    ("numpy", "numpy"),
    ("translators", "translators"),
    ("openai", "openai"),
]
WECHAT_OCR_PIP = "py-wechat-ocr==0.1.1"
# translators probes OneTrust geo on import; SSL/proxy failures must not abort conversion.
_OPTIONAL_IMPORT_PKGS = frozenset({"translators"})


def _prepare_import(mod: str) -> None:
    if mod == "translators":
        os.environ.setdefault("translators_default_region", "CN")


def _import_license_mod(mod: str) -> bool:
    """True if the package is present. Optional pkgs may fail later (network), not pip."""
    _prepare_import(mod)
    try:
        __import__(mod)
        return True
    except ImportError:
        return False
    except Exception:
        if mod in _OPTIONAL_IMPORT_PKGS:
            return True
        raise


def _wechat_wrapper_importable() -> bool:
    """True if either wcocr or wechat_ocr can be imported."""
    try:
        import wcocr  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import wechat_ocr  # noqa: F401
        return True
    except ImportError:
        return False


def _log_ocr_health(log, engine=None):
    """Log OCR engine health check (WeChat + PP-OCRv6). READY only if initialized."""
    try:
        from license_trans.ocr_wechat import wechat_ocr_health_check
        from license_trans import load_config
        cfg = load_config()
        wx = {}
        if engine is not None and hasattr(engine, "health_status"):
            status = engine.health_status()
            wx = status.get("wechat") or {}
            log(f"  主 OCR: {status.get('primary')}", "ok")
            if status.get("primary") == "wechat" and not status.get("ppocr_loaded"):
                log("  仅微信 OCR（不加载 PP-OCRv6）", "ok")
            else:
                log(f"  Fallback 模式: {'是' if status.get('fallback_mode') else '否'}",
                    "warn" if status.get("fallback_mode") else "ok")
        else:
            wx = wechat_ocr_health_check(cfg, init=False)
        if wx.get("initialized") or wx.get("ready"):
            log(f"  WeChat OCR: READY ({wx.get('path')})", "ok")
        elif wx.get("available"):
            log(
                f"  WeChat OCR: AVAILABLE (未初始化) — {wx.get('error') or wx.get('path')}",
                "warn",
            )
        else:
            log(f"  WeChat OCR: UNAVAILABLE — {wx.get('error')}", "dim")
    except Exception as exc:
        log(f"  WeChat OCR health check failed: {exc}", "warn")
    try:
        from license_trans.ai_client import is_ai_enabled
        from license_trans import load_config
        cfg = load_config()
        if is_ai_enabled(cfg):
            log("  AI 语义校验: READY", "ok")
        else:
            log("  AI 语义校验: 未配置 (仅规则模式)", "dim")
    except Exception as exc:
        log(f"  AI health check failed: {exc}", "warn")


def _pipe_license_logs(log):
    """Forward license_trans INFO logs into the GUI pane during a job."""
    lt = logging.getLogger("license_trans")
    lt.setLevel(logging.INFO)

    class _Handler(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
                if record.levelno >= logging.ERROR:
                    level = "err"
                elif record.levelno >= logging.WARNING:
                    level = "warn"
                else:
                    level = "dim"
                log(msg, level)
            except Exception:
                pass

    handler = _Handler()
    handler.setLevel(logging.INFO)
    lt.addHandler(handler)
    lt.propagate = False

    def detach():
        lt.removeHandler(handler)

    return detach


def collect_images(paths: list[Path], recursive: bool = False) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        path = Path(path)
        if path.is_dir():
            if recursive:
                for p in path.rglob("*"):
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                        files.append(p)
            else:
                files.extend(sorted(
                    c for c in path.iterdir()
                    if c.is_file() and c.suffix.lower() in IMAGE_EXTS))
        elif path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            files.append(path)
    unique: list[Path] = []
    for item in files:
        key = item.resolve() if item.exists() else item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def open_path(path: Path) -> None:
    target = path if path.exists() else path.parent
    if not target.exists():
        return
    os.startfile(str(target))  # noqa: S606  Windows


def ensure_license_deps(log) -> bool:
    missing: list[tuple[str, str]] = []
    for mod, pip_name in LICENSE_PKGS:
        try:
            if _import_license_mod(mod):
                continue
        except Exception as exc:
            log(f"导入 {mod} 失败: {exc}", "err")
            return False
        missing.append((mod, pip_name))
    if sys.platform == "win32" and not _wechat_wrapper_importable():
        missing.append(("wechat_ocr", WECHAT_OCR_PIP))
    if not missing:
        return True
    if getattr(sys, "frozen", False):
        # 打包后依赖已捆绑; 可选包(translators)缺了不影响核心功能(改用词库/AI)
        optional_missing = [p for m, p in missing if m in _OPTIONAL_IMPORT_PKGS]
        required_missing = [(m, p) for m, p in missing if m not in _OPTIONAL_IMPORT_PKGS]
        if optional_missing:
            log(f"打包环境缺少可选依赖(改用词库/AI): {' '.join(optional_missing)}", "warn")
        if required_missing:
            log(f"打包环境缺少依赖: {' '.join(p for _, p in required_missing)}", "err")
            return False
        return True
    names = " ".join(p for _, p in missing)
    log(f"首次使用执照翻译, 正在安装依赖: {names}", "warn")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", *[p for _, p in missing]],
        capture_output=True, text=True)
    if r.returncode != 0:
        optional = [p for m, p in missing if m in _OPTIONAL_IMPORT_PKGS]
        required = [p for m, p in missing if m not in _OPTIONAL_IMPORT_PKGS]
        log(f"依赖安装失败: {(r.stderr or r.stdout)[-400:]}", "err")
        if required:
            return False
        if optional:
            log("在线翻译库安装失败，将改用词库/AI", "warn")
        return True
    log("执照翻译依赖安装完成", "ok")
    return True


OCR_ENGINE_CHOICES = (
    ("wechat_v6", "仅微信 OCR"),
    ("v6_medium", "仅 PP-OCRv6 Medium"),
    ("v3", "仅 PP-OCRv3（旧版/快）"),
)
_OCR_LABEL_BY_KEY = {k: lab for k, lab in OCR_ENGINE_CHOICES}
_OCR_KEY_BY_LABEL = {lab: k for k, lab in OCR_ENGINE_CHOICES}


def _save_ocr_choice(key: str) -> None:
    """Persist OCR engine + PP-OCR model + WeChat toggle."""
    cfg_path = ROOT / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    wx = dict(cfg.get("wechat_ocr") or {})
    if key == "wechat_v6":
        wx["enabled"] = True
        wx["fallback_ppocr"] = False
        cfg["ocr_engine"] = "wechat"
        cfg["ocr_model"] = "v6_medium"
    elif key == "v3":
        wx["enabled"] = False
        cfg["ocr_engine"] = "rapidocr"
        cfg["ocr_model"] = "v3"
    else:
        wx["enabled"] = False
        cfg["ocr_engine"] = "rapidocr"
        cfg["ocr_model"] = "v6_medium"
    cfg["wechat_ocr"] = wx
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_ocr_choice_label() -> str:
    """Combobox display text from config (WeChat is default primary)."""
    cfg_path = ROOT / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return _OCR_LABEL_BY_KEY["wechat_v6"]
    wx_on = (cfg.get("wechat_ocr") or {}).get("enabled", True) is not False
    model = str(cfg.get("ocr_model") or "v6_medium")
    engine = str(cfg.get("ocr_engine") or "wechat")
    if wx_on and engine in ("wechat", "auto", ""):
        key = "wechat_v6"
    elif model == "v3":
        key = "v3"
    else:
        key = "v6_medium"
    return _OCR_LABEL_BY_KEY.get(key, _OCR_LABEL_BY_KEY["wechat_v6"])


def _read_ai_api_key() -> str:
    cfg_path = ROOT / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return str((cfg.get("ai") or {}).get("api_key") or "").strip()
    except Exception:
        return ""


def _save_ai_api_key(key: str) -> None:
    """Persist OpenAI-compatible API key used by license translation AI."""
    cfg_path = ROOT / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    ai = dict(cfg.get("ai") or {})
    key = (key or "").strip()
    ai["api_key"] = key
    if key:
        ai["enabled"] = True
        ai.setdefault("base_url", "https://api.deepseek.com/v1")
        ai.setdefault("model", "deepseek-chat")
        ai.setdefault("timeout", 60)
    cfg["ai"] = ai
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_ocr_model(model: str) -> None:
    """Back-compat: persist only ocr_model (used by tests / older callers)."""
    if model in _OCR_LABEL_BY_KEY:
        _save_ocr_choice(model)
        return
    if "微信" in model or model == "wechat_v6":
        _save_ocr_choice("wechat_v6")
        return
    if "v3" in model.lower() and "v6" not in model.lower():
        _save_ocr_choice("v3")
        return
    _save_ocr_choice("v6_medium")


def _read_ocr_model() -> str:
    """Back-compat: return stored ocr_model key."""
    cfg_path = ROOT / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cfg.get("ocr_model", "v6_medium")
    except Exception:
        return "v6_medium"


class LicensePage(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._busy = False
        self._preview_photo = None
        self._last_pdf = ""
        self._rows: dict[str, str] = {}  # path -> iid
        self._outputs: dict[str, str] = {}  # path -> pdf
        self._fields_cache: dict[str, dict] = {}  # path -> parsed fields dict
        self._file_dirs: dict[str, str] = {}  # path -> parent dir str
        self._common_dir: str | None = None  # shared parent dir or None
        self._detecting = False  # folder detection in progress
        self._gpu_detecting = False
        self._gpu_token = 0
        self._gpu_timeout_job = None
        self._log_buffer: list[str] = []
        self._preview_token: int = 0
        self._build()
        self.after(50, self._drain)
        self._append_log(
            "只转换 A 格式公司法人营业执照。个体户/合伙/分公司会标成「非A格式」。",
            "dim")
        self.after(200, lambda: self.detect_gpu(silent=True))

    def _build(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 4))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text=" 收件(执照照片) ")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        bar = ttk.Frame(left)
        bar.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Button(bar, text="加入照片…", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="加入文件夹…", command=self.add_folder).pack(
            side="left", padx=6)
        ttk.Button(bar, text="扫描执照库…", command=self.scan_library).pack(
            side="left", padx=6)
        self.btn_remove = ttk.Button(bar, text="移除选中", command=self.remove_selected)
        self.btn_remove.pack(side="left")
        self.btn_clear = ttk.Button(bar, text="清空", command=self.clear_queue)
        self.btn_clear.pack(side="left", padx=6)
        self.btn_edit = ttk.Button(bar, text="编辑字段…", command=self.edit_fields, state="disabled")
        self.btn_edit.pack(side="left", padx=6)
        self.lbl_queue = ttk.Label(bar, text="队列 0", foreground="#777777")
        self.lbl_queue.pack(side="right", padx=(0, 4))

        wrap = ttk.Frame(left)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        cols = ("file", "status", "output", "note")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=10)
        self.tree.heading("file", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.heading("output", text="输出 PDF")
        self.tree.heading("note", text="备注")
        self.tree.column("file", width=220)
        self.tree.column("status", width=72, anchor="center", stretch=False)
        self.tree.column("output", width=160)
        self.tree.column("note", width=180)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", lambda _e: self.open_selected_pdf())
        for st, color in STATUS_COLOR.items():
            self.tree.tag_configure(st, foreground=color)
        ttk.Label(
            left,
            text="jpg / png / webp / tif。红章日期看不清可在右侧指定发证日期。",
            foreground="#777777",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        right = ttk.LabelFrame(body, text=" 出件 ")
        right.grid(row=0, column=1, sticky="nsew")
        out_row = ttk.Frame(right)
        out_row.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(out_row, text="输出目录").pack(side="left")
        self.var_out = tk.StringVar(value=str(ROOT / "license_out"))
        self.ent_out = ttk.Entry(out_row, textvariable=self.var_out)
        self.ent_out.pack(side="left", fill="x", expand=True, padx=6)
        self.btn_browse_out = ttk.Button(out_row, text="浏览…", command=self.browse_out, width=8)
        self.btn_browse_out.pack(side="left")
        self.btn_open_out = ttk.Button(out_row, text="打开", command=self.open_out_dir, width=6)
        self.btn_open_out.pack(side="left", padx=(4, 0))
        self.lbl_out_hint = ttk.Label(right, text="", foreground="#888888")
        self.lbl_out_hint.pack(anchor="w", padx=10, pady=(0, 2))

        date_row = ttk.Frame(right)
        date_row.pack(fill="x", padx=10, pady=4)
        self.var_date_on = tk.BooleanVar(value=False)
        self.var_date = tk.StringVar()
        ttk.Checkbutton(
            date_row, text="指定发证日期", variable=self.var_date_on,
            command=self._sync_date,
        ).pack(side="left")
        self.ent_date = ttk.Entry(date_row, textvariable=self.var_date, width=16)
        self.ent_date.pack(side="left", padx=6)
        self.ent_date.config(state="disabled")
        ttk.Label(date_row, text="例 2022-12-09", foreground="#777777").pack(
            side="left")

        ocr_row = ttk.Frame(right)
        ocr_row.pack(fill="x", padx=10, pady=(4, 2))
        ttk.Label(ocr_row, text="OCR 模型").pack(side="left")
        self.var_ocr_model = tk.StringVar(value=_read_ocr_choice_label())
        self.cmb_ocr_model = ttk.Combobox(
            ocr_row, textvariable=self.var_ocr_model, width=30,
            state="readonly",
            values=[lab for _, lab in OCR_ENGINE_CHOICES],
        )
        self.cmb_ocr_model.pack(side="left", padx=6)
        self.cmb_ocr_model.bind("<<ComboboxSelected>>", self._on_model_change)
        self.btn_detect_gpu = ttk.Button(
            ocr_row, text="检测 GPU", command=self.detect_gpu, width=10)
        self.btn_detect_gpu.pack(side="left", padx=(4, 0))
        self.lbl_gpu = ttk.Label(ocr_row, text="GPU: 未检测", foreground="#888888")
        self.lbl_gpu.pack(side="left", padx=6)

        act = ttk.Frame(right)
        act.pack(fill="x", padx=10, pady=(4, 6))
        self.btn_start = ttk.Button(act, text="▶ 开始转换", command=self.start_convert)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(
            act, text="■ 停止", command=self.request_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_open_pdf = ttk.Button(
            act, text="打开 PDF", command=self.open_selected_pdf, state="disabled")
        self.btn_open_pdf.pack(side="right")
        self.progress = ttk.Progressbar(right, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_progress = ttk.Label(right, text="空闲")
        self.lbl_progress.pack(anchor="w", padx=10)

        self.preview = tk.Label(
            right, text="转换完成后预览英文页（点击打开 PDF）",
            bg="#f0f0f0", fg="#777777", relief="groove", bd=1, cursor="hand2")
        self.preview.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.preview.bind("<Button-1>", lambda _e: self.open_selected_pdf())

        log_box = ttk.LabelFrame(self, text=" 日志 ")
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log_text = ScrolledText(
            log_box, height=8, state="disabled",
            font=("Consolas", 9), bg="#fafafa")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=8)
        for tag, color in LOG_TAGS.items():
            self.log_text.tag_configure(tag, foreground=color)

    def _sync_date(self):
        self.ent_date.config(
            state="normal" if self.var_date_on.get() else "disabled")

    def _on_model_change(self, _event=None):
        sel = self.var_ocr_model.get()
        key = _OCR_KEY_BY_LABEL.get(sel, "wechat_v6")
        _save_ocr_choice(key)
        self._append_log(f"OCR 模型已切换为: {sel}（下次转换生效）", "info")

    def detect_gpu(self, silent: bool = False):
        """Detect GPU off the UI thread. UI watchdog always clears「检测中」."""
        self._gpu_token += 1
        token = self._gpu_token
        self._gpu_detecting = True
        self.btn_detect_gpu.config(state="disabled")
        self.lbl_gpu.config(text="GPU: 检测中…", foreground="#888888")
        if self._gpu_timeout_job is not None:
            try:
                self.after_cancel(self._gpu_timeout_job)
            except tk.TclError:
                pass
        self._gpu_timeout_job = self.after(
            _GPU_UI_TIMEOUT_MS, lambda: self._gpu_watchdog(token))

        def _work():
            try:
                info = probe_onnx_providers(_GPU_PROBE_TIMEOUT_S, use_cache=silent)
                text, color, detail = gpu_label_from_providers(info["providers"])
                if info.get("error") and not info.get("ok"):
                    detail = f"{detail} | {info['error']}"
                self.q.put(("gpu_result", token, text, color, detail, silent))
            except Exception as exc:
                self.q.put((
                    "gpu_result", token, "GPU: 检测失败", "#c62828", str(exc), silent,
                ))

        threading.Thread(target=_work, daemon=True, name="gpu-detect").start()

    def _gpu_watchdog(self, token: int):
        if token != self._gpu_token or not self._gpu_detecting:
            return
        self._apply_gpu_result(
            token,
            "GPU: 检测超时 (使用 CPU)",
            "#b26a00",
            "GPU 探测超时，已按 CPU 继续。可再点「检测 GPU」。",
            silent=False,
        )

    def _apply_gpu_result(
        self, token: int, text: str, color: str, detail: str, silent: bool = False,
    ):
        if token != self._gpu_token:
            return
        self._gpu_detecting = False
        if self._gpu_timeout_job is not None:
            try:
                self.after_cancel(self._gpu_timeout_job)
            except tk.TclError:
                pass
            self._gpu_timeout_job = None
        self.lbl_gpu.config(text=text, foreground=color)
        self.btn_detect_gpu.config(state="normal")
        if detail and not silent:
            self._append_log(detail.replace("\n", " | "), "dim")

    def _sync_actions(self):
        has = bool(self._rows)
        busy = self._busy or self._detecting
        self.btn_start.config(state="disabled" if busy or not has else "normal")
        self.btn_stop.config(state="normal" if self._busy else "disabled")
        self.btn_clear.config(state="disabled" if busy or not has else "normal")
        self.btn_remove.config(state="disabled" if busy or not has else "normal")
        self.btn_edit.config(state="disabled" if busy else "normal")
        sel = self.tree.selection()
        can_edit = bool(sel) and not busy
        if can_edit:
            key = next((k for k, v in self._rows.items() if v == sel[0]), None)
            can_edit = bool(key and key in self._fields_cache)
        self.btn_edit.config(state="normal" if can_edit else "disabled")
        pdf = self._selected_pdf()
        self.btn_open_pdf.config(
            state="normal" if pdf and pdf.exists() else "disabled")
        self.lbl_queue.config(text=f"队列 {len(self._rows)}")
        self._sync_out_dir()

    def _sync_out_dir(self):
        """Lock/unlock output directory entry based on number of distinct dirs."""
        if self._detecting:
            self.ent_out.config(state="disabled")
            self.btn_browse_out.config(state="disabled")
            self.lbl_out_hint.config(text="正在扫描文件夹…")
            return
        dirs = set(self._file_dirs.values())
        if len(dirs) <= 1:
            self.ent_out.config(state="normal")
            self.btn_browse_out.config(state="normal")
            if len(dirs) == 1:
                common = next(iter(dirs))
                if self.var_out.get() == str(ROOT / "license_out"):
                    self.var_out.set(common)
                self.lbl_out_hint.config(text="单目录: 可手动更改输出目录")
            else:
                self.lbl_out_hint.config(text="")
        else:
            self.ent_out.config(state="disabled")
            self.btn_browse_out.config(state="disabled")
            self.var_out.set("（多个目录，各自输出到执照所在文件夹）")
            self.lbl_out_hint.config(
                text=f"执照分布在 {len(dirs)} 个目录, PDF 输出到各自所在文件夹")

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="选择执照照片", parent=self,
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if files:
            self.ingest([Path(p) for p in files])

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择照片文件夹（递归扫描子目录）", parent=self)
        if folder:
            self._start_detect([Path(folder)])

    def _start_detect(self, paths: list[Path]):
        """Recursively find images in folders, run OCR, keep only licenses."""
        all_images = collect_images(paths, recursive=True)
        if not all_images:
            self._append_log("没有找到任何图片", "warn")
            return
        self._detecting = True
        self._sync_actions()
        self.progress.config(maximum=len(all_images), value=0)
        self.lbl_progress.config(text=f"扫描中 0/{len(all_images)}")
        self._append_log(f"开始扫描 {len(all_images)} 张图片, 识别营业执照…", "warn")
        self.worker = threading.Thread(
            target=self._work_detect, args=(all_images,), daemon=True)
        self.worker.start()

    def _work_detect(self, images: list[Path]):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        try:
            if not ensure_license_deps(log):
                self.q.put(("detect_done", 0, 0))
                return
        except Exception as exc:
            log(f"依赖检查失败: {exc}", "err")
            self.q.put(("detect_done", 0, 0))
            return
        from license_trans.ocr_service import OcrService
        from license_trans.pipeline import detect_license, _pick_best_license

        detach_logs = _pipe_license_logs(log)
        try:
            log("加载 OCR 引擎(首次会稍慢)…", "dim")
            try:
                from license_trans.pipeline import load_config
                engine = OcrService(load_config())
            except Exception as e:
                log(f"OCR 引擎加载失败: {e}", "err")
                self.q.put(("detect_done", 0, len(images)))
                return
            prov = getattr(engine, "gpu_provider", "CPU")
            eng = getattr(engine, "engine_name", "RapidOCR")
            quick_workers = min(5, len(images))
            log(f"OCR 引擎就绪: {eng} ({prov}), 并行{quick_workers}线程快速扫描…", "ok")
            _log_ocr_health(log, engine)
            total = len(images)
            by_dir: dict[str, list[tuple[Path, list]]] = {}
            id_by_dir: dict[str, list[tuple[Path, list, str]]] = {}
            scanned = 0
            completed = 0
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from license_trans.id_card import (
                already_id_copy, classify_id_side, named_id_side,
            )
            with ThreadPoolExecutor(max_workers=quick_workers) as pool:
                futures = {pool.submit(engine.quick_detect, img): img for img in images}
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        for f in futures:
                            f.cancel()
                        log("已停止扫描", "warn")
                        break
                    img_path = futures[fut]
                    completed += 1
                    try:
                        is_license, lines = fut.result()
                    except Exception as exc:
                        log(f"扫描失败 {img_path.name}: {exc}", "err")
                        is_license = False
                        lines = []
                    if is_license:
                        key = str(img_path.parent.resolve())
                        by_dir.setdefault(key, []).append((img_path, lines))
                        scanned += 1
                        log(f"  ✓ 执照  {img_path.name}  ({len(lines)} 行)", "ok")
                    else:
                        if already_id_copy(img_path) and not img_path.stem.startswith(("id-", "id_")):
                            pass
                        else:
                            side = classify_id_side(lines) or named_id_side(img_path)
                            if side:
                                key = str(img_path.parent.resolve())
                                id_by_dir.setdefault(key, []).append((img_path, lines, side))
                    self.q.put(("detect_progress", completed, total, img_path.name, is_license))
                    if completed % 20 == 0:
                        log(f"  进度 {completed}/{total}  已找到 {scanned} 张执照", "dim")
            from license_trans.pipeline import _commit_best_ids
            for dir_path, cands in id_by_dir.items():
                _commit_best_ids(cands, log)
            # Pick best license per directory
            best_per_dir: list[Path] = []
            for dir_path, candidates in by_dir.items():
                if len(candidates) == 1:
                    best_per_dir.append(candidates[0][0])
                else:
                    best, _ = _pick_best_license(candidates)
                    best_per_dir.append(best)
                    log(f"  {Path(dir_path).name}: {len(candidates)} 张执照, "
                        f"选用 {best.name}", "dim")
            for p in best_per_dir:
                self.q.put(("detect_hit", str(p.resolve())))
            log(f"扫描完成: {scanned} 张执照, 去重后保留 {len(best_per_dir)} 张 "
                f"(每个公司文件夹仅保留最清晰的一张)", "ok")
            self.q.put(("detect_done", len(best_per_dir), total))
        finally:
            detach_logs()

    def scan_library(self):
        """Scan 中国执照库/ — detect licenses, translate, move to 资料/."""
        from license_trans.pipeline import reset_reserve_exhausted
        reset_reserve_exhausted()  # 手动扫描=可能加了新料, 重置耗尽标记
        if self._busy or self._detecting:
            return
        source = ROOT / "中国执照库"
        if not source.exists():
            messagebox.showwarning(
                "执照翻译", f"执照库不存在:\n{source}", parent=self)
            return
        dest = ROOT / "资料"
        self._detecting = True
        self._sync_actions()
        self.progress.config(maximum=1, value=0)
        self.lbl_progress.config(text="扫描执照库…")
        self._append_log(
            f"开始扫描执照库: {source} → {dest} (并行5线程)", "warn")
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self._work_scan_library,
            args=(source, dest), daemon=True)
        self.worker.start()

    def _work_scan_library(self, source: Path, dest: Path):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        def progress(i, tot, name):
            self.q.put(("scan_progress", i, tot, name))

        try:
            if not ensure_license_deps(log):
                self.q.put(("scan_done", 0, 0, 0, 0, 0))
                return
        except Exception as exc:
            log(f"依赖检查失败: {exc}", "err")
            self.q.put(("scan_done", 0, 0, 0, 0, 0))
            return
        from license_trans.ocr_service import OcrService
        from license_trans.pipeline import process_license_library

        detach_logs = _pipe_license_logs(log)
        try:
            log("加载 OCR 引擎(首次会稍慢)…", "dim")
            try:
                from license_trans.pipeline import load_config
                engine = OcrService(load_config())
            except Exception as e:
                log(f"OCR 引擎加载失败: {e}", "err")
                self.q.put(("scan_done", 0, 0, 0, 0, 0))
                return
            prov = getattr(engine, "gpu_provider", "CPU")
            eng = getattr(engine, "engine_name", "RapidOCR")
            log(f"OCR 引擎就绪: {eng} ({prov}), 开始扫描执照库…", "ok")
            _log_ocr_health(log, engine)
            try:
                result = process_license_library(
                    source, dest, max_workers=5, engine=engine,
                    log=log, progress=progress, stop_event=self.stop_event)
            except Exception as exc:
                log(f"扫描执照库异常: {exc}", "err")
                self.q.put(("scan_done", 0, 0, 0, 0, 0))
                return
            self.q.put(("scan_done",
                         result["processed"], result["failed"],
                         result["moved"], result["skipped"],
                         result.get("review", 0)))
            if result.get("review"):
                log(f"AI仲裁未通过 {result['review']} 家, 已写入 中国执照库/待人工处理.xlsx", "warn")
        finally:
            detach_logs()

    def ingest(self, paths: list[Path]):
        files = collect_images(paths)
        added = 0
        for path in files:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in self._rows:
                continue
            iid = self.tree.insert(
                "", "end", tags=(STATUS_WAIT,),
                values=(path.name, STATUS_WAIT, "—", "待转换"))
            self._rows[key] = iid
            self._file_dirs[key] = str(path.parent.resolve()) if path.parent.exists() else str(path.parent)
            added += 1
        skipped = len(files) - added
        if added:
            self._append_log(f"加入 {added} 张照片", "ok")
        if skipped:
            self._append_log(f"跳过 {skipped} 张重复文件", "dim")
        if not files:
            self._append_log("没有找到可用图片", "warn")
        self._sync_actions()

    def remove_selected(self):
        if self._busy or self._detecting:
            return
        for iid in self.tree.selection():
            key = next((k for k, v in self._rows.items() if v == iid), None)
            self.tree.delete(iid)
            if key:
                self._rows.pop(key, None)
                self._outputs.pop(key, None)
                self._fields_cache.pop(key, None)
                self._file_dirs.pop(key, None)
        self._sync_actions()

    def clear_queue(self):
        if self._busy or self._detecting:
            return
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._outputs.clear()
        self._fields_cache.clear()
        self._file_dirs.clear()
        self.var_out.set(str(ROOT / "license_out"))
        self._sync_actions()

    def browse_out(self):
        folder = filedialog.askdirectory(
            title="选择输出目录", parent=self,
            initialdir=self.var_out.get() or str(ROOT / "license_out"))
        if folder:
            self.var_out.set(folder)

    def open_out_dir(self):
        dirs = set(self._file_dirs.values())
        if len(dirs) == 1:
            path = Path(next(iter(dirs)))
        else:
            out_val = self.var_out.get().strip()
            if out_val and out_val != "（多个目录，各自输出到执照所在文件夹）":
                path = Path(out_val)
            elif dirs:
                path = Path(next(iter(dirs)))
            else:
                path = Path(self.var_out.get().strip() or ROOT / "license_out")
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def start_convert(self):
        if self._busy or self._detecting:
            return
        pending = []
        for key, iid in self._rows.items():
            status = str(self.tree.item(iid, "values")[1])
            if status != STATUS_DONE:
                pending.append(key)
        if not pending:
            messagebox.showinfo(
                "执照翻译", "没有待转换的照片。已完成的不会重跑。", parent=self)
            return
        issue = ""
        if self.var_date_on.get():
            issue = self.var_date.get().strip()
            if not issue:
                messagebox.showwarning(
                    "执照翻译", "勾选了指定发证日期, 请填写例如 2022-12-09。",
                    parent=self)
                return
        # Determine output dirs: single override or per-file
        dirs = set(self._file_dirs.get(k) for k in pending if k in self._file_dirs)
        out_override = None
        if len(dirs) <= 1:
            out_val = self.var_out.get().strip()
            if out_val and out_val != "（多个目录，各自输出到执照所在文件夹）":
                out_override = out_val
                Path(out_override).mkdir(parents=True, exist_ok=True)
        self.stop_event.clear()
        self._set_busy(True)
        self.progress.config(maximum=len(pending), value=0)
        self.lbl_progress.config(text=f"准备转换 {len(pending)} 张")
        if out_override:
            self._append_log(f"开始转换 {len(pending)} 张 → {out_override}", "warn")
        else:
            self._append_log(
                f"开始转换 {len(pending)} 张 → 各自所在目录 (英文版)", "warn")
        self.worker = threading.Thread(
            target=self._work, args=(pending, out_override, issue), daemon=True)
        self.worker.start()

    def request_stop(self):
        if self._busy or self._detecting:
            self.stop_event.set()
            self._append_log("已请求停止…", "warn")

    def _work(self, paths: list[str], out_override: str | None, issue_date: str):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))

        try:
            if not ensure_license_deps(log):
                self.q.put(("done", 0, 0, 0, True))
                return
        except Exception as exc:
            log(f"依赖检查失败: {exc}", "err")
            self.q.put(("done", 0, 0, 0, True))
            return
        from license_trans.classify import NotFormatA
        from license_trans.ocr_service import OcrService
        from license_trans.pipeline import convert_image

        detach_logs = _pipe_license_logs(log)
        try:
            log("加载 OCR 引擎(首次会稍慢)…", "dim")
            try:
                from license_trans.pipeline import load_config
                engine = OcrService(load_config())
            except Exception as e:
                log(f"OCR 引擎加载失败: {e}", "err")
                self.q.put(("done", 0, 0, len(paths), True))
                return
            prov = getattr(engine, "gpu_provider", "CPU")
            eng = getattr(engine, "engine_name", "RapidOCR")
            convert_workers = min(3, len(paths))
            log(f"OCR 引擎就绪: {eng} ({prov}), 并行{convert_workers}线程转换…", "ok")
            _log_ocr_health(log, engine)
            done = skipped = failed = 0
            completed = 0
            total = len(paths)
            work_dir = ROOT / "license_work"

            def _process_one(raw):
                src = Path(raw)
                if out_override:
                    out_dir = Path(out_override)
                else:
                    out_dir = Path(self._file_dirs.get(raw, str(src.parent)))
                dest = out_dir / (src.stem + "英文版.pdf")
                self.q.put(("item", raw, STATUS_RUN, "", ""))
                log(f"  → {src.name}")
                try:
                    info = convert_image(
                        src, dest, work_dir=work_dir, ocr=engine,
                        issue_date=issue_date or None)
                    return ("ok", raw, dest, info)
                except NotFormatA as exc:
                    return ("skip", raw, str(exc))
                except Exception as exc:
                    return ("fail", raw, str(exc)[:200])

            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=convert_workers) as pool:
                futures = {pool.submit(_process_one, raw): raw for raw in paths}
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        for f in futures:
                            f.cancel()
                        log("已停止, 剩余文件未处理", "warn")
                        break
                    completed += 1
                    try:
                        result = fut.result()
                    except Exception as exc:
                        failed += 1
                        log(f"异常: {exc}", "err")
                        self.q.put(("progress", completed, total))
                        continue
                    kind = result[0]
                    raw = result[1]
                    if kind == "ok":
                        _, _, dest, info = result
                        warnings = list(dict.fromkeys(
                            (info["fields"].get("warnings") or [])
                            + (info["english"].get("warnings") or [])))
                        needs = list(info.get("needs_review") or [])
                        st = info.get("status") or {}
                        if needs:
                            note = "需人工确认: " + ", ".join(needs)
                        elif warnings:
                            note = "需核对: " + ", ".join(_warn_label(w) for w in warnings)
                        else:
                            note = "字段完整"
                        row_status = STATUS_REVIEW if needs else STATUS_DONE
                        self.q.put(("item", raw, row_status, str(dest), note))
                        self.q.put(("fields", raw, info["fields"]))
                        self.q.put(("preview", str(dest)))
                        log(f"  ✓ {Path(dest).name}", "ok")
                        log(
                            f"  主 OCR：{st.get('ocr_primary') or info.get('ocr_primary') or '—'}  "
                            f"Fallback：{'是' if st.get('fallback_used') or info.get('fallback_used') else '否'}  "
                            f"AI 校验：{'需复核' if needs else ('通过' if st.get('ai_validated') else '规则模式')}  "
                            f"字段重识别：{st.get('field_retries', 0)}  "
                            f"联网核验：{st.get('web_verified', 0)}  "
                            f"人工确认：{st.get('need_review', len(needs))}",
                            "err" if needs else "ok",
                        )
                        if needs:
                            log(f"  ⚠ {Path(raw).name}  关键字段无法确认: {', '.join(needs)}", "err")
                        elif warnings:
                            log(f"  {Path(raw).name}  {note}", "warn")
                        done += 1
                    elif kind == "skip":
                        _, _, exc_msg = result
                        self.q.put(("item", raw, STATUS_SKIP, "", exc_msg))
                        log(f"  ⊘ {Path(raw).name}  {exc_msg}", "warn")
                        skipped += 1
                    elif kind == "fail":
                        _, _, exc_msg = result
                        self.q.put(("item", raw, STATUS_ERR, "", exc_msg))
                        log(f"  ✗ {Path(raw).name}  {exc_msg}", "err")
                        failed += 1
                    self.q.put(("progress", completed, total))
            self.q.put(("done", done, skipped, failed, self.stop_event.is_set()))
        finally:
            detach_logs()

    def _drain(self):
        MAX_PER_TICK = 40
        processed = 0
        row_dirty = False
        pending_preview = None
        try:
            while processed < MAX_PER_TICK:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[2], msg[1])
                elif kind == "item":
                    _, path, status, output, note = msg
                    self._update_row(path, status, output, note, sync=False)
                    row_dirty = True
                    self.lbl_progress.config(text=f"{Path(path).name}  ·  {status}")
                elif kind == "progress":
                    _, done, tot = msg
                    self.progress.config(maximum=tot, value=done)
                elif kind == "preview":
                    pending_preview = msg[1]
                elif kind == "fields":
                    self._fields_cache[msg[1]] = msg[2]
                elif kind == "detect_progress":
                    _, i, tot, name, is_lic = msg
                    self.progress.config(maximum=tot, value=i)
                    tag = "✓" if is_lic else "✗"
                    self.lbl_progress.config(text=f"扫描 {i}/{tot}  {tag} {name}")
                elif kind == "detect_hit":
                    path = msg[1]
                    key = path
                    if key in self._rows:
                        processed += 1
                        continue
                    p = Path(path)
                    iid = self.tree.insert(
                        "", "end", tags=(STATUS_WAIT,),
                        values=(p.name, STATUS_WAIT, "—", "已识别为执照"))
                    self._rows[key] = iid
                    self._file_dirs[key] = str(p.parent.resolve()) if p.parent.exists() else str(p.parent)
                    row_dirty = True
                elif kind == "detect_done":
                    _, found, tot = msg
                    self._detecting = False
                    self.progress.config(value=0)
                    self.lbl_progress.config(text="空闲")
                    self._append_log(
                        f"扫描完成: {found}/{tot} 张为营业执照", "ok" if found else "warn")
                    self.stop_event.clear()
                    row_dirty = True
                elif kind == "scan_progress":
                    _, i, tot, name = msg
                    self.progress.config(maximum=tot, value=i)
                    self.lbl_progress.config(text=f"扫描执照库 {i}/{tot}  {name}")
                elif kind == "scan_done":
                    _, processed_n, failed_n, moved_n, skipped_n, review_n = msg
                    self._detecting = False
                    self.progress.config(value=0)
                    self.lbl_progress.config(text="空闲")
                    _total = processed_n + failed_n + review_n + skipped_n
                    _fail_total = failed_n + review_n
                    self._append_log(
                        f"===== 执照库扫描完成: 共 {_total} / 成功 {processed_n} "
                        f"/ 失败 {_fail_total}"
                        + (f" (含仲裁未过 {review_n})" if review_n else "")
                        + f" / 跳过 {skipped_n} =====",
                        "ok" if _fail_total == 0 else "warn")
                    self.stop_event.clear()
                    row_dirty = True
                    messagebox.showinfo(
                        "执照库扫描完成",
                        f"共扫描: {_total} 个\n成功: {processed_n} 个\n"
                        f"失败: {_fail_total} 个"
                        + (f" (含仲裁未过 {review_n})" if review_n else "")
                        + f"\n已移动到资料: {moved_n} 个\n跳过(无执照): {skipped_n} 个",
                        parent=self)
                elif kind == "gpu_result":
                    _, token, text, color, detail, silent = msg
                    self._apply_gpu_result(token, text, color, detail, silent)
                elif kind == "done":
                    _, done, skipped, failed, stopped = msg
                    self._set_busy(False)
                    self.lbl_progress.config(text="空闲")
                    title = "已停止" if stopped else "转换完成"
                    self._append_log(
                        f"===== 汇总: 完成 {done} / 非A {skipped} / 失败 {failed} =====",
                        "ok" if failed == 0 else "warn")
                    dirs = set(self._file_dirs.values())
                    if len(dirs) <= 1 and self.var_out.get().strip():
                        out_info = self.var_out.get()
                    else:
                        out_info = "各自所在文件夹"
                    messagebox.showinfo(
                        title,
                        f"完成: {done} 个\n非A格式: {skipped} 个\n失败: {failed} 个\n\n"
                        f"PDF 输出目录: {out_info}",
                        parent=self)
                processed += 1
        except queue.Empty:
            pass

        # Batch flush log buffer to file (one write per tick, not per message)
        if self._log_buffer:
            try:
                with open(ROOT / "gui.log", "a", encoding="utf-8") as f:
                    f.writelines(self._log_buffer)
            except OSError:
                pass
            self._log_buffer.clear()

        # Render only the latest preview (skip stale ones)
        if pending_preview:
            self._show_preview(pending_preview)

        if row_dirty or pending_preview:
            self._sync_actions()

        self.after(50, self._drain)

    def _update_row(self, path: str, status: str, output: str, note: str,
                    sync: bool = True):
        iid = self._rows.get(path)
        if not iid:
            return
        vals = list(self.tree.item(iid, "values"))
        vals[1] = status
        if output:
            vals[2] = Path(output).name
            self._outputs[path] = output
            self._last_pdf = output
            try:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
            except tk.TclError:
                pass
        vals[3] = note or "—"
        self.tree.item(iid, values=vals, tags=(status,))
        if sync:
            self._sync_actions()

    def _on_select(self):
        pdf = self._selected_pdf()
        if pdf and pdf.exists():
            self._show_preview(str(pdf))
        self._sync_actions()

    def _selected_pdf(self) -> Path | None:
        sel = self.tree.selection()
        if sel:
            iid = sel[0]
            key = next((k for k, v in self._rows.items() if v == iid), None)
            if key and key in self._outputs:
                p = Path(self._outputs[key])
                if p.exists():
                    return p
        if self._last_pdf:
            p = Path(self._last_pdf)
            if p.exists():
                return p
        for path in reversed(list(self._outputs.values())):
            p = Path(path)
            if p.exists():
                return p
        return None

    def open_selected_pdf(self):
        path = self._selected_pdf()
        if path and path.exists():
            open_path(path)
        else:
            messagebox.showinfo("执照翻译", "还没有可打开的 PDF。", parent=self)

    def edit_fields(self):
        sel = self.tree.selection()
        if not sel:
            return
        key = next((k for k, v in self._rows.items() if v == sel[0]), None)
        if not key or key not in self._fields_cache:
            return
        fields = self._fields_cache[key]
        # Output dir: override if single, else file's parent
        dirs = set(self._file_dirs.values())
        if len(dirs) <= 1:
            out_val = self.var_out.get().strip()
            if out_val and out_val != "（多个目录，各自输出到执照所在文件夹）":
                out_dir = Path(out_val)
            else:
                out_dir = Path(self._file_dirs.get(key, str(Path(key).parent)))
        else:
            out_dir = Path(self._file_dirs.get(key, str(Path(key).parent)))
        dest = out_dir / (Path(key).stem + "英文版.pdf")
        dlg = _FieldEditDialog(self, fields)
        self.wait_window(dlg)
        if dlg.result:
            self._fields_cache[key] = dlg.result
            self._append_log(f"手动修正字段, 重新渲染 {Path(key).name}", "warn")
            self._rerender(key, dest)

    def _rerender(self, key: str, dest: Path):
        if self._busy:
            return
        fields = self._fields_cache.get(key)
        if not fields:
            return
        self._set_busy(True)
        self.progress.config(maximum=1, value=0)
        self.lbl_progress.config(text=f"重新渲染 {Path(key).name}")
        self.worker = threading.Thread(
            target=self._work_rerender, args=(key, dest, fields), daemon=True)
        self.worker.start()

    def _work_rerender(self, key: str, dest: Path, fields: dict):
        def log(msg, level="info"):
            self.q.put(("log", level, f"[{datetime.now():%H:%M:%S}] {msg}"))
        try:
            from license_trans.pipeline import convert_image
            info = convert_image(
                Path(key), dest, work_dir=ROOT / "license_work",
                fields_override=fields)
            warnings = list(dict.fromkeys(
                (info["fields"].get("warnings") or [])
                + (info["english"].get("warnings") or [])))
            note = "需核对: " + ", ".join(_warn_label(w) for w in warnings) if warnings else "字段完整"
            self.q.put(("item", key, STATUS_DONE, str(dest), note))
            self.q.put(("fields", key, info["fields"]))
            self.q.put(("preview", str(dest)))
            log(f"重新渲染完成  {dest}", "ok")
        except Exception as exc:
            log(f"重新渲染失败: {exc}", "err")
            self.q.put(("item", key, STATUS_ERR, "", str(exc)[:200]))
        self.q.put(("progress", 1, 1))
        self.q.put(("done", 1, 0, 0, False))

    def _show_preview(self, pdf_path: str):
        """Render PDF preview in a background thread, update widget on main thread."""
        self._last_pdf = pdf_path
        token = self._preview_token = self._preview_token + 1
        target_w = max(280, self.preview.winfo_width() - 8)

        def _bg_render():
            try:
                import fitz
                from PIL import Image
                path = Path(pdf_path)
                if not path.exists():
                    self.after(0, lambda: self._apply_preview(None, pdf_path, token))
                    return
                doc = fitz.open(path)
                page = doc[0]
                scale = max(0.6, target_w / max(page.rect.width, 1))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                doc.close()
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                self.after(0, lambda: self._apply_preview(img, pdf_path, token))
            except Exception:
                self.after(0, lambda: self._apply_preview(None, pdf_path, token))

        threading.Thread(target=_bg_render, daemon=True).start()

    def _apply_preview(self, img, pdf_path: str, token: int):
        """Called on main thread via after(0) — only applies if token is current."""
        if token != self._preview_token:
            return  # stale render, skip
        self._last_pdf = pdf_path
        if img is None:
            self.preview.config(image="", text=Path(pdf_path).name)
            self._sync_actions()
            return
        from PIL import ImageTk
        self._preview_photo = ImageTk.PhotoImage(img)
        self.preview.config(image=self._preview_photo, text="")
        self._sync_actions()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._sync_actions()

    def _append_log(self, text: str, level: str = "info"):
        self.log_text.config(state="normal")
        self.log_text.insert(
            "end", text + "\n", level if level in LOG_TAGS else "info")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self._log_buffer.append(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} [license/{level}] {text}\n")


EDITABLE_FIELDS = [
    ("name", "名称"),
    ("type", "类型"),
    ("legal_person", "法定代表人"),
    ("scope", "经营范围"),
    ("capital", "注册资本"),
    ("established", "成立日期"),
    ("address", "住所"),
    ("term", "营业期限"),
    ("authority", "登记机关"),
    ("issue_date", "发证日期"),
    ("credit_code", "信用代码"),
]


class _FieldEditDialog(tk.Toplevel):
    def __init__(self, parent, fields: dict):
        super().__init__(parent)
        self.title("编辑字段 — 修正后重新翻译渲染")
        self.result: dict | None = None
        self._vars: dict[str, tk.StringVar] = {}

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 6))

        ttk.Label(
            body,
            text="修正中文源字段后点「重新翻译」，会跳过 OCR 直接重新翻译并渲染 PDF。",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 8))

        form = ttk.Frame(body)
        form.pack(fill="both", expand=True)
        for i, (key, label) in enumerate(EDITABLE_FIELDS):
            ttk.Label(form, text=label, width=10).grid(row=i, column=0, sticky="nw", pady=2)
            var = tk.StringVar(value=str(fields.get(key, "")))
            self._vars[key] = var
            if key == "scope":
                entry = tk.Text(form, width=70, height=6, font=("Microsoft YaHei", 9))
                entry.insert("1.0", var.get())
                entry.grid(row=i, column=1, sticky="ew", pady=2)
                self._vars[f"_scope_widget"] = entry
            else:
                ttk.Entry(form, textvariable=var, width=70).grid(
                    row=i, column=1, sticky="ew", pady=2)
        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="重新翻译", command=self._ok).pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()

    def _ok(self):
        out = {}
        for key, _label in EDITABLE_FIELDS:
            if key == "scope" and "_scope_widget" in self._vars:
                out[key] = self._vars["_scope_widget"].get("1.0", "end").strip()
            else:
                out[key] = self._vars[key].get().strip()
        self.result = out
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
