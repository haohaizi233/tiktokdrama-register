# -*- coding: utf-8 -*-
"""Runtime hook: 打包后给 onnxruntime/capi 加 DLL 搜索路径.

onnxruntime_pybind11_state.pyd 依赖 onnxruntime.dll / DirectML.dll,
在 onedir 布局下它们在 _internal/onnxruntime/capi/, 需显式加入搜索路径。
"""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _base = Path(sys._MEIPASS)
    for _sub in ("onnxruntime", "onnxruntime/capi"):
        _d = _base / _sub
        if _d.is_dir():
            try:
                os.add_dll_directory(str(_d))
            except (OSError, AttributeError):
                pass
