# -*- coding: utf-8 -*-
"""TikTok短剧中心 自动注册工具 - 入口

用法: 双击 启动.bat, 或命令行 python run_gui.py
- 当前解释器缺tkinter时, 自动寻找并切换到带tkinter的Python
- 首次运行自动安装依赖(playwright + Chromium), 之后秒开
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(ROOT / "gui.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [init] {msg}\n")
    except OSError:
        pass

def _find_tkinter_pythons() -> list[str]:
    """收集候选解释器命令(已验证可用tkinter): py launcher 各版本 + PATH中的所有python"""
    candidates: list[str] = []

    def add(cmd_prefix: list[str]) -> None:
        try:
            r = subprocess.run(cmd_prefix + ["-c", "import tkinter"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                candidates.append(" ".join(cmd_prefix))
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError, OSError):
            pass

    # py launcher 常见版本, 从新到旧
    for tag in ("-3.13", "-3.12", "-3.11", "-3.10", "-3"):
        add(["py", tag])
    # PATH 中的 python (where python)
    try:
        out = subprocess.run(["where", "python"], capture_output=True,
                             text=True, timeout=30).stdout
        for line in out.splitlines():
            line = line.strip()
            if line and line.lower().endswith(".exe"):
                add([f'"{line}"'])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return candidates

def ensure_tkinter() -> None:
    _log(f"启动: 解释器={sys.executable}")
    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass
    _log("当前Python缺少tkinter(GUI必需), 正在寻找其他Python…")
    for cmd in _find_tkinter_pythons():
        if Path(sys.executable).resolve().as_posix() in cmd.replace('"', "").lower():
            continue
        _log(f"切换到: {cmd}")
        r = subprocess.run(f'{cmd} "{Path(__file__)}" ' + " ".join(sys.argv[1:]), shell=True)
        sys.exit(r.returncode)
    input("[初始化] 没有找到带tkinter的Python。\n"
          "请安装官方Python(https://www.python.org/downloads/, 默认选项即可)后重试。\n按回车退出…")
    sys.exit(1)

def ensure_dependencies() -> None:
    for mod in ("playwright", "openpyxl"):
        try:
            __import__(mod)
            continue
        except ImportError:
            pass
        _log(f"检测到缺少 {mod}, 正在自动安装(仅首次)…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", mod])
        if r.returncode != 0:
            input(f"[初始化] {mod} 安装失败, 请检查网络(或换国内pip源)后重试。按回车退出…")
            sys.exit(1)
        _log(f"{mod} 安装完成")

    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    exe = Path(p.chromium.executable_path)
    p.stop()
    if not exe.exists():
        _log("正在下载 Chromium 浏览器(约150MB, 仅首次)…")
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if r.returncode != 0:
            input("[初始化] Chromium 下载失败, 请检查网络后重试。按回车退出…")
            sys.exit(1)
        _log("Chromium 就绪")

def main() -> None:
    ensure_tkinter()
    ensure_dependencies()
    import gui_app
    auto = 0
    if "--auto" in sys.argv:
        i = sys.argv.index("--auto")
        auto = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].lstrip("-").isdigit() else 1
    gui_app.run(smoke="--smoke" in sys.argv, auto=auto)

if __name__ == "__main__":
    main()
