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


# ---------------------------------------------------------------------------
# 全局兜底：把 subprocess.Popen 统一打上 CREATE_NO_WINDOW
# ---------------------------------------------------------------------------
# 为什么还要这一层：
#     只靠"在每个调用点手写 creationflags"永远会漏 —— 而且有几个模块
#     （backend/selfdev.py 的回撤机制自身）被锁成只读，改不了。
#     漏一处就闪一次窗，用户看到的就是"窗口闪来闪去"。
# 做法：
#     进程启动时 patch 一次 Popen.__init__，Windows 上自动补标志。
#     调用方显式要"新控制台 / 独立进程"时不动它，尊重原意图。
def install_nowin_patch():
    """给 subprocess.Popen 打全局补丁：Windows 上默认不分配控制台窗口。

    幂等：重复调用只生效一次。
    """
    if os.name != "nt":
        return False
    import subprocess as _sp

    if getattr(_sp.Popen, "_nowin_patched", False):
        return True

    _orig_init = _sp.Popen.__init__

    # 调用方若明确要「新控制台窗口」，尊重它 —— 那是真的要看得见的窗口。
    # ⚠️ DETACHED_PROCESS 不在此列（2026-11-12 用户报「迭代时反复弹黑窗」）：
    #    backend/selfdev.py 重启后端时用的是
    #       DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    #    实测（本机 EnumWindows 前后对比）：
    #       · 带 DETACHED_PROCESS               → 必然弹一个黑窗，
    #         窗口类 CASCADIA_HOSTING_WINDOW_CLASS，即 Win11 默认终端接管；
    #       · DETACHED | CREATE_NO_WINDOW       → 照样弹（这个组合压不住）；
    #       · 去掉 DETACHED、只留 CREATE_NO_WINDOW → 窗口数为 0。
    #    所以这里把 DETACHED_PROCESS 剥掉再补 CREATE_NO_WINDOW。
    #    不影响「独立于父进程」的语义：CREATE_NO_WINDOW 本身就会给新进程
    #    一个**自己的隐藏控制台**（不继承父进程的），父进程退出也不牵连它；
    #    CREATE_BREAKAWAY_FROM_JOB 等其它标志一律原样保留。
    _KEEP = 0x00000010   # CREATE_NEW_CONSOLE：调用方明确要窗口，不覆盖
    _DETACHED = 0x00000008   # DETACHED_PROCESS：必须剥掉，否则必弹窗

    def _patched_init(self, *args, **kwargs):
        try:
            cf = int(kwargs.get("creationflags", 0) or 0)
            if not (cf & _KEEP):
                cf = (cf & ~_DETACHED) | CREATE_NO_WINDOW
                kwargs["creationflags"] = cf
                # 第二道保险（2026-11-11 用户报「一叠叠 Terminal 窗口」）：
                # Win11 若把默认终端设成 Windows Terminal，CREATE_NO_WINDOW
                # 并不能完全阻止它接管控制台 —— 会拉起 OpenConsole.exe 开窗口，
                # 客户端进程被强杀后窗口还会残留成一叠僵尸窗口（实测残留 48 个）。
                # 再显式要求「以隐藏方式创建窗口」，把这条路径也堵上。
                if kwargs.get("startupinfo") is None:
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0   # SW_HIDE
                    kwargs["startupinfo"] = si
        except Exception:
            pass
        return _orig_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _patched_init
    _sp.Popen._nowin_patched = True
    return True
