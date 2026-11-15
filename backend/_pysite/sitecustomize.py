# -*- coding: utf-8 -*-
"""让**任何** Python 子进程自动装上「黑窗防护」补丁。

原理
----
Python 启动时若在 sys.path 上找到 `sitecustomize`，会自动 import 它。
`procwin.ensure_child_python_env()` 把本目录塞进 PYTHONPATH，
于是后端的所有 Python 子进程（regress 子进程 / run_python 的 snippet /
selfdev_run 的子进程 / 用户脚本）都继承这个 PYTHONPATH，
它们再起进程时就不会弹黑窗了。

为什么需要
----------
现场证据（data/procwin_watch.jsonl）：
    PseudoConsoleWindow | cmd.exe | 父链 pythonw(子进程) ← pythonw(后端)
子进程没装补丁，它起的进程就是裸的 —— 起一次弹一次。

安全性
------
· 全程 try/except，出错绝不影响子进程本身的运行；
· 项目根只 append 到 sys.path 末尾，不抢占用户自己的同名模块。
"""

import os
import sys

try:
    if os.name == "nt":
        _here = os.path.dirname(os.path.abspath(__file__))       # backend/_pysite
        _root = os.path.dirname(os.path.dirname(_here))          # 项目根
        if _root not in sys.path:
            sys.path.append(_root)
        from backend import procwin as _pw
        _pw.install_nowin_patch()
except Exception:                       # noqa: BLE001
    # 补丁装不上只是会闪窗，绝不能让子进程跑不起来
    pass
