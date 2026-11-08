import subprocess
import os

# 1) 杀掉占用 8000 的旧 python 进程（我之前启动的 uvicorn）
r = subprocess.run(['netstat', '-ano'], capture_output=True, text=True, errors='ignore')
killed = []
for line in r.stdout.splitlines():
    if ':8000' in line and 'LISTENING' in line:
        pid = line.strip().split()[-1]
        print('杀掉旧进程 PID', pid)
        subprocess.run(['taskkill', '/PID', pid, '/F'], capture_output=True)
        killed.append(pid)
if not killed:
    print('8000 端口空闲')

# 2) 生成启动脚本（GBK）
CRLF = chr(13) + chr(10)
lines = [
    '@echo off',
    'chcp 936 >nul',
    'title 本地多模态助手（源码版 · 含 DeepSeek）',
    'cd /d "D:\\local-multimodal-src"',
    '',
    'echo ============================================================',
    'echo   本地多模态助手（源码版）',
    'echo   含：本地 30B + DeepSeek API 双通道',
    'echo   正在启动，窗口马上弹出...',
    'echo ============================================================',
    'echo.',
    '',
    'rem 先清掉占用 8000 的旧 python 进程，避免端口冲突闪退',
    'for /f "tokens=5" %%i in (\'netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"\') do (',
    '  taskkill /PID %%i /F >nul 2>&1',
    ')',
    'timeout /t 1 /nobreak >nul',
    '',
    'rem 用 pyw 无控制台启动，窗口由 run.py 里的 pywebview 弹出',
    'start "" pyw -3 run.py',
    '',
    'rem 5 秒后本窗口自动关闭',
    'timeout /t 5 /nobreak >nul',
    'exit',
]
content = CRLF.join(lines) + CRLF

p = r'D:\local-multimodal-src\启动助手.bat'
open(p, 'wb').write(content.encode('gbk'))
print('已生成:', p, os.path.getsize(p), '字节')

# 3) 复制到桌面
desk = r'C:\Users\19853\Desktop\本地AI助手（源码版）.bat'
open(desk, 'wb').write(content.encode('gbk'))
print('已复制到桌面:', desk, os.path.getsize(desk), '字节')

# 4) 校验
raw = open(p, 'rb').read()
print()
print('--- 校验（GBK 解码）---')
for line in raw.decode('gbk').split(CRLF):
    if line.strip():
        print(' ', line)
