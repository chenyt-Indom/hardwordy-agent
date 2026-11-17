@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "rollback_console.py"
if errorlevel 1 pause
