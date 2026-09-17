@echo off
rem GBK encoded - do not re-save as UTF-8
chcp 936 > nul
cd /d "%~dp0"
title TikTok�̾����� �Զ�ע�Ṥ��

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

echo ��������, �״����л��Զ���װ����, ���Ժ�...
%PYEXE% run_gui.py %*
if errorlevel 1 goto :fail
exit /b 0

:nopy
echo.
echo [����] ������û�п��õ� Python��
echo �뵽 https://www.python.org/downloads/ ���ذ�װ(һ·Ĭ����һ��),
echo ��װ��ɺ�����˫�����ļ����ɡ�
echo.
pause
exit /b 1

:fail
echo.
echo [����] ����ʧ��, ��ϸ��Ϣ��ͬĿ¼ gui.log, �ɽ�ͼ������
pause
exit /b 1
