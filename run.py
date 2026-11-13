# -*- coding: utf-8 -*-
"""本地多模态助手 —— 桌面启动入口

双击 exe（或 `py -3 run.py`）即启动：
1. 在后台线程拉起本地 FastAPI 服务（uvicorn）
2. 用 pywebview 弹出独立的桌面应用窗口，内嵌加载本地界面（不依赖外部浏览器）
3. 窗口关闭后自动回收后台服务线程，干净退出。

模型推理由本机 Ollama（默认 qwen3-vl:8b）完成；文生图/图片微改由随应用
分发的本地 SD 模型（sd_model 目录）完成，全程离线。
"""
import os
import sys

# ⚠️ 2026-11-09：pythonw.exe 没有控制台，sys.stdout/stderr 是 None。
#   只要程序里任何一处 print()/logging 往 stdout 写，就会抛 AttributeError 直接退出 ——
#   表现就是"双击快捷方式（pythonw）一闪而过，服务没起来，界面发不出消息"。
#   这里在**入口最前面**把 None 的流重定向到 data/app.log，既救活 pythonw，也留下日志。
if sys.stdout is None or sys.stderr is None:
    try:
        _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(_log_dir, exist_ok=True)
        _f = open(os.path.join(_log_dir, "app.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _f
        if sys.stderr is None:
            sys.stderr = _f
    except Exception:
        pass

import threading
import time
import urllib.request

# ⚠️⚠️ pythonw.exe 下 sys.stdout / sys.stderr 都是 None（无控制台）。
# 后端代码里有大量 print()（诊断日志、[cigshape] 异常等），一执行就抛
#   AttributeError: 'NoneType' object has no attribute 'write'
# 然后进程瞬间退出 —— 用户双击快捷方式看到的就是"闪一下没了"。
# 实测 2026-11-08：桌面 .lnk 指向 pythonw + run.py 时，进程活不到 1 秒。
# 这里兜住：没有标准输出就丢到 devnull，功能不受影响。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import uvicorn

from backend import config
from backend.main import app

# 监听地址：默认 0.0.0.0，同一局域网里的手机/平板也能打开。
#   · 想恢复「只有本机能访问」，启动前设环境变量 MM_HOST=127.0.0.1
#   · ⚠️ 应用本身没有登录口令，绑 0.0.0.0 等于同网段任何人都能用（含跑代码/改源码），
#     在不信任的网络（宿舍、公司、公共 WiFi）里建议改回 127.0.0.1。
HOST = (os.environ.get("MM_HOST") or "0.0.0.0").strip() or "0.0.0.0"
PORT = 8000
# 浏览器打开 / 健康检查统一走回环地址：
#   0.0.0.0 只是「监听所有网卡」的意思，它不是一个能去连的地址。
URL = f"http://127.0.0.1:{PORT}/"

# 由外部启动器（桌面快捷方式用的「启动窗口.pyw」）拉起时置 1：
# 此时本进程只负责后端服务，窗口交给启动器显示，避免弹出两个窗口。
NO_WINDOW = os.environ.get("MM_NO_WINDOW") == "1"
# ★ 2026-11-12：把「本进程会不会建窗口」写进环境，供 /api/health 报告。
#   另一个实例（双击启动的那个）拿到 window=False 就知道对面是无窗口后端，
#   于是可以接管，而不是傻乎乎地「已有实例 → 打开浏览器 → 退出」。
os.environ["MM_SELF_WINDOW"] = "0" if NO_WINDOW else "1"


def _serve(server: uvicorn.Server) -> None:
    """在后台线程运行 uvicorn 服务。"""
    config.load_config()  # 确保配置文件生成
    server.run()


def _wait_until_up(timeout: float = 60.0) -> bool:
    """轮询等待后端就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _alert(msg: str, title: str = "本地多模态助手") -> None:
    """无控制台时用系统弹窗提示失败原因，避免"闪退"且没有任何反馈。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        print(msg)


# --------------------------------------------------------------------------
#  语音唤醒：把窗口弹到最前
# --------------------------------------------------------------------------
WINDOW_TITLE = "Hardwordy Agent"


def _focus_window_win32() -> bool:
    """按标题找到应用窗口并置前（不依赖 pywebview 对象，因此对
    "窗口由外部启动器创建"的情况也有效）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if WINDOW_TITLE in (buf.value or ""):
                found.append(hwnd)
                return False        # 找到一个就停
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        # 最小化了要先还原，否则置前只能看到任务栏闪烁
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        else:
            user32.ShowWindow(hwnd, 5)      # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _old_window_exists() -> bool:
    """按标题检测是否已有应用窗口（不依赖 pywebview 对象）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if WINDOW_TITLE in (buf.value or ""):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(_cb, 0)
        return bool(found)
    except Exception:
        return False


def _wait_old_window_gone(timeout: float = 6.0) -> None:
    """重启时：等上一个实例的窗口先关掉，再开自己的窗口。

    为什么需要（2026-11-13 用户反馈「先弹出新页面、老页面再关」）：
    旧进程退出时，uvicorn 先释放 8000 端口，pywebview 的窗口要晚几百毫秒
    才真正销毁。新进程 `_wait_port_free()` 一拿到端口就往下走、立刻
    create_window，于是两个窗口短暂并存 —— 用户看到的就是
    「新窗口先冒出来，老窗口才消失」。

    这里在开窗前轮询窗口标题，旧窗口消失（或超时）再继续。
    正常启动时根本没有旧窗口，`_old_window_exists()` 立刻返回 False，零开销。
    """
    if os.name != "nt":
        return
    t0 = time.time()
    waited = False
    while time.time() - t0 < timeout:
        if not _old_window_exists():
            if waited:
                _wlog("旧窗口已消失（等了 %.1f 秒），开始建新窗口" % (time.time() - t0))
            return
        waited = True
        time.sleep(0.15)
    _wlog("旧窗口 %.0f 秒内没关掉，仍然继续建新窗口（避免卡死）" % timeout)


def _make_wake_focus(window=None):
    """生成"被唤醒时"的回调：先试 pywebview 对象，再退回 Win32 按标题找。"""
    def _focus() -> None:
        if window is not None:
            try:
                window.show()
                window.restore()
            except Exception:
                pass
        _focus_window_win32()
    return _focus


def _autostart_voice() -> None:
    """启动时自动打开麦克风监听，之后**一直看着它**（见 `_voice_watchdog`）。

    容器里没有麦克风，这里会失败 —— 失败就静默跳过，界面上点 🎤 会给出提示，
    不能让"没有麦克风的部署"因为自动启动而报错。
    """
    try:
        from backend import config as _cfg
        if not _cfg.load_config().get("voice_auto_start", True):
            return
    except Exception:
        return
    try:
        from backend import main as _main
        # 模型要在后台加载，稍等一下再开，避免和 ollama 抢资源
        time.sleep(3)
        r = _main.voice_start()
        print(f"[voice] 自动监听：{r}")
    except Exception as e:
        print(f"[voice] 自动监听失败（可忽略）：{e}")
        return
    try:
        _voice_watchdog(_main)
    except Exception as e:
        print(f"[voice] 看门狗退出（可忽略）：{e}")


def _voice_watchdog(main_mod) -> None:
    """盯着监听线程：它要是**自己没了**，就把它拉起来。

    ⚠️ 为什么必须有这个：实测遇到过 `running:false, error:null` —— 监听线程
    已经退出，但**一点线索都没有**（不是"打开麦克风失败"，也不是"没找到模型"），
    界面上只会显示「语音已停止」。用户看到的就是"怎么喊都没反应"。

    两条边界：
      · **用户自己点 🎤 关掉的不重开** —— 由 `VoiceListener.needs_restart()` 判断，
        开关的主动权始终在用户手里；
      · 容器里没有麦克风 → 失败后**指数退避**（30s → … → 10 分钟），不会刷日志。
    """
    fails = 0
    while True:
        time.sleep(30 if fails == 0 else min(600, 30 * (2 ** fails)))
        try:
            from backend import config as _cfg
            if not _cfg.load_config().get("voice_auto_start", True):
                continue
            vl = getattr(main_mod, "_voice", None)
            if vl is None or not vl.needs_restart():
                continue
            r = main_mod.voice_start()
            if r.get("ok"):
                fails = 0
                print("[voice] 监听线程不在了，已自动重新开始")
            else:
                fails += 1
        except Exception:
            fails += 1


def _running_health(timeout: float = 2.0) -> dict | None:
    """端口上那个实例的 /api/health；不是本应用（或连不上）返回 None。"""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(URL.rstrip("/") + "/api/health")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return d if isinstance(d, dict) and "online" in d else None
    except Exception:
        return None


def _already_running() -> bool:
    """已经有实例在跑吗（端口上的 /api/health 能通且是本应用的应答）。

    ⚠️ 为什么必须判（2026-11-08 实测踩到）：
      重复启动时第二个进程 bind 失败，但 `_wait_until_up()` 会**连上第一个实例**
      并返回 True —— 于是第二个进程一直挂着不退，成了僵尸：
        · 用户以为开了两个窗口，其实只有一个能用；
        · 它持有旧代码、旧线程（实测还锁着模型目录，导致"卸载模型"报拒绝访问）。
      所以：发现已在运行 → 本实例直接退出，把注意力交给已经跑着的那个。
    """
    return _running_health() is not None


def _port_in_use(port: int = PORT, timeout: float = 0.35) -> bool:
    """端口上有没有人在监听。

    ⚠️⚠️ 2026-11-13 实测推翻旧做法：**不能用 bind 探测**。
    原来这里 `sk.bind(("127.0.0.1", PORT))` 成功就当端口空闲。但 Windows 上
    即使另一个 socket 正监听 `0.0.0.0:PORT`，再 bind `127.0.0.1:PORT` 依然成功
    （实测：18097 被 0.0.0.0 监听时，bind 127.0.0.1 照样不报错）。
    于是 `_wait_port_free()` **永远返回 True**、0.0 秒就过，等于没等 ——
    这就是 data/app.log 里那 3 次 `[Errno 10048]` 一直没被挡住的原因。

    现在改成两步探测（2026-11-13 逐个实测过）：
      ① bind("0.0.0.0", port)，绑不上 = 被占。能挡住「别人监听 0.0.0.0」这个主场景
         （实测 True），且反复探测 30 次结果稳定。
      ② 再 connect 一次补漏 —— 万一对方**只绑了 127.0.0.1**，①能绑上（实测 False，
         会漏），但 connect 连得上（实测 True）。
    两步组合后：0.0.0.0 监听 / 127.0.0.1 监听 / 真实 8000 / 释放后，全部判断正确。
    ⚠️ ②只调一次，不要循环重试 —— listen backlog 小的服务端会被反复 connect
       占满，之后新连接被 RST，反而误判成「空闲」（实测踩到：1.5 秒才释放的端口
       0.65 秒就被判空了）。
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.bind(("0.0.0.0", port))
    except OSError:
        return True                      # ① 绑不上 → 确实被占
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk2:
            sk2.settimeout(timeout)
            if sk2.connect_ex(("127.0.0.1", port)) == 0:
                return True              # ② 只绑了回环地址的情况
    except OSError:
        pass
    return False


def _wait_port_free(timeout: float = 40.0) -> bool:
    """等 8000 端口空出来（自开发重启时用：旧进程还没退干净，新进程别急着 bind）。"""
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        if not _port_in_use():
            return True
        _t.sleep(0.3)
    return False


def _kill_pid(pid: int) -> bool:
    """结束指定进程（只在「接管无窗口后端」时用）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
        return True
    except Exception:
        return False


def _wlog(msg: str) -> None:
    """窗口诊断日志。pythonw 没有控制台，出问题时这是唯一能留下的痕迹。

    2026-11-12：为查「点重启后进程活着但没窗口」加的。写完就不删了 ——
    以后凡是"有没有窗口"的疑难，直接看 data/window_debug.log。
    """
    try:
        import time as _t
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "window_debug.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s pid=%d %s\n" % (_t.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def main() -> None:
    config.load_config()
    if os.environ.get("MM_WAIT_PORT") == "1":
        _wait_port_free()
    # 自开发触发的重启：新进程起来时旧进程可能还占着端口，别把自己挡回去
    if os.environ.get("MM_SKIP_ALREADY_CHECK") != "1":
        _h = _running_health()
        if _h is not None:
            # ★★ 2026-11-12：对面是个**没有窗口**的后端（多半是自迭代重启留下的，
            #   它继承了 MM_NO_WINDOW=1 却没人给它开窗口）→ 接管：结束它，自己开窗。
            #   以前这里一律「已有实例 → 打开浏览器 → 退出」，于是桌面窗口永远不出现，
            #   用户看到的就是「自迭代之后双击打不开」。
            #   对面窗口是好的，或者本进程本来就该无窗口 → 保持原来的行为。
            if (not NO_WINDOW) and (_h.get("window") is False):
                _old = int(_h.get("pid") or 0)
                print("[run] 检测到无窗口实例（PID %s），正在接管…" % _old)
                _kill_pid(_old)
                _wait_port_free(20.0)
            else:
                print("[run] 已有实例在运行（%s），本实例退出。" % URL)
                # ⚠️ 只 print 的话，从桌面快捷方式启动（没有控制台）会表现为
                #   "双击了没反应"。这里把已经跑着的界面打开到默认浏览器，
                #   用户至少能立刻看到窗口。
                try:
                    import webbrowser
                    webbrowser.open(URL)
                except Exception:
                    pass
                return
    # log_level="info" → 打开 **access log**（每个请求一行，含状态码）。
    # 原来用 "warning"，后端一个字都不记请求 —— 排查"界面第一次打开某项是空的/
    # 灰的"这类问题时，完全看不到前端到底发没发请求、请求是成功还是失败，
    # 只能靠猜。日志落在 %TEMP%\mm_backend.log，排查完可以调回 "warning"。
    # ★★ 2026-11-13：bind 之前再确认一次端口真的空了。
    # 为什么（日志实证，不是推测）：data/app.log 里有 3 次
    #     [Errno 10048] error while attempting to bind on address ('0.0.0.0', 8000)
    # 新进程在旧进程还没退干净时就 bind → 失败 → 自己这条 uvicorn 线程直接死掉；
    # 但紧接着 _wait_until_up() 却能连上（旧进程还在服务）→ 误判「服务已就绪」
    # → 去开窗口 → 几秒后旧进程退出、端口没了 → 页面所有请求全断。
    # 用户看到的就是「重启后新窗口一打开就被拦 / 连不上」。
    if not _wait_port_free(15.0):
        _wlog("端口 %d 等 15 秒仍被占用，仍然尝试启动（大概率会失败）" % PORT)
    else:
        _wlog("端口 %d 已空闲，开始启动服务" % PORT)

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT,
                                           log_level="info"))
    t = threading.Thread(target=_serve, args=(server,), daemon=True)
    t.start()

    if not _wait_until_up():
        # 服务起不来就退，避免白弹一个空白窗口
        server.should_exit = True
        _alert("本地服务启动失败。\n\n请依次检查：\n"
               "1) Ollama 是否已安装并正在运行\n"
               "2) 端口 8000 是否被其他程序占用\n"
               "3) 是否已拉取模型 qwen3-vl:8b")
        raise SystemExit("本地服务启动失败")

    # ★ 2026-11-13：再确认一次「8000 上应答的就是自己」。
    # _wait_until_up() 只验证"有人应答"，一旦上面 bind 失败（端口被旧进程占着），
    # 它连上的其实是旧实例 → 返回 True → 误判就绪 → 开窗口 → 旧进程一退页面全断。
    # 这正是用户说的「重启后新窗口一打开就被拦」。比对 pid 才能识破。
    _h_self = _running_health(2.0)
    _hp = int((_h_self or {}).get("pid") or 0)
    if _hp and _hp != os.getpid():
        _wlog("8000 上应答的是别的进程 pid=%d（本进程 %d），不开窗口"
              % (_hp, os.getpid()))
        _alert("8000 端口被另一个实例占用（PID %d）。\n\n"
               "本窗口不会正常工作，已停止启动。\n"
               "请先关掉那个实例（或结束该进程）再启动。" % _hp)
        server.should_exit = True
        raise SystemExit("端口被其他实例占用")
    _wlog("服务自检通过 pid=%d" % os.getpid())

    if NO_WINDOW:
        # 被外部启动器拉起：这里只保持后端服务存活，窗口由启动器负责，
        # 否则会和启动器各开一个窗口（用户看到两个）。
        # 窗口不由本进程创建，所以"唤醒置前"只能靠 Win32 按标题找。
        try:
            from backend import voice as _voice
            _voice.set_wake_hook(_make_wake_focus(None))
        except Exception:
            pass
        threading.Thread(target=_autostart_voice, daemon=True,
                         name="voice-autostart").start()
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            t.join(timeout=5)
        return

    try:
        import webview  # 延迟导入，避免源码环境未安装时阻塞后端
    except ImportError as exc:
        _wlog("导入 pywebview 失败: %r" % (exc,))
        _alert("缺少桌面窗口组件 pywebview：\n" + str(exc))
        raise

    # 重启时先等上一个实例的窗口关掉，再开自己的 —— 否则两个窗口会短暂并存
    _wait_old_window_gone()
    _wlog("准备创建窗口 NO_WINDOW=%s 标题=%s" % (NO_WINDOW, WINDOW_TITLE))

    window = webview.create_window(
        WINDOW_TITLE,
        URL,
        width=1280,
        height=840,
        min_size=(960, 640),
        text_select=True,
    )
    # 语音唤醒时要能把这个窗口弹到最前 —— 在这里注册回调
    try:
        from backend import voice as _voice
        _voice.set_wake_hook(_make_wake_focus(window))
    except Exception:
        pass
    threading.Thread(target=_autostart_voice, daemon=True,
                     name="voice-autostart").start()
    try:
        webview.start(private_mode=False)
        _wlog("webview.start 正常返回（窗口被关闭或启动失败）")
    except BaseException as exc:
        _wlog("webview.start 抛异常: %r" % (exc,))
        raise
    finally:
        server.should_exit = True
        t.join(timeout=5)


if __name__ == "__main__":
    main()