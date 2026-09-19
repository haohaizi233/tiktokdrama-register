# -*- coding: utf-8 -*-
"""统一路径: 区分打包后exe目录(用户数据)和捆绑资源目录(glossary/assets).

- 脚本模式: 两者都指向项目根目录
- PyInstaller冻结: APP_ROOT=exe所在目录(用户数据), BUNDLE_ROOT=_MEIPASS(捆绑资源)
"""
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
    BUNDLE_ROOT = Path(sys._MEIPASS).resolve()
else:
    APP_ROOT = Path(__file__).resolve().parent
    BUNDLE_ROOT = APP_ROOT
