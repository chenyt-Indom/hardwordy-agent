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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-11-13 第三次复发（用户报「在对话里干活时黑窗一个接一个停不下来」）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
现场实测（15ms 高频采样 EnumWindows，抓一闪而过的窗口）：

    起 python.exe 不加 flags  → CASCADIA_HOSTING_WINDOW_CLASS "Windows Terminal"
                                 存活 1.438 秒
    起 git --version 不加 flags → CASCADIA_HOSTING_WINDOW_CLASS "Windows Terminal"
                                 存活 0.249 秒
    起 git --version 加 NO_WINDOW → 0 个窗口

结论：
    ① 罪魁不是 conhost，而是 **Windows Terminal 接管了控制台**
       （窗口类 CASCADIA_HOSTING_WINDOW_CLASS）；
    ② CREATE_NO_WINDOW 依然有效，问题只是**有些调用路径没被覆盖到**。

所以这一版加了第二道保险 `_patch_execute_child()`：
    直接 patch `subprocess.Popen._execute_child` —— 那是真正调
    CreateProcess 的最后一站，**任何调用方都绕不过去**（包括
    subprocess.run / call / check_output / 第三方库内部起的进程）。
    在那一层无条件补上 CREATE_NO_WINDOW（显式要 CREATE_NEW_CONSOLE 的除外）。
"""

import os

# CREATE_NO_WINDOW：不为新进程创建控制台窗口
CREATE_NO_WINDOW = 0x08000000

# 非 Windows 上是 0（POSIX 没有这个概念，传 0 等于不设置）
NOWIN = CREATE_NO_WINDOW if os.name == "nt" else 0

_CREATE_NEW_CONSOLE = 0x00000010   # 调用方明确要窗口，尊重它
_DETACHED_PROCESS = 0x00000008     # 必须剥掉，否则必弹窗（见下）


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
# 第一道：patch Popen.__init__（调用方**没写** creationflags 时补上）
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

    if not getattr(_sp.Popen, "_nowin_patched", False):
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
        def _patched_init(self, *args, **kwargs):
            try:
                cf = int(kwargs.get("creationflags", 0) or 0)
                if not (cf & _CREATE_NEW_CONSOLE):
                    cf = (cf & ~_DETACHED_PROCESS) | CREATE_NO_WINDOW
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

    # 第二道保险一并装上
    _patch_execute_child()
    return True


# ---------------------------------------------------------------------------
# 第二道（2026-11-13 新增）：patch Popen._execute_child —— 最后一站，绕不过去
# ---------------------------------------------------------------------------
# 为什么还要这一层：
#     第一道只补「没写 creationflags」的情况。但只要某条路径显式写了
#     creationflags（哪怕是 0 以外的其它值，或第三方库自己算了一遍），
#     第一道就不动它 —— 于是又弹。
#     实测三次复发都是"某条路径漏了"，所以干脆在 CreateProcess 前一刻
#     无条件补上，从机制上让它不可能漏。
#
# _execute_child 的签名（CPython 3.12+）：
#     (self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env,
#      startupinfo, creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite,
#      errread, errwrite, restore_signals, gid, gids, uid, umask,
#      start_new_session, process_group, ...)
#   去掉 self 后 creationflags 在 args[8]、startupinfo 在 args[7]。
#   为了不被签名变化搞坏，这里用"按名字找 + 按位置兜底"的方式处理。
def _patch_execute_child():
    """在 CreateProcess 前一刻强制补 CREATE_NO_WINDOW。幂等。"""
    if os.name != "nt":
        return False
    import subprocess as _sp

    if getattr(_sp.Popen, "_nowin_exec_patched", False):
        return True
    if not hasattr(_sp.Popen, "_execute_child"):
        return False

    _orig = _sp.Popen._execute_child

    def _patched(self, *args, **kwargs):
        try:
            # ① 关键字形式
            if "creationflags" in kwargs:
                cf = int(kwargs.get("creationflags") or 0)
                if not (cf & _CREATE_NEW_CONSOLE):
                    kwargs["creationflags"] = (cf & ~_DETACHED_PROCESS) | CREATE_NO_WINDOW
            # ② 位置参数形式（正常路径都走这里）
            elif len(args) > 8:
                a = list(args)
                cf = int(a[8] or 0)
                if not (cf & _CREATE_NEW_CONSOLE):
                    a[8] = (cf & ~_DETACHED_PROCESS) | CREATE_NO_WINDOW
                    args = tuple(a)
            else:
                # ③ 都没给 —— 用实例上的值（Popen.__init__ 会存 self.creationflags）
                cf = int(getattr(self, "creationflags", 0) or 0)
                if not (cf & _CREATE_NEW_CONSOLE):
                    if len(args) > 8:
                        a = list(args); a[8] = (cf & ~_DETACHED_PROCESS) | CREATE_NO_WINDOW
                        args = tuple(a)

            # ④ startupinfo：位置参数在 args[7]，没有就补一个隐藏的
            si = kwargs.get("startupinfo")
            if si is None and len(args) > 7:
                si = args[7]
            if si is None:
                si = _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                if len(args) > 7:
                    a = list(args); a[7] = si; args = tuple(a)
                else:
                    kwargs["startupinfo"] = si
        except Exception:
            pass   # 任何意外都不能影响子进程启动
        return _orig(self, *args, **kwargs)

    _sp.Popen._execute_child = _patched
    _sp.Popen._nowin_exec_patched = True
    return True


def status():
    """自检：当前进程里补丁到底装上没有（用于 /api/diag 和启动日志）。

    用户报过「明明改了却还在闪」—— 有个能直接看的状态比猜有用。
    """
    import subprocess as _sp
    import sys
    return {
        "os": os.name,
        "executable": sys.executable,
        "nowin_value": hex(NOWIN),
        "init_patched": bool(getattr(_sp.Popen, "_nowin_patched", False)),
        "exec_patched": bool(getattr(_sp.Popen, "_nowin_exec_patched", False)),
    }


def probe(cmd=None):
    """真起一个子进程，回报实际传给 CreateProcess 的 creationflags。

    比"看代码里写没写"可靠 —— 补丁有没有生效，一测就知道。
    """
    import subprocess as _sp
    seen = {}

    orig = _sp.Popen._execute_child

    def _spy(self, *args, **kwargs):
        try:
            if "creationflags" in kwargs:
                seen["creationflags"] = int(kwargs.get("creationflags") or 0)
            elif len(args) > 8:
                seen["creationflags"] = int(args[8] or 0)
            seen["startupinfo"] = "given" if (kwargs.get("startupinfo") or (len(args) > 7 and args[7])) else "none"
        except Exception:
            pass
        return orig(self, *args, **kwargs)

    _sp.Popen._execute_child = _spy
    try:
        _sp.run(cmd or ["cmd", "/c", "echo", "probe"],
                capture_output=True, timeout=15)
    except Exception as exc:
        seen["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        _sp.Popen._execute_child = orig

    cf = seen.get("creationflags", 0)
    seen["has_no_window"] = bool(cf & CREATE_NO_WINDOW)
    seen["hex"] = hex(cf)
    return seen
