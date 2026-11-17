@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
title 检查代码能否启动（不影响正在运行的程序）
cd /d "D:\local-multimodal-src"

echo ============================================================
echo   检查当前代码是否正常
echo    - 不会杀掉正在运行的程序
echo    - 会用临时端口 8011 另起一次服务来验证
echo ============================================================
echo.

echo [1/2] 语法 / 导入检查...
py -3 rescue.py --precheck
if errorlevel 1 goto BAD

echo.
echo [2/2] 真起服务验证（临时端口 8011，约 5 秒）...
py -3 rescue.py --deep-check
if errorlevel 1 goto BAD

echo.
echo ------------------------------------------------------------
echo [结果] 一切正常：当前代码可以正常启动。
echo        主程序没有被打断，可以继续用。
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:BAD
echo.
echo ------------------------------------------------------------
echo [结果] 检查没通过 —— 这正是「双击启动没反应」的原因。
echo        想回到能用的版本，请双击「回滚救急.bat」。
echo ------------------------------------------------------------
echo.
pause
exit /b 1
