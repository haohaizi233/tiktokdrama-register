@echo off
rem GBK encoded - do not re-save as UTF-8
chcp 936 > nul
cd /d "%~dp0"
title TikTok短剧中心 自动注册工具

set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto :nopy
%PYEXE% -c "import sys" >nul 2>nul
if errorlevel 1 set "PYEXE="
if not defined PYEXE goto :nopy

echo 正在启动, 首次运行会自动安装依赖, 请稍候...
%PYEXE% run_gui.py %*
if errorlevel 1 goto :fail
exit /b 0

:nopy
echo.
echo [错误] 电脑上没有可用的 Python。
echo 请到 https://www.python.org/downloads/ 下载安装(一路默认下一步),
echo 安装完成后重新双击本文件即可。
echo.
pause
exit /b 1

:fail
echo.
echo [出错] 启动失败, 详细信息见同目录 gui.log, 可截图反馈。
pause
exit /b 1
