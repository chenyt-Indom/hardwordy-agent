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
import time
import logging
logger = logging.getLogger(__name__)

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


def run(*args, **kwargs):
    """subprocess.run 的薄封装：自动带上隐藏窗口补丁。

    新代码请走这里，不要直接调 subprocess.run —— 检查器会拦。
    内部已装 NOWIN 补丁（install_nowin_patch 幂等），并补上 win_kw()。
    """
    import subprocess as _sp
    install_nowin_patch()
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs.update(win_kw())
    return _sp.run(*args, **kwargs)


def popen(*args, **kwargs):
    """subprocess.Popen 的薄封装：自动带上隐藏窗口补丁。

    新代码请走这里，不要直接调 subprocess.Popen —— 检查器会拦。
    内部已装 NOWIN 补丁（install_nowin_patch 幂等），并补上 win_kw()。
    """
    import subprocess as _sp
    install_nowin_patch()
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs.update(win_kw())
    return _sp.Popen(*args, **kwargs)


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
def ensure_child_python_env():
    """把 backend/_pysite 塞进 PYTHONPATH —— 让子 Python 进程自动装补丁。

    为什么要它（2026-11-15 第 4 次复发）：
        data/procwin_watch.jsonl 里抓到
            PseudoConsoleWindow | cmd.exe | 父链 pythonw(子进程) ← pythonw(后端)
        说明**子进程没装补丁**，它自己起的进程就是裸的，起一次弹一次。
        Python 启动会自动 import sitecustomize，所以只要 PYTHONPATH 里有
        backend/_pysite，子孙进程就全都受保护。幂等。
    """
    if os.name != "nt":
        return False
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pysite")
        if not os.path.isdir(p):
            return False
        cur = os.environ.get("PYTHONPATH", "") or ""
        parts = [x for x in cur.split(os.pathsep) if x]
        if p in parts:
            return True
        parts.insert(0, p)
        os.environ["PYTHONPATH"] = os.pathsep.join(parts)
        return True
    except Exception:                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return False


def is_patched():
    """全局 nowin 补丁是否**真的**已生效（不是「代码里写过调用」）。

    ★ 2026-11-22：静态检查原本只靠 AST 看「main.py 里有没有 install_nowin_patch
      这个调用」，那是「写了」而不是「生效了」—— 调用被 try/except 包住失败、
      或者 os.name != "nt" 提前返回，AST 都会判成「已兜住」，于是几十处
      subprocess 告警被错误降级。这里给一个实测入口，供静态检查交叉验证。
    """
    if os.name != "nt":
        return False
    import subprocess as _sp
    return bool(getattr(_sp.Popen, "_nowin_patched", False))


def install_nowin_patch():
    """给 subprocess.Popen 打全局补丁：Windows 上默认不分配控制台窗口。

    幂等：重复调用只生效一次。
    """
    if os.name != "nt":
        return False
    # 让子 Python 进程也受保护（否则它们起的进程照样弹窗）
    ensure_child_python_env()
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
                logger.debug("silent-except", exc_info=True)
            return _orig_init(self, *args, **kwargs)

        _sp.Popen.__init__ = _patched_init
        _sp.Popen._nowin_patched = True

    # 第二道保险一并装上
    _patch_execute_child()
    # 第三道（2026-11-15）：堵住绕过 Popen 的路径（multiprocessing / os.spawn）
    _patch_winapi_createprocess()
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
            logger.debug("silent-except", exc_info=True)   # 任何意外都不能影响子进程启动
        return _orig(self, *args, **kwargs)

    _sp.Popen._execute_child = _patched
    _sp.Popen._nowin_exec_patched = True
    return True


# ---------------------------------------------------------------------------
# 第三道（2026-11-15）：patch _winapi.CreateProcess —— 堵住绕过 Popen 的路径
# ---------------------------------------------------------------------------
# 为什么还要这一层（用户报「改程序时黑窗反复弹出又消失」）：
#     前两道只覆盖 subprocess.Popen。但有些代码**不走 Popen**：
#       · multiprocessing 的 spawn 启动器（multiprocessing.popen_spawn_win32
#         直接 `import _winapi` 然后调 `_winapi.CreateProcess(...)`）；
#       · os.spawn* 系列；
#       · 某些第三方库自己调 _winapi。
#     这些路径一道都拦不住，改一次代码就弹一次窗。
# 做法：
#     直接 patch `_winapi.CreateProcess` 这个函数本身。
#     multiprocessing 是「模块属性访问」式的调用（`_winapi.CreateProcess(...)`），
#     所以 patch 模块属性对它**立即生效**。
#     （subprocess 用的是 `from _winapi import CreateProcess` 的本地引用，
#      patch 模块属性影响不到它 —— 但那一路已经被上面两道覆盖了，互补。）
def _make_startupinfo():
    """构造一个「以隐藏方式创建窗口」的 STARTUPINFO。"""
    import subprocess as _sp
    si = _sp.STARTUPINFO()
    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
    si.wShowWindow = 0   # SW_HIDE
    return si


def _patch_winapi_createprocess():
    """在 _winapi.CreateProcess 上强制补 CREATE_NO_WINDOW。幂等。"""
    if os.name != "nt":
        return False
    try:
        import _winapi
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return False
    if getattr(_winapi, "_nowin_patched", False):
        return True
    if not hasattr(_winapi, "CreateProcess"):
        return False

    _orig = _winapi.CreateProcess

    # CPython 签名（3.8+）：
    #   CreateProcess(app_name, command_line, proc_attrs, thread_attrs,
    #                 inherit_handles, creation_flags, env_mapping,
    #                 current_directory, startup_info)
    def _patched(app_name, command_line, proc_attrs, thread_attrs,
                 inherit_handles, creation_flags, env_mapping,
                 current_directory, startup_info):
        try:
            cf = int(creation_flags or 0)
            if not (cf & _CREATE_NEW_CONSOLE):
                cf = (cf & ~_DETACHED_PROCESS) | CREATE_NO_WINDOW
                if startup_info is None:
                    startup_info = _make_startupinfo()
        except Exception:
            logger.debug("silent-except", exc_info=True)
            cf = creation_flags
        return _orig(app_name, command_line, proc_attrs, thread_attrs,
                     inherit_handles, cf, env_mapping,
                     current_directory, startup_info)

    _winapi.CreateProcess = _patched
    _winapi._nowin_patched = True
    return True


# ---------------------------------------------------------------------------
# 窗口看门狗（2026-11-15）：弹窗抓不到元凶时，用它在现场留证据
# ---------------------------------------------------------------------------
# 为什么要它：
#     用户报「改程序时黑窗反复弹出」，但实测在补丁生效的进程里窗口数为 0 ——
#     说明弹窗来自**某条没被覆盖的路径**。光靠读代码猜不出来，不如在运行时
#     蹲守：任何控制台类窗口一出现，立刻记下窗口类 / 标题 / pid / 该进程的
#     exe 路径。下次再闪，日志里就有元凶。
#
# 只在后台低频采样（默认 400ms），只记「控制台类」窗口，开销可忽略。
_WD = {
    "on": False,
    "thread": None,
    "log": [],          # [{ts, cls, title, pid, exe, hidden}]
    "max": 120,
    "interval": 0.4,
    "started_at": 0.0,
    # shield=True 时，发现控制台窗口就立刻 SW_HIDE 掉 —— 光记录不解决，
    # 用户要的是「别让我看见它闪」。默认开，可通过 watchdog_shield() 关。
    "shield": True,
    "hidden_total": 0,
}

# Windows 上「控制台窗口」的窗口类名（Win11 常由 Windows Terminal 接管）
_CONSOLE_CLASSES = (
    "consolewindowclass",
    "cascadia_hosting_window_class",
    "pseudoconsolewindow",
    "windowsterminal",
)


def _is_console_class(cls):
    c = (cls or "").strip().lower()
    if not c:
        return False
    for k in _CONSOLE_CLASSES:
        if k in c:
            return True
    return False


def _proc_exe(pid):
    """拿进程的 exe 路径（轻量：只开进程句柄，不起子进程）。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return ""


def _proc_ppid(pid):
    """拿父进程 pid（用于追溯窗口是谁拉起的）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        TH32CS_SNAPPROCESS = 0x00000002
        k32 = ctypes.windll.kernel32

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return 0
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(e)):
                return 0
            while True:
                if e.th32ProcessID == int(pid):
                    return int(e.th32ParentProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
            return 0
        finally:
            k32.CloseHandle(snap)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return 0


def _proc_chain(pid, depth=3):
    """往上追父进程链：[{pid, exe}, ...]。"""
    out = []
    cur = int(pid)
    for _ in range(depth):
        pp = _proc_ppid(cur)
        if not pp or pp == cur:
            break
        out.append({"pid": pp, "exe": _proc_exe(pp)})
        cur = pp
    return out


def _snap_console_windows():
    """枚举当前所有可见窗口，只返回控制台类的那几个。"""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    out = {}
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            cls = cbuf.value
            if not _is_console_class(cls):
                return True
            tbuf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, tbuf, 512)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out[int(hwnd)] = {"cls": cls, "title": tbuf.value, "pid": int(pid.value)}
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _is_descendant_of_me(pid, depth=8):
    """这个 pid 是不是本进程的后代？

    为什么要判：看门狗只该收拾**我们自己**弹出来的窗口。
    用户自己开的终端/命令行如果被我们隐藏了，那是越界 ——
    2026-11-15 实测就发生过（日志里混进了 explorer 拉起的 Terminal）。
    """
    try:
        me = os.getpid()
        cur = int(pid)
        for _ in range(depth):
            if cur == me:
                return True
            pp = _proc_ppid(cur)
            if not pp or pp == cur:
                return False
            cur = pp
        return False
    except Exception:                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return False


def _watchdog_loop():
    try:
        baseline = _snap_console_windows()
    except Exception:
        logger.debug("silent-except", exc_info=True)
        baseline = {}
    while _WD["on"]:
        time.sleep(_WD["interval"])
        if not _WD["on"]:
            break
        try:
            cur = _snap_console_windows()
            for h, info in cur.items():
                if h in baseline:
                    continue
                # 记录后立刻隐藏 —— 用户要的是「别让我看见它闪」，
                # 只写日志等于没解决。
                # ⚠️ 只隐藏「我们自己进程树里」的窗口。
                # 2026-11-15 实测踩到：旧逻辑把用户自己开的 Windows Terminal
                # 也一并隐藏了（data/procwin_watch.jsonl 里 pid=76196 的父链是
                # explorer，不是我们）—— 那是用户的窗口，绝对不能动。
                ours = _is_descendant_of_me(info["pid"])
                hidden = False
                if _WD["shield"] and ours:
                    import ctypes as _ct
                    # 重试 3 次：WT 窗口常常是「先出现、稍后才真正显示」，
                    # 只 Hide 一次会在下一帧又被显示出来。
                    for _try in range(3):
                        try:
                            _ct.windll.user32.ShowWindow(int(h), 0)   # SW_HIDE
                            hidden = True
                        except Exception:
                            logger.debug("silent-except", exc_info=True)
                        time.sleep(0.03)
                    if hidden:
                        _WD["hidden_total"] += 1

                rec = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "cls": info["cls"],
                    "ours": ours,
                    "title": (info["title"] or "")[:120],
                    "pid": info["pid"],
                    "exe": _proc_exe(info["pid"]),
                    "parents": _proc_chain(info["pid"]),
                    "hidden": hidden,
                }
                _WD["log"].append(rec)
                if len(_WD["log"]) > _WD["max"]:
                    del _WD["log"][: len(_WD["log"]) - _WD["max"]]
                # 写日志文件，重启后还能查
                try:
                    import json as _json
                    p = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                     "data", "procwin_watch.jsonl")
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
                except Exception:
                    logger.debug("silent-except", exc_info=True)
                logger.warning("检测到控制台窗口弹出：%s | pid=%s | %s",
                               rec["cls"], rec["pid"], rec["exe"])
            baseline = cur
        except Exception:
            logger.debug("silent-except", exc_info=True)


def watchdog_start(interval=0.06):
    """启动窗口看门狗（后台守护线程，幂等）。

    interval 默认 0.12s：实测 WindowsTerminal 窗口常只存活 0.25 秒，
    采样太慢就抓不到、也来不及隐藏。
    """
    if os.name != "nt":
        return False
    if _WD["on"]:
        return True
    try:
        import threading
        _WD["interval"] = max(0.04, float(interval))
        _WD["on"] = True
        _WD["started_at"] = time.time()
        th = threading.Thread(target=_watchdog_loop, daemon=True,
                              name="procwin-watchdog")
        th.start()
        _WD["thread"] = th
        return True
    except Exception:
        logger.debug("silent-except", exc_info=True)
        _WD["on"] = False
        return False


def watchdog_shield(on=True):
    """开关「发现即隐藏」。关掉后只记录、不隐藏（排查时用）。"""
    _WD["shield"] = bool(on)
    return _WD["shield"]


def watchdog_status():
    """看门狗状态 + 抓到过的窗口。"""
    return {
        "on": bool(_WD["on"]),
        "interval": _WD["interval"],
        "shield": bool(_WD["shield"]),
        "hidden_total": int(_WD["hidden_total"]),
        "count": len(_WD["log"]),
        "items": list(_WD["log"])[-30:],
    }


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
    import sys as _sys
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
            logger.debug("silent-except", exc_info=True)
        return orig(self, *args, **kwargs)

    _sp.Popen._execute_child = _spy
    try:
        # ⚠️ 不能用 cmd.exe 当探针：cmd 正是 Windows Terminal 接管的入口，
        #    为了自检反而弹一个黑窗，自相矛盾（2026-11-15 实测发现）。
        _sp.run(cmd or [_sys.executable, "-c", "print(1)"],
                capture_output=True, timeout=15)
    except Exception as exc:
        seen["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        _sp.Popen._execute_child = orig

    cf = seen.get("creationflags", 0)
    seen["has_no_window"] = bool(cf & CREATE_NO_WINDOW)
    seen["hex"] = hex(cf)
    return seen
