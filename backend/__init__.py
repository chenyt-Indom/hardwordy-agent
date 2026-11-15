# -*- coding: utf-8 -*-
"""Hardwordy Agent后端包。

⚠️ 这里装「黑窗防护」补丁（2026-11-15 用户第 4 次报「改程序时黑窗反复弹出又消失」）

为什么从 main.py 挪到这里：
    以前补丁只在 backend/main.py 里装，于是**子进程完全没有保护**。
    现场证据（data/procwin_watch.jsonl）：

        PseudoConsoleWindow | cmd.exe | pid=9948
        父链: pythonw(57328，我们的子进程) ← pythonw(19028，后端)

    也就是说：regress 起的子进程、run_python 的 snippet、selfdev_run 的子进程，
    它们自己再起进程（cmd / python / node）时没有任何防护，起一次弹一次窗。

    放在包的 __init__ 里 → 任何 `import backend.*` 的子进程都会自动装上。

实测依据（同机，每种组合起 6 次 cmd.exe，8ms 采样控制台窗口）：
    不加 flags            → 7 个控制台窗口（Terminal / ConsoleWindowClass / PseudoConsole）
    只 CREATE_NO_WINDOW   → 0 个
    所以只要补丁覆盖到，就不会再弹。
"""

import os as _os

_NOWIN_OK = None
try:
    if _os.name == "nt":
        from . import procwin as _procwin

        _NOWIN_OK = _procwin.install_nowin_patch()
        # 顺带让**孙子进程**也受保护（把 _pysite 塞进 PYTHONPATH）
        _procwin.ensure_child_python_env()
except Exception:                      # noqa: BLE001
    # 装不上不影响主流程，但绝不能因此让 import backend 失败
    _NOWIN_OK = False
