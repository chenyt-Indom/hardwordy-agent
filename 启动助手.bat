@echo off
chcp 936 >nul
title 本地多模态助手（源码版 · 含 DeepSeek）
cd /d "D:\local-multimodal-src"

echo ============================================================
echo   本地多模态助手（源码版）
echo   含：本地 30B + DeepSeek API 双通道
echo   正在启动，窗口马上弹出...
echo ============================================================
echo.

rem 先清掉占用 8000 的旧 python 进程，避免端口冲突闪退
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /PID %%i /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

rem 用 pyw 无控制台启动，窗口由 run.py 里的 pywebview 弹出
start "" pyw -3 run.py

rem 5 秒后本窗口自动关闭
timeout /t 5 /nobreak >nul
exit
