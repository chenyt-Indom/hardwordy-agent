@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
title 本地多模态助手（源码版 · 含 DeepSeek）
cd /d "D:\local-multimodal-src"

echo ============================================================
echo   本地多模态助手（源码版）
echo   含：本地 30B + DeepSeek API 双通道
echo ============================================================
echo.

rem ===== 启动前预检：专防「改完代码重启就起不来」=====
echo [1/4] 检查代码能否启动...
py -3 rescue.py --precheck
if not errorlevel 1 goto START

echo.
echo [警告] 代码预检没通过 —— 这正是「双击没反应」的原因。
echo        正在自动回滚到最近的健康版本（当前状态会先备份）...
echo.
py -3 rescue.py --auto --reason "启动前预检失败"
if errorlevel 1 goto FAIL
echo.
echo [完成] 已回滚，继续启动。
echo.

:START
rem 先清掉占用 8000 的旧 python 进程，避免端口冲突闪退
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /PID %%i /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [2/4] 正在启动...
start "" pyw -3 run.py

echo [3/4] 等待服务就绪（最多 60 秒）...
py -3 rescue.py --wait-health 60
if not errorlevel 1 goto READY

echo.
echo [警告] 服务没起来（健康检查不通）。
echo        正在自动回滚到最近的健康版本...
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /PID %%i /F >nul 2>&1
)
timeout /t 1 /nobreak >nul
py -3 rescue.py --auto --reason "启动后健康检查失败"
if errorlevel 1 goto FAIL
echo [完成] 已回滚，重新启动...
start "" pyw -3 run.py
py -3 rescue.py --wait-health 60
if errorlevel 1 goto FAIL

:READY
echo.
echo [4/4] 服务已就绪，窗口应该已经弹出来了。
rem 把这次成功启动记成「可回滚版本」，下次崩了就能滚回这里
py -3 rescue.py --snapshot --tag 启动成功 >nul 2>&1
timeout /t 5 /nobreak >nul
exit /b 0

:FAIL
echo.
echo [失败] 自动救急没成功。
echo        请双击「回滚救急.bat」手动回滚到更早的版本。
echo        救急日志： data\rescue.log
echo.
pause
exit /b 1
