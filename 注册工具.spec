# -*- coding: utf-8 -*-
"""PyInstaller spec - 打包成 onedir exe.

捆绑资源(glossary/assets, 只读)放 _MEIPASS;
用户数据(pool_data/auth/logs/资料/中国执照库/config.json等)放exe旁边(paths.APP_ROOT).

构建: python build.py  (或 pyinstaller 注册工具.spec)
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Playwright + OCR/翻译 全量收集(含原生扩展和模型文件)
datas = []
binaries = []
hiddenimports = []

for pkg in ("playwright", "cn2an", "pypinyin", "qrcode", "openai",
            "onnxruntime", "rapidocr", "fitz", "pymupdf", "cv2", "PIL",
            "rapidocr_onnxruntime", "google.protobuf"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# translators 是纯 Python 的 HTTP 客户端, 不需要 collect_all(会拉入重型传递依赖)
hiddenimports += ["translators"]

# py-wechat-ocr 的 wcocr 原生扩展
for pkg in ("wechat_ocr", "wcocr"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# 捆绑只读资源(glossary + assets) → _MEIPASS
datas += [
    ("glossary", "glossary"),
    ("assets", "assets"),
]

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "openpyxl",
        "PIL._tkinter_finder",
        # OCR 引擎的动态导入
        "rapidocr_onnxruntime",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["rthook_onnxruntime.py"],
    excludes=[
        "pytest", "tests",
        # 多 Qt 绑定冲突, 应用不用 Qt
        "PyQt6", "PySide6", "PyQt5", "PySide2",
        # 重型科学计算/IDE 包, 非应用依赖(translators 等传递拉入)
        "matplotlib", "scipy", "pandas", "numba", "llvmlite",
        "IPython", "jedi", "parso", "black", "blib2to3",
        "zmq", "pyzmq", "notebook", "jupyter",
        "win32com", "pythoncom", "pywintypes",
        # yt-dlp 被 openai/playwright 传递拉入, 应用不用
        "yt_dlp",
        # rapidocr 的 pytorch/tensorrt 后端, 应用只用 onnxruntime 后端
        "torch", "tensorrt", "tensorboard",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="注册工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="注册工具",
)
