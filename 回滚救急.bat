@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
title 救急 · 回滚到上个可用版本
cd /d "D:\local-multimodal-src"

echo ============================================================
echo    程序打不开 / 起不来？这里能回滚
echo    回滚前会先自动备份当前状态，不怕越救越坏
echo ============================================================
echo.

echo [1] 当前快照：
py -3 rescue.py --list
echo.

echo [2] 检查当前代码能否启动：
py -3 rescue.py --precheck
echo.

echo ------------------------------------------------------------
echo  即将自动回滚到最近的「健康」快照。
echo   - 会自动跳过起不来的快照，逐个往前试
echo   - 回滚前先把当前状态存成 bad_ 快照，可再回滚回来
echo  按 Ctrl+C 取消，按任意键继续...
echo ------------------------------------------------------------
pause >nul

py -3 rescue.py --auto --reason "用户双击回滚救急"
if errorlevel 1 goto FAIL

echo.
echo [3] 回滚完成，正在重新启动程序...
timeout /t 2 /nobreak >nul
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /PID %%i /F >nul 2>&1
)
timeout /t 1 /nobreak >nul
start "" pyw -3 run.py
echo.
echo 已发起启动，稍等几秒看窗口是否弹出。
echo 如果还是打不开，再双击本文件（会继续往更早的版本回滚）。
echo 救急日志： data\rescue.log
echo.
pause
exit /b 0

:FAIL
echo.
echo [失败] 自动回滚没能救回来。
echo   - 可能是所有健康快照都起不来
echo   - 可以手动指定更早的快照：
echo       py -3 rescue.py --rollback --snap snap_20261117_
echo   - 详细原因看： data\rescue.log
echo.
pause
exit /b 1
