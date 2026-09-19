# -*- coding: utf-8 -*-
"""一键打包脚本.

用法: python build.py
- 自动安装 pyinstaller (若缺)
- 用 注册工具.spec 构建 onedir 产物到 dist/注册工具/
- 构建后自动在 dist/注册工具/ 旁边创建用户数据空目录(资料/中国执照库/pool_data/auth/logs),
  并拷贝 config.json / institutions.json / 机构名.txt 供首次运行使用
"""
import os
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "注册工具.spec"
DIST = ROOT / "dist" / "注册工具"

def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    print("[build] 安装 pyinstaller…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

def build() -> None:
    print("[build] 开始 PyInstaller 构建(onedir)…")
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
    ])

def fix_msvc_runtime() -> None:
    """用系统/Python 目录下的正确 MSVC 运行时 DLL 覆盖 dist 里的不兼容版本。

    onnxruntime.dll 编译时链接的 VCRUNTIME140/MSVCP140 版本必须与运行时一致,
    否则 DllMain 初始化失败("动态链接库初始化例程失败")。
    torch 自带的旧版 MSVC DLL 会被 PyInstaller 优先收集, 导致 MSVCP140(14.36)
    与 VCRUNTIME140(14.42) 版本不匹配。
    """
    internal = DIST / "_internal"
    if not internal.exists():
        return
    pydir = Path(sys.executable).resolve().parent
    sysdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    fixed = []
    for dll in ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
        src = pydir / dll
        dst = internal / dll
        if src.exists() and dst.exists():
            shutil.copy2(src, dst)
            fixed.append(dll)
    # MSVCP140 用系统版本(与 VCRUNTIME140 同一 MSVC redistributable)
    for dll in ("MSVCP140.dll", "MSVCP140_1.dll"):
        src = sysdir / dll
        dst = internal / dll
        if src.exists() and dst.exists():
            shutil.copy2(src, dst)
            fixed.append(dll)
    if fixed:
        print(f"  修复 MSVC 运行时: {', '.join(fixed)}")


def seed_user_data() -> None:
    """在 dist/注册工具/ 旁边创建首次运行需要的空目录与可编辑配置."""
    if not DIST.exists():
        print(f"[build] 警告: 未找到 {DIST}")
        return
    print("[build] 创建用户数据目录…")
    for d in ("资料", "中国执照库", "pool_data", "auth", "logs"):
        (DIST / d).mkdir(exist_ok=True)
    # 拷贝可编辑配置(若存在则不覆盖, 保留用户修改)
    for f in ("config.json", "institutions.json", "机构名.txt"):
        src = ROOT / f
        dst = DIST / f
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            print(f"  拷贝 {f}")
    fix_msvc_runtime()
    print(f"[build] 完成 → {DIST}")
    print("  双击 dist/注册工具/注册工具.exe 启动")

if __name__ == "__main__":
    ensure_pyinstaller()
    build()
    seed_user_data()
