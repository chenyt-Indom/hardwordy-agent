"""Windows 上起子进程时隐藏控制台窗口 —— 治「小黑窗反复弹出又消失」。

问题现象（2026-11-10 用户反馈）：
    用着用着总有一些后台窗口弹出来、又消失，反复出现。

根因：
    Windows 上 `subprocess.run/Popen` 启动**控制台程序**（git.exe / python.exe /
    nvidia-smi.exe / node.exe …）时，系统会为它分配一个控制台，也就是
    conhost.exe + 一个黑色窗口。程序跑完（git 常常几十毫秒）窗口立刻消失，
    看起来就是「闪一下」。一次操作起几个子进程，就会闪好几下。

实测证据（本机抓 Win32_ProcessStartTrace）：
    起子进程时确实会连带创建 conhost.exe / OpenConsole.exe；
    不加 CREATE_NO_WINDOW 就必然出窗。

修法：
    给 subprocess 传 creationflags=CREATE_NO_WINDOW（0x08000000）。
    它让子进程**不分配控制台**，窗口根本不会出现，stdout/stderr 仍照常能收
    （因为我们都是 capture_output / DEVNULL，不依赖真实控制台）。

用法：
    from .procwin import NOWIN
    subprocess.run(cmd, capture_output=True, creationflags=NOWIN)
    # 或者更省事（自动只在 Windows 加，且不覆盖已有值）：
    subprocess.run(cmd, capture_output=True, **win_kw())
"""

import os

# CREATE_NO_WINDOW：不为新进程创建控制台窗口
CREATE_NO_WINDOW = 0x08000000

# 非 Windows 上是 0（POSIX 没有这个概念，传 0 等于不设置）
NOWIN = CREATE_NO_WINDOW if os.name == "nt" else 0


def win_kw(extra=None):
    """返回可以直接 ** 展开进 subprocess 的关键字参数。

    调用方自己已经给了 creationflags 时不覆盖（尊重原意图）。
    """
    kw = {}
    if NOWIN:
        kw["creationflags"] = NOWIN
    if extra:
        kw.update(extra)
    return kw
