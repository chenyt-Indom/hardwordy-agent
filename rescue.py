# -*- coding: utf-8 -*-
"""崩溃救急与全栈快照 —— **不依赖主程序、不 import backend**。

为什么必须单独一个文件
----------------------
用户报过（2026-11-17 原话）：
  「确保在程序全面完全崩溃时也能完成全栈回滚」
  「自迭代时程序崩溃连不上网络，点击自开发想要回滚到上个版本结果没有办法回滚」

崩溃时 `backend.version.rollback()` 根本够不着：它要靠前端按钮 → HTTP 接口
→ 主进程，而主进程正是死掉的那个。所以本文件：

  · **只用标准库**（zipfile / subprocess / urllib），不 import backend 任何东西
  · 由 `启动助手.bat` 在**主程序之外**调用，也能由用户双击 `回滚救急.bat`
  · 快照含源码 + config.json + data 状态文件（不只源码，比 git 那套更全）

与 backend/version.py 的分工
----------------------------
  · version.py：日常「版本列表 / 单文件回滚 / 撤销回滚」，走 git，需要界面
  · rescue.py：**兜底**。程序起不来时用，不依赖 git、不依赖界面

命令
----
  py -3 rescue.py --snapshot [--tag 说明]   建快照（记录当前是否健康）
  py -3 rescue.py --list                   列出快照
  py -3 rescue.py --precheck               检查 import backend.main 能否成功
  py -3 rescue.py --health                 探测 8000 端口健康接口
  py -3 rescue.py --wait-health 40         等健康接口就绪（最多 40 秒）
  py -3 rescue.py --auto [--deep]          自动回滚到最近「健康」的快照并验证
  py -3 rescue.py --rollback [--snap X]    回滚到指定（或最近）快照
  py -3 rescue.py --clean-snapshots        按保留策略清理旧快照

退出码：0 = 成功 / 健康；1 = 失败（bat 用 errorlevel 判断）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile

# ---------------------------------------------------------------- 路径定位
# ⚠️⚠️ 2026-11-17（打包 11.17b 实测踩到，致命）：PyInstaller 冻结后
#   `__file__` 的目录是 exe 同级，而 backend/ frontend/ 在 **_internal** 里。
#   照旧算法 collect_files() 只能收集到 rescue.py 自己 1 个文件 ——
#   快照是空壳，回滚等于什么都没恢复（实测：1 个文件）。
#   所以必须区分两个根：
#     · SRC_ROOT —— 源码/静态资源根（frozen 时 = _internal，含 backend/ frontend/）
#     · ROOT     —— 数据根（frozen 时 = exe 同级，含 data/ config.json）
FROZEN = bool(getattr(sys, "frozen", False))


def _resolve_roots():
    if FROZEN:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable or ""))
        src = getattr(sys, "_MEIPASS", "") or os.path.join(exe_dir, "_internal")
        if not os.path.isdir(src):
            src = exe_dir
        return src, exe_dir
    here = os.path.dirname(os.path.abspath(__file__))
    # ⚠️ 打包产物里 rescue.py 与 _internal 同级。如果用户机器上**装了 Python**、
    #   直接 `py -3 rescue.py --list` 跑（FROZEN 为 False），照旧算法会去 exe 同级
    #   找 frontend/backend —— 那里没有，快照又变成空壳。
    #   所以这里再探一次 _internal：有它就是打包产物。
    inner = os.path.join(here, "_internal")
    if os.path.isdir(inner) and (
            os.path.isdir(os.path.join(inner, "frontend"))
            or os.path.isdir(os.path.join(inner, "backend"))):
        return inner, here
    return here, here


SRC_ROOT, ROOT = _resolve_roots()
# 是否处于「打包产物」环境（含独立运行 rescue.py 的情况）：两个根不同即为打包产物
PACKAGED = FROZEN or os.path.normcase(os.path.abspath(SRC_ROOT)) != os.path.normcase(os.path.abspath(ROOT))
DATA = os.path.join(ROOT, "data")
SNAP_DIR = os.path.join(DATA, "rescue_snapshots")
STATE_PATH = os.path.join(DATA, "rescue_state.json")
LOG_PATH = os.path.join(DATA, "rescue.log")
# ⚠️ 2026-11-17 实测：两个线程同时回滚到不同快照时，一个会报「成功」而磁盘实际是另一个版本
#   （另一边的自动退回把它的结果盖掉了）。回滚必须串行 —— 用锁文件互斥。
LOCK_PATH = os.path.join(DATA, ".rescue_rollback.lock")
# 正在回滚的那个线程的 id（None=没人回滚）。
# ⚠️ 不能用「进程级布尔标志」：实测多线程下另一个线程也会看到 True 而放行，
#    照样抓到半回滚状态。必须精确到线程 —— 回滚内部建 bad_ 快照时是同线程，放行；
#    别的线程/别的进程想建快照，一律拒绝。
_LOCK_OWNER_TID = None

PORT = 8000
VERIFY_PORT = 8011          # 验证用的临时端口，避开主程序的 8000
KEEP_SNAPS = 8              # 保留最近几个自动快照
KEEP_BAD = 2                # 「回滚前坏状态」额外保留几个
KEEP_MAN = 5                # 手动快照（man_）单独计数，不被自动快照挤掉
KEEP_ITER = 30              # 自我迭代自动版本（it_）：每批迭代改动前建一个，约 145MB
KEEP_EXE = 5                # exe 版本保留几个（每个约 45MB，5 个约 225MB）
EXE_POOL_MAX_MB = 400       # 单个 exe 超过这个大小就不入库（防误收巨型文件）

# ---------------------------------------------------------------- 快照范围
# 源码目录：整个收（体积小），但排掉缓存与产物
SRC_DIRS = ["backend", "frontend", "launcher"]

# 根目录下按扩展名收（run.py / 启动助手.bat / 各测试脚本 等）
SRC_ROOT_EXTS = {
    ".py", ".pyw", ".bat", ".cmd", ".md", ".txt", ".spec",
    ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml",
}

# 目录名黑名单：出现在任何层级都跳过
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".git", "node_modules", ".venv", "venv", "env",
    "build", "dist", "_backup_out", "rembg", "vendor", "data", "models",
    "saved_images", "image_library", "sessions", "docker", "_qa_out",
    "logs", "handoff", "_bench", "dist-lite",
    # ⚠️ 2026-11-17（打包实测）：打包产物的 _internal/backend 里带着 68 个
    #   `_feature_tests` 测试脚本（待实现功能清单，不参与运行）。
    #   收进快照只会让快照无谓变大、并让 clean 的删除范围变宽。
    "_feature_tests",
}

# 文件名黑名单（通配）
EXCLUDE_FILE_PAT = (
    "*.pyc", "*.pyo", "*.bak", "*.bak-*", "*.bak_*", "*.bak.*",
    "*_tmp*", "*_old*", "*.log", "*.log.1",
    # ⚠️ 密钥不进快照：回滚到旧密钥会让当前可用的 API 直接失效，
    #    那是「越救越坏」。密钥请单独保管（data/secrets.json）。
    "secrets.json",
)

# data 根目录下的「状态文件」白名单（用户指定：技能 / 定时任务 / 自迭代 / 待批准）
DATA_STATE_FILES = [
    "skills.json",            # 技能
    "schedules.json",         # 定时任务
    "schedule_vecs.json",     # 定时任务的语义索引（scheduler.py 用）—— 只滚 schedules.json 会让两者错位
    "selfloop.json",          # 反思循环（含待批准闸门配置）
    "self_maint.json",        # 自我维护
    "version_task.json",      # 任务步数记录
    "version_undo.json",      # 撤销回滚记录
    "chat_model.json",        # 对话模型选择
    "feat_radar.json",        # 功能雷达
    "boot_audit.json",        # 启动自检历史
    "boot_gate.json",         # 重启闸门
    "selfdev_journal.jsonl",  # 自开发留痕
    "_env_backup.json",       # 环境备份
]

# data 下要收的「状态子目录」：只取 .json / .jsonl，且跳过它们自己的备份与日志
DATA_STATE_DIRS = {
    "self_iterate": {".json", ".jsonl"},
}
DATA_SUBDIR_EXCLUDE = {"backups", "agent", "logs", "snapshots", "_archive"}


def _pid_alive(pid):
    """进程还活着吗（只读探测，不改任何东西）。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259        # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True          # 探测不了就当它还活着，宁可等也不误抢
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class RollbackBusy(Exception):
    """另一个回滚正在进行。"""


def _acquire_lock(timeout=45.0, stale_secs=180):
    """抢回滚锁。返回 (fd, interrupted)。

    interrupted=True 表示发现**上一次回滚没正常结束**（持锁进程已死，或锁文件过久没释放）
    —— 那时磁盘可能停在半回滚状态，调用方要如实提示用户再滚一次。

    抢不到锁抛 RollbackBusy；锁文件建不了（目录只读等）返回 (None, ...) 退化为无锁，
    不能因为锁建不了就完全不让回滚。
    """
    interrupted = False
    start = time.time()
    try:
        os.makedirs(DATA, exist_ok=True)
    except Exception:
        pass
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            except Exception:
                pass
            return fd, interrupted
        except FileExistsError:
            stale = False
            try:
                age = time.time() - os.path.getmtime(LOCK_PATH)
                txt = ""
                try:
                    with open(LOCK_PATH, encoding="utf-8", errors="ignore") as f:
                        txt = f.read().strip()
                except Exception:
                    pass
                pid = int(txt) if txt.isdigit() else 0
                if pid == os.getpid() and _LOCK_OWNER_TID is None:
                    # ⚠️ 2026-11-17 实测：锁文件里是本进程 pid、本进程也活着、
                    #   又没超 180 秒 —— 原来的判定会认为「锁有效」，于是建快照和
                    #   回滚全被拒，提示「另一个回滚正在进行」，可本进程根本没有任何
                    #   线程在回滚（是上次误用/异常留下的残留锁）。要干等 180 秒才能
                    #   自愈。现在：pid 是自己、且本进程没有持锁线程 → 必然是残留，
                    #   立即接管。
                    #   注意必须判 _LOCK_OWNER_TID：多线程下 A 线程持锁时 pid 也是自己，
                    #   那时不能当残留（否则 B 线程会把 A 的锁抢掉，并发保护失效）。
                    stale = True
                elif pid and not _pid_alive(pid):
                    stale = True                     # 持锁进程已死 → 上次被强杀
                elif age > stale_secs:
                    stale = True                     # 锁太久没释放，多半也死了
                if stale:
                    interrupted = True
                    try:
                        os.remove(LOCK_PATH)
                    except OSError:
                        pass
                    continue
            except FileNotFoundError:
                continue                             # 刚好被别人释放，立刻重试
            if time.time() - start > timeout:
                raise RollbackBusy(
                    "另一个回滚正在进行（锁已被占用 %.0f 秒），本次未改动任何文件。"
                    "等它结束再试。" % (time.time() - start))
            time.sleep(0.15)
        except OSError:
            return None, interrupted                 # 建不了锁文件 → 不阻断


def _release_lock(fd):
    """释放回滚锁。

    ⚠️ 2026-11-17 实测：`_acquire_lock()` 返回的是 (fd, interrupted) 元组，而这里只收
    单个 fd —— 签名不对称，一旦有人写成 `_release_lock(_acquire_lock())`（误用），
    `os.close(元组)` 抛 TypeError 被吞掉 → **文件句柄没关** → Windows 上打开中的文件
    删不掉 → `os.remove` 也失败被吞 → **锁文件永久残留**。而残留锁里记的是本进程
    自己的 pid、进程还活着、又没超 180 秒 → 下次建快照/回滚全被拒，
    提示「另一个回滚正在进行」，实际根本没人在回滚，要干等 180 秒。
    所以这里：① 收到元组自动解包；② 关句柄失败要记日志；③ 删除重试几次。
    """
    if isinstance(fd, tuple):
        fd = fd[0] if fd else None
    try:
        if fd is not None:
            os.close(fd)
    except Exception as e:
        _log("释放锁时关闭句柄失败：%r" % (e,))
    for _i in range(5):
        try:
            if not os.path.exists(LOCK_PATH):
                return
            os.remove(LOCK_PATH)
            return
        except FileNotFoundError:
            return
        except OSError:
            # Windows 上刚 close 的句柄有短暂延迟，重试
            time.sleep(0.05 * (_i + 1))
    if os.path.exists(LOCK_PATH):
        _log("警告：锁文件删不掉（%s），下次回滚可能被误判为「正在进行」" % LOCK_PATH)


def _log(msg):
    """写救急日志。崩溃排查就靠它 —— 一定要在最早失败点也能写。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _say(line)


def _say(msg):
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass


def _excluded_file(name):
    low = name.lower()
    for pat in EXCLUDE_FILE_PAT:
        rx = "^" + re.escape(pat).replace(r"\*", ".*") + "$"
        if re.match(rx, low, re.I):
            return True
    return False


def _python_exe():
    """拿到**有控制台**的 python.exe。

    ⚠️ 由 pyw / pythonw 启动时 sys.executable 是 pythonw.exe，它没有标准输出，
    子进程的报错会被吞掉 —— 而这里恰恰要靠子进程的报错判断代码坏没坏。
    """
    exe = sys.executable or ""
    base = os.path.basename(exe).lower()
    if base.startswith("pythonw"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(cand):
            return cand
    return exe or "python"


def _child_env():
    """给子进程的环境：不要弹窗、不要弹浏览器、不要自检。"""
    env = dict(os.environ)
    env["MM_NO_WINDOW"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("MM_SELF_WINDOW", None)
    return env


def _decode(raw):
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _sha256_file(path, chunk=1 << 20):
    """算文件 sha256。读不了返回 ""（调用方按「无法核对」处理，不当成失败）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except OSError:
        return ""


def _project_id():
    """项目稳定身份：data/project_id 里的一个 uuid。

    为什么需要：manifest 里记了 root，但**路径会变**（项目被移动/改名/换机器），
    靠路径判断「是不是同一个项目」会把「项目搬了个位置」误判成「外来快照」，
    导致移动项目后全部历史快照都不能用。
    这个文件放在 data/ 里，会跟着项目目录一起走 —— 项目移动后 id 不变，
    从别的项目拷来的快照 id 不同，正好能区分开。
    建不出来就返回 ""（调用方按「无法判定」处理，不因此拒绝回滚）。
    """
    p = os.path.join(ROOT, "data", "project_id")
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                v = f.read().strip()
            if v:
                return v
        os.makedirs(os.path.dirname(p), exist_ok=True)
        v = uuid.uuid4().hex
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        return v
    except OSError:
        return ""


# ---------------------------------------------------------------- 文件收集
def collect_files():
    """返回 [(绝对路径, zip 内相对路径)]。相对路径统一用 / 分隔。"""
    out = []
    seen = set()

    def add(abs_path, rel):
        rel = rel.replace("\\", "/")
        if rel in seen or not os.path.isfile(abs_path):
            return
        seen.add(rel)
        out.append((abs_path, rel))

    # 1) 源码目录（frozen 时在 _internal 里，不在 exe 同级）
    for d in SRC_DIRS:
        base = os.path.join(SRC_ROOT, d)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
            for f in fn:
                if _excluded_file(f):
                    continue
                ap = os.path.join(dp, f)
                add(ap, os.path.relpath(ap, SRC_ROOT))

    # 2) 根目录白名单扩展名
    for f in sorted(os.listdir(ROOT)):
        ap = os.path.join(ROOT, f)
        if not os.path.isfile(ap):
            continue
        if os.path.splitext(f)[1].lower() not in SRC_ROOT_EXTS:
            continue
        if _excluded_file(f):
            continue
        add(ap, f)

    # 3) config.json（.gitignore 里排除了，但它决定行为，必须进快照）
    cfg = os.path.join(ROOT, "config.json")
    if os.path.isfile(cfg):
        add(cfg, "config.json")

    # 4) data 状态文件
    for name in DATA_STATE_FILES:
        ap = os.path.join(DATA, name)
        if os.path.isfile(ap) and not _excluded_file(name):
            add(ap, "data/" + name)

    # 5) data 状态子目录（只要 .json/.jsonl）
    for sub, exts in DATA_STATE_DIRS.items():
        base = os.path.join(DATA, sub)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [x for x in dn if x not in DATA_SUBDIR_EXCLUDE]
            for f in fn:
                if os.path.splitext(f)[1].lower() not in exts:
                    continue
                if _excluded_file(f):
                    continue
                ap = os.path.join(dp, f)
                add(ap, os.path.relpath(ap, ROOT))

    return out


def _dst_path(rel):
    """把快照内的相对路径映射回真实落盘位置。

    ⚠️ frozen 时 backend/ frontend/ 在 _internal（SRC_ROOT），
    而 data/ 与根目录单层文件（config.json）在 exe 同级（ROOT）。
    搞错的话回滚会把 data 写进 _internal 里 —— 界面看着"成功"，实际没生效。
    """
    rel = (rel or "").replace("\\", "/")

    # ⚠️⚠️ 2026-11-17（安全审查）：快照 zip 可能被篡改、或从别处拷来。
    #   原实现直接把 zip 条目名 join 到项目目录上，于是
    #     "../../evil.py"        → 写到项目外面
    #     "C:/Windows/xxx.dll"   → Windows 上 join 遇绝对路径直接返回它本身
    #   这叫 Zip Slip。任何越界条目一律拒绝，绝不落盘。
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError("非法快照路径：%r" % rel)
    if any(seg == ".." for seg in rel.split("/")):
        raise ValueError("非法快照路径（含 ..）：%r" % rel)

    if rel.startswith("data/"):
        dst, base = os.path.join(ROOT, rel.replace("/", os.sep)), ROOT
    elif "/" not in rel:        # config.json / 启动助手.bat 这类根目录文件
        dst, base = os.path.join(ROOT, rel), ROOT
    else:
        dst, base = os.path.join(SRC_ROOT, rel.replace("/", os.sep)), SRC_ROOT

    # 最后用 abspath 兜一次（防 symlink / 大小写 / 分隔符花样）
    real = os.path.normcase(os.path.abspath(dst))
    base_real = os.path.normcase(os.path.abspath(base))
    if real != base_real and not real.startswith(base_real + os.sep):
        raise ValueError("非法快照路径（越界）：%r" % rel)
    return dst


# ⚠️⚠️ 2026-11-17 实测（第 N 轮全面检测）：
#   「快照能不能被写回去」和「路径对不对」是两件事，_dst_path 只管后者。
#   实测两个真实故障：
#     ① 目标文件是只读属性（git 检出、从光盘拷贝、用户手动设只读都会这样）
#        → open(dst,"wb") 抛 PermissionError → failed → 判失败
#        → **自动退回也写不进去** → 项目停在半新半旧的坏状态，只能人工救。
#        一个只读文件就能让整个回滚机制瘫痪，这不能接受。
#     ② 目标路径被占成了一个目录（同名目录残留）
#        → open(dst,"wb") 抛 PermissionError → 该文件永远恢复不了。
#   所以写盘前必须先「腾位置」：去掉只读属性；是目录就先删掉。
def _prepare_dst(dst):
    """写盘前腾出位置：清只读属性、把同名目录清掉。返回 (ok, 说明)。"""
    try:
        # ⚠️⚠️ 2026-11-17 实测（第 N 轮）：open(dst,"wb") 写的是「路径指向的那份数据」，
        #   不是「这个路径本身」。于是两种链接都会把内容写到别处去：
        #     · 符号链接 → 写到链接目标（可能在项目外）
        #     · 硬链接   → 和原文件是同一份数据，写它等于改原文件（实测把项目外的
        #                  OUTSIDE.txt 内容改成了项目里的内容，而路径检查完全看不出来）
        #   路径检查（_dst_path）只能保证「路径在项目内」，管不了「这份数据属于谁」。
        #   唯一可靠的做法：先把目标位置的条目删掉（断链），再新建一个全新文件。
        #   删硬链接只是把链接计数减一，原文件本身不受影响；删符号链接不动目标。
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
            return True, "removed-dir"
        if os.path.lexists(dst):
            try:
                os.chmod(dst, 0o666)
            except Exception:
                pass
            try:
                os.remove(dst)      # 断开一切链接，保证接下来写的是新文件
            except Exception as exc:
                return False, "无法替换已有条目：%s: %s" % (type(exc).__name__, exc)
        return True, "ok"
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


# ⚠️ 2026-11-17 实测：一个 306 KB 的快照让回滚往磁盘写了 300 MB，还报「回滚成功」。
#   这是 zip bomb（高压缩比）：攻击者或损坏的快照能让磁盘瞬间塞满。
#   真正的快照不会这样（本项目全部源码压缩后才 4.8 MB），所以设硬上限，
#   超了就拒绝回滚，绝不去写。
MAX_ENTRY_MB = 96          # 单个文件上限
MAX_TOTAL_MB = 512         # 单次回滚解压总量上限


def _entry_too_big(zf, name, total):
    """检查 zip 条目大小。返回 (超限说明 or "", 该条目字节数)。"""
    try:
        info = zf.getinfo(name)
        sz = int(getattr(info, "file_size", 0) or 0)
    except Exception:
        return "", 0
    if sz > MAX_ENTRY_MB * 1024 * 1024:
        return ("%s 解压后 %.0f MB，超过单文件上限 %d MB（疑似 zip bomb）"
                % (name, sz / 1048576.0, MAX_ENTRY_MB)), sz
    if total + sz > MAX_TOTAL_MB * 1024 * 1024:
        return ("解压总量超过 %d MB（疑似 zip bomb），已拒绝" % MAX_TOTAL_MB), sz
    return "", sz


def _git_head():
    try:
        p = subprocess.run(["git", "-c", "core.quotepath=false", "rev-parse", "--short", "HEAD"],
                           cwd=ROOT, capture_output=True, timeout=20)
        if p.returncode == 0:
            return _decode(p.stdout).strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 健康探测
def http_health(port=PORT, timeout=2.0):
    """探测 /api/health。返回 (ok, 说明)。"""
    url = "http://127.0.0.1:%d/api/health" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(200)
            return r.status == 200, _decode(body)[:120]
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def port_in_use(port=PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def _clean_import_code(body):
    """拼一段「确保 import 的是本项目」的代码。

    ⚠️⚠️ 必须清 sys.modules（2026-11-17 实测踩到，差点让整套救急形同虚设）：
      backend/_pysite/sitecustomize.py 会在**每个** Python 子进程启动时执行
      `from backend import procwin`，把**真实项目的 backend 包**提前注册进
      sys.modules。此后子进程里的 `import backend.main` 直接复用这个已注册的包，
      **根本不会去看当前工作区**。
      实测：在临时目录把 main.py 写成语法错误，预检照样返回「成功」。
      修法：先 insert(0, ROOT) 再清掉 sys.modules 里所有 backend*，让它重新按路径找。
    """
    # ⚠️⚠️ 2026-11-17 二次踩到：光清 sys.modules 不够。
    #   实测：PYTHONPATH 里带 `backend\_pysite` 时，sitecustomize 会把**别的项目**的
    #   backend 目录挂进 sys.path；清掉 sys.modules 后重新 import，解析到的仍是那个项目
    #   → 把目标项目的 main.py 删掉，预检照样返回「import 成功」（假阳性）。
    #   修法：① 先把 sys.path 里所有「含 backend 子目录、但不是本项目」的项摘掉；
    #         ② 导入后再断言模块真的落在 SRC_ROOT 下（verify_import 里做）。
    return (
        "import sys, os\n"
        "_ROOT = %r\n"
        "_ap_root = os.path.abspath(_ROOT)\n"
        "_keep = []\n"
        "for _p in sys.path:\n"
        "    try:\n"
        "        _ap = os.path.abspath(_p or '.')\n"
        "    except Exception:\n"
        "        _keep.append(_p); continue\n"
        "    if _ap == _ap_root or not os.path.isdir(os.path.join(_ap, 'backend')):\n"
        "        _keep.append(_p)\n"
        "sys.path[:] = _keep\n"
        "sys.path.insert(0, _ROOT)\n"
        "for _m in [k for k in list(sys.modules) if k == 'backend' or k.startswith('backend.')]:\n"
        "    del sys.modules[_m]\n"
        % SRC_ROOT
    ) + body


def _verify_files_frozen(expected=None):
    """打包版的验证：关键文件完整性检查。

    ⚠️⚠️ 2026-11-17（实测）：frozen 时 sys.executable 就是**应用 exe 本身**。
    `subprocess.run([exe, "-c", code])` 不会被当成「跑一段代码」，
    而是被 PyInstaller 当成**正常启动**再开一个应用实例 ——
    会抢 8000 端口、可能弹窗，而且返回码永远不是 0，验证必然假失败。
    所以打包版改用「文件在不在 / 是不是空的 / JSON 能不能解析」来判定，
    这正好覆盖回滚真正会改的那部分（frontend 静态资源 + config + data 状态）。
    """
    problems = []
    checked = 0
    fe_dir = os.path.join(SRC_ROOT, "frontend")
    has_fe = os.path.isdir(fe_dir)

    # 1) 有「期望大小」（回滚时由 zip 条目提供）→ 按实际/期望比对。
    #    ⚠️⚠️ 2026-11-17 实测踩到：原先硬编码 index.html≥500B、app.js≥5000B 这类
    #    绝对阈值，只对当前这个项目成立。换一个前端文件本来就小的项目，
    #    每次回滚都会被判「疑似被清空」→ 全部自动退回，机制直接失效。
    #    改成跟快照里的真实大小比，跟项目无关，也不会误伤小文件。
    if expected:
        for rel, exp in sorted(expected.items()):
            if "/" not in rel:
                continue
            try:
                ap = _dst_path(rel)
            except ValueError:
                continue
            if not os.path.isfile(ap):
                problems.append("缺文件：%s" % rel)
                continue
            checked += 1
            try:
                act = os.path.getsize(ap)
            except OSError:
                problems.append("无法读取：%s" % rel)
                continue
            # 允许 10% 浮动（换行/编码差异），但不允许被清空或截断
            if exp > 0 and act < exp * 0.9:
                problems.append("%s 只有 %d B（快照里是 %d B，疑似被清空或截断）"
                                % (rel, act, exp))

    # 1b) 核心前端文件：只查「存在」与「非 0 字节」，不设绝对大小下限
    must = ["frontend/index.html", "frontend/app.js"]
    for rel in must:
        try:
            ap = _dst_path(rel)
        except ValueError:
            continue
        if not os.path.isfile(ap):
            # 打包版若无 frontend 目录，说明这个包本来就没带前端 —— 不算回滚的错
            if has_fe:
                problems.append("缺文件：%s" % rel)
            continue
        checked += 1
        if os.path.getsize(ap) == 0:
            problems.append("%s 是空文件" % rel)

    # 2) 其余前端文件：不能是 0 字节
    if has_fe:
        for dp, dn, fn in os.walk(fe_dir):
            dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
            for f in fn:
                if _excluded_file(f):
                    continue
                ap = os.path.join(dp, f)
                checked += 1
                try:
                    if os.path.getsize(ap) == 0:
                        problems.append("%s 是空文件" % os.path.relpath(ap, SRC_ROOT).replace("\\", "/"))
                except OSError:
                    problems.append("无法读取：%s" % os.path.basename(ap))

    for rel in ["config.json"] + ["data/" + n for n in DATA_STATE_FILES if n.endswith(".json")]:
        ap = _dst_path(rel)
        if not os.path.isfile(ap):
            continue
        checked += 1
        try:
            with open(ap, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as exc:
            problems.append("%s 解析失败：%s" % (rel, exc))

    if problems:
        return False, "打包版文件完整性检查未通过：" + "；".join(problems[:6])
    return True, "打包版：文件完整性检查通过（%d 项）" % checked


def verify_import(timeout=180, expected=None):
    """真跑一次 `import backend.main`。比语法检查强：能抓住缺依赖 / 循环导入 / 名字错误。"""
    if PACKAGED:
        return _verify_files_frozen(expected)
    # 导入后必须断言：拿到的 backend.main 真的在 SRC_ROOT 下。
    # 否则「导入了别的项目」会被当成「本项目能启动」，健康校验就形同虚设。
    code = _clean_import_code(
        "import backend.main\n"
        "import os as _os\n"
        "_f = _os.path.abspath(getattr(backend.main, '__file__', '') or '')\n"
        "_r = _os.path.abspath(%r)\n"
        "if not (_f == _r or _f.startswith(_r + _os.sep)):\n"
        "    raise SystemExit('IMPORT_WRONG_PROJECT: ' + _f)\n"
        "print('IMPORT_OK')\n" % SRC_ROOT
    )
    try:
        p = subprocess.run([_python_exe(), "-c", code], cwd=ROOT,
                           capture_output=True, timeout=timeout, env=_child_env())
    except subprocess.TimeoutExpired:
        return False, "import backend.main 超时（%d 秒）" % timeout
    except Exception as exc:
        return False, "无法启动 python：%s" % exc
    out = (_decode(p.stdout) + _decode(p.stderr)).strip()
    if p.returncode == 0 and "IMPORT_OK" in out:
        return True, "import backend.main 成功"
    tail = out[-1200:] if out else "（没有任何输出，返回码 %s）" % p.returncode
    return False, tail


def _app_exe():
    """找到要验证的应用 exe。

    frozen 时 sys.executable 就是它；但「装了 Python 的机器上独立跑 rescue.py」
    时 sys.executable 是 python.exe —— 拿它去验证毫无意义，得在 ROOT 下找 exe。
    """
    if FROZEN:
        return sys.executable or ""
    try:
        cands = [os.path.join(ROOT, f) for f in os.listdir(ROOT)
                 if f.lower().endswith(".exe") and not f.lower().startswith("unins")]
    except Exception:
        return ""
    if len(cands) == 1:
        return cands[0]
    # 多个 exe 时挑最大的那个（应用本体一定比 updater 之类大得多）
    cands.sort(key=lambda p: os.path.getsize(p) if os.path.isfile(p) else 0, reverse=True)
    return cands[0] if cands else ""


# ------------------------------------------------------------ exe 版本池（打包版）
# ⚠️⚠️ 2026-11-17：打包版的 backend 被编译进 exe 的 PYZ 里，磁盘上根本没有
#   backend/*.py（实测 _internal/backend 只有 _feature_tests / _pysite），
#   所以「只回滚源码目录」在 exe 版上永远动不了后端。
#   办法：把 exe 本身也存一份 —— 换 exe 就等于换后端。
#   exe 放独立池子里按 sha256 去重，**不塞进 zip**：否则每份快照多 45MB，
#   还得过 zip bomb 闸门，而且同一版 exe 会被重复存 8 次。


def _exe_pool():
    return os.path.join(SNAP_DIR, "_exe_pool")


def _save_exe_to_pool(exe_path):
    """把当前应用 exe 复制进版本池（按 sha256 去重）。返回写进 manifest 的 dict。

    读正在运行的 exe 是允许的（Windows 只锁写不锁读），所以随时能存。
    任何失败都只记日志、返回 None —— 不能因为 exe 存不了就让代码快照失败。
    """
    try:
        if not exe_path or not os.path.isfile(exe_path):
            return None
        size = os.path.getsize(exe_path)
        if size <= 0 or size > EXE_POOL_MAX_MB * 1048576:
            _log("快照：exe 体积异常（%d B），不入版本池" % size)
            return None
        sha = _sha256_file(exe_path)
        if not sha:
            return None
        pool = _exe_pool()
        os.makedirs(pool, exist_ok=True)
        dst = os.path.join(pool, sha[:16] + ".exe")
        if not (os.path.isfile(dst) and os.path.getsize(dst) == size):
            tmp = dst + ".tmp"
            shutil.copy2(exe_path, tmp)
            os.replace(tmp, dst)
            _log("exe 入库：%s（%.1f MB）" % (os.path.basename(dst), size / 1048576.0))
        return {"name": os.path.basename(exe_path), "sha256": sha, "size": size,
                "pool": os.path.basename(dst),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(os.path.getmtime(exe_path)))}
    except Exception as exc:
        _log("快照：exe 入库失败（不影响代码快照）：%s: %s" % (type(exc).__name__, exc))
        return None


def _prune_exe_pool():
    """exe 池保留 KEEP_EXE 个，但**绝不删还被现存快照引用的那份**。

    只按数量删会出事：老快照还在列表里，用户点它回滚时 exe 已经没了 ——
    只能回滚代码，而那正是 exe 版最需要的那一半。所以引用优先，再按时间补足。
    """
    pool = _exe_pool()
    if not os.path.isdir(pool):
        return 0
    referenced = set()
    try:
        for s in list_snapshots():
            info = s.get("app_exe") or {}
            if isinstance(info, dict) and info.get("pool"):
                referenced.add(info["pool"])
    except Exception:
        pass
    files = []
    for n in os.listdir(pool):
        fp = os.path.join(pool, n)
        try:
            if n.lower().endswith(".exe") and os.path.isfile(fp):
                files.append((os.path.getmtime(fp), n, fp))
        except OSError:
            continue
    files.sort(reverse=True)
    keep = set(n for _m, n, _p in files[:KEEP_EXE]) | referenced
    removed = 0
    for _m, n, fp in files:
        if n in keep:
            continue
        try:
            os.remove(fp)
            removed += 1
            _log("清理旧 exe 版本：%s" % n)
        except OSError:
            pass
    return removed


def _swap_script_path(exe_path):
    return os.path.join(os.path.dirname(os.path.abspath(exe_path)), "_swap_exe.bat")


def _write_swap_script(exe_path):
    """生成「等应用退出 → 换 exe → 重启」的脚本。

    内容全 ASCII：中文注释在 cmd 的 GBK 窗口里容易变乱码，宁可不要。
    """
    exe = os.path.abspath(exe_path)
    name = os.path.basename(exe)
    body = [
        "@echo off",
        "rem Generated by rescue.py -- swap app exe after it exits.",
        "setlocal",
        'set "EXE=%~dp0' + name + '"',
        "set /a N=0",
        ":wait",
        'tasklist /FI "IMAGENAME eq ' + name + '" 2>nul | find /I "' + name + '" >nul',
        "if errorlevel 1 goto swap",
        "set /a N+=1",
        "if %N% GEQ 300 goto swap",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":swap",
        'if not exist "%EXE%.new" goto done',
        'if exist "%EXE%.old" del /f /q "%EXE%.old" >nul 2>nul',
        'move /y "%EXE%" "%EXE%.old" >nul 2>nul',
        'move /y "%EXE%.new" "%EXE%" >nul 2>nul',
        'if exist "%EXE%.old" del /f /q "%EXE%.old" >nul 2>nul',
        'start "" "%EXE%"',
        ":done",
        '(goto) 2>nul & del /f /q "%~f0"',
    ]
    p = _swap_script_path(exe)
    txt = "\n".join(body) + "\n"
    # ⚠️ 必须用 GBK 写：真实 exe 名是中文（本地多模态助手.exe），用 ascii 会直接
    #   抛 UnicodeEncodeError，整个 pending 分支就废了（沙箱里英文名测不出来）。
    #   cmd 默认代码页是 936，GBK 写进去中文文件名才对得上。
    try:
        with open(p, "w", encoding="gbk", newline="\r\n") as f:
            f.write(txt)
    except UnicodeEncodeError:
        # 名字里有 GBK 表示不了的字符：切到 UTF-8 并让脚本自己切代码页
        with open(p, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("chcp 65001 >nul\n" + txt)
    return p


def _restore_exe(info):
    """把快照记录的 exe 恢复到应用目录。

    三种结果：
      · same     —— 本来就是这一版，不用动；
      · replaced —— 应用没锁住它，已直接换好，重启即生效；
      · pending  —— 应用正在运行（Windows 不许覆盖运行中的 exe），已留 <exe>.new
                    并生成 _swap_exe.bat：关掉应用就自动换 + 重启。
    """
    if not isinstance(info, dict):
        return None
    pool_file = os.path.join(_exe_pool(), info.get("pool") or "")
    if not info.get("pool") or not os.path.isfile(pool_file):
        return {"ok": False, "reason": "missing",
                "detail": "快照记录的 exe 已不在版本池里（可能已被清理）：代码已回滚，"
                          "exe 需重新安装才能换回旧后端。"}
    cur = _app_exe()
    if not cur or not os.path.isfile(cur):
        return {"ok": False, "reason": "noexe",
                "detail": "找不到当前应用 exe，已跳过 exe 恢复（代码已回滚）。"}
    try:
        cur_sha = _sha256_file(cur)
    except Exception:
        cur_sha = ""
    if cur_sha and cur_sha == info.get("sha256"):
        return {"ok": True, "reason": "same", "detail": "exe 与快照一致，无需替换。"}

    # 1) 直接替换：应用没在跑（或不是从它启动）时一步到位
    tmp = cur + ".new"
    try:
        shutil.copy2(pool_file, tmp)
        os.replace(tmp, cur)
        _log("exe 已直接替换：%s" % os.path.basename(cur))
        return {"ok": True, "reason": "replaced",
                "detail": "exe 已换回快照版本，重启应用后生效。"}
    except Exception as exc:
        _log("exe 直接替换失败（多半是正在运行）：%s: %s" % (type(exc).__name__, exc))

    # 2) 被占用：留 .new + 替换脚本，关掉应用后自动完成
    try:
        if not (os.path.isfile(tmp) and os.path.getsize(tmp) == os.path.getsize(pool_file)):
            shutil.copy2(pool_file, tmp)
        bat = _write_swap_script(cur)
        return {"ok": True, "reason": "pending",
                "detail": "exe 已就绪（%s.new）：关掉应用后会自动替换并重启；"
                          "也可双击 %s 立刻完成。"
                          % (os.path.basename(cur), os.path.basename(bat)),
                "new_file": tmp, "script": bat}
    except Exception as exc:
        return {"ok": False, "reason": "fail",
                "detail": "exe 恢复失败：%s: %s（代码已回滚，exe 未换）"
                          % (type(exc).__name__, exc)}


def _verify_exe_boot(timeout=120):
    """打包版：真起一次应用 exe，探 8000 健康接口，再关掉。

    只在 8000 空闲时做 —— 否则会跟正在运行的主程序抢端口。
    数据目录指向临时目录，避免污染真实 data/。
    """
    if port_in_use(PORT):
        return True, "打包版：主程序正在运行（8000 已占用），跳过真启动验证"
    exe = _app_exe()
    if not exe or not os.path.isfile(exe):
        return False, "打包版：找不到应用 exe（%s），无法做真启动验证" % (exe or "空")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mm_verify_")
    env = _child_env()
    env["MM_NO_WINDOW"] = "1"
    env["MM_DATA_DIR"] = tmp
    env["MM_SKIP_ALREADY_CHECK"] = "1"
    proc = None
    try:
        proc = subprocess.Popen([exe], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, "打包版 exe 启动后立刻退出（返回码 %s）" % proc.returncode
            ok, _msg = http_health(PORT, timeout=2.0)
            if ok:
                return True, "打包版：exe 真启动验证通过（端口 %d）" % PORT
            time.sleep(2.0)
        return False, "打包版：等了 %d 秒健康接口仍不通" % timeout
    except Exception as exc:
        return False, "打包版真启动验证异常：%s: %s" % (type(exc).__name__, exc)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)


def verify_deep(timeout=120, allow_main_running=False):
    """真起服务 + 访问健康接口（最可靠，但慢、且会占内存）。

    默认只在 8000 没被占用时做 —— 主程序正在跑就别再起一个抢资源。
    allow_main_running=True 时不受这条限制：用临时端口 8011 另起一个，
    验证的是「磁盘上的代码」能否真启动，主程序照常跑不受影响。
    """
    if PACKAGED:
        return _verify_exe_boot(timeout=timeout)
    if port_in_use(PORT) and not allow_main_running:
        return False, "8000 端口已有服务在跑，跳过真启动验证（避免抢资源）"
    if port_in_use(VERIFY_PORT):
        return False, "%d 端口被占用，无法做真启动验证" % VERIFY_PORT
    code = _clean_import_code(
        "import uvicorn\n"
        "from backend.main import app\n"
        "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='critical')\n" % VERIFY_PORT
    )
    proc = None
    try:
        proc = subprocess.Popen([_python_exe(), "-c", code], cwd=ROOT,
                                env=_child_env(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                err = _decode(proc.stderr.read() if proc.stderr else b"")
                return False, "起服务时进程退出：%s" % (err[-900:] or "无输出")
            ok, msg = http_health(VERIFY_PORT, timeout=2.0)
            if ok:
                return True, "真起服务验证通过（端口 %d）" % VERIFY_PORT
            last = msg
            time.sleep(1.5)
        return False, "等了 %d 秒健康接口仍不通：%s" % (timeout, last)
    except Exception as exc:
        return False, "真启动验证异常：%s: %s" % (type(exc).__name__, exc)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def wait_health(seconds=40):
    deadline = time.time() + max(1, seconds)
    last = ""
    while time.time() < deadline:
        ok, msg = http_health(PORT)
        if ok:
            return True, msg
        last = msg
        time.sleep(1.0)
    return False, last or "健康接口一直不通"


# ---------------------------------------------------------------- 快照
def _state_read():
    d = {"fails": 0, "last_fail": "", "last_auto": None}
    try:
        if os.path.isfile(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                d.update(raw)
    except Exception:
        pass
    return d


def _state_write(d):
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        _log("状态写入失败：%s" % sys.exc_info()[1])


def create_snapshot(tag="", healthy=None, prefix="snap", keep_path=""):
    """建一个快照。healthy=None 时现场探测一次健康接口。

    为什么记 healthy：自动回滚**只**往「建的时候是好的」那些快照回滚。
    否则可能滚到一个本来就坏的版本，越滚越死。
    """
    # ⚠️ 2026-11-17 实测：回滚进行中建快照，抓到了 v2:397 + v1:3 的**混合状态**，
    #   还标成 healthy=True。那份快照拿去回滚会得到半新半旧的代码，而且验证能过。
    #   所以：别人在回滚时拒绝建快照；回滚自己内部建 bad_ 快照时（_IN_ROLLBACK）放行。
    _self_holds = (_LOCK_OWNER_TID is not None and
                   _LOCK_OWNER_TID == threading.get_ident())
    if not _self_holds and os.path.exists(LOCK_PATH):
        _stale = False
        try:
            _age = time.time() - os.path.getmtime(LOCK_PATH)
            _txt = ""
            try:
                with open(LOCK_PATH, encoding="utf-8", errors="ignore") as _f:
                    _txt = _f.read().strip()
            except Exception:
                pass
            _pid = int(_txt) if _txt.isdigit() else 0
            if (_pid and not _pid_alive(_pid)) or _age > 180:
                _stale = True
        except Exception:
            _stale = True
        if not _stale:
            return {"ok": False,
                    "error": "另一个回滚正在进行，此时建快照会抓到「回滚到一半」的状态，"
                             "已拒绝。请等它结束后再建。"}

    # ⚠️ 2026-11-17 实测：data 路径被同名文件占住时，os.makedirs 抛
    #   NotADirectoryError 且**没人捕获**，前端收到 500 而不是清晰提示。
    #   目录建不出来就没有写快照的可能，这里明确返回失败原因。
    try:
        os.makedirs(SNAP_DIR, exist_ok=True)
    except OSError as _e:
        return {"ok": False,
                "error": "快照目录无法创建（%s）：%s。请检查 data 目录是否存在同名文件或权限不足。"
                         % (SNAP_DIR, _e)}
    if healthy is None:
        healthy, _msg = http_health(PORT, timeout=1.5)

    files = collect_files()
    if not files:
        return {"ok": False, "error": "没有收集到任何文件（路径异常？）"}

    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_tag = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", tag or "")[:28].strip("_")
    suffix = ("_" + safe_tag) if safe_tag else ""
    # 同一秒内可能连建两个快照（实测：回滚完立刻再回滚）——同名会互相覆盖，
    # 把「回滚前状态」这份退路吃掉，导致想回到回滚前却回不去。
    # ⚠️⚠️ 2026-11-17 并发实测：原先用 `if os.path.exists(path)` 判重，
    #   8 线程同时建快照时有两个线程都判定「不存在」，抢到同一个文件名，
    #   两个 zipfile 同时往一个文件写 → 成品 zip 损坏（自检才抓出来，成功 6/8）。
    #   改用 O_CREAT|O_EXCL 原子占名：抢不到就换下一个候选，跨线程/跨进程都安全。
    name = ""
    path = ""
    for _try in range(200):
        if _try == 0:
            cand = "%s_%s%s.zip" % (prefix, ts, suffix)
        elif _try == 1:
            cand = "%s_%s_%03d%s.zip" % (prefix, ts, int((time.time() * 1000) % 1000), suffix)
        else:
            cand = "%s_%s_%03d_%d%s.zip" % (prefix, ts, int((time.time() * 1000) % 1000), _try, suffix)
        cand_path = os.path.join(SNAP_DIR, cand)
        try:
            _fd = os.open(cand_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(_fd)
            name, path = cand, cand_path
            break
        except FileExistsError:
            continue
        except OSError:
            break
    if not path:
        return {"ok": False, "error": "无法分配快照文件名（目录不可写？）"}

    # 记录每个文件的真实大小：回滚时拿它核对，能发现「快照里的文件被截断/清空」。
    # 没有这份记录时，回滚只能拿 zip 自己的大小当基准 —— 快照自己坏了就查不出来。
    _sizes = {}
    for _ap, _rel in files:
        try:
            _sizes[_rel] = os.path.getsize(_ap)
        except OSError:
            pass

    manifest = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": tag or "",
        "healthy": bool(healthy),
        "git_head": _git_head(),
        "count": len(files),
        "files": [rel for _ap, rel in files],
        "sizes": _sizes,
        "root": ROOT,
        "src_root": SRC_ROOT,
        "project_id": _project_id(),
        "packaged": bool(PACKAGED),
        "python": sys.version.split()[0],
    }
    # 打包版：exe 里编译着 backend，换 exe 才等于换后端 —— 把 exe 也存一份。
    # 必须在写 _manifest.json 之前做，否则清单里没有这条记录，回滚时不知道要换 exe。
    if PACKAGED:
        _exe_info = _save_exe_to_pool(_app_exe())
        if _exe_info:
            manifest["app_exe"] = _exe_info
    try:
        total = 0
        # 只把「真正写进 zip」的文件记进 manifest：个别文件被占用/读不了时跳过它，
        # 快照仍然可用，但清单必须如实反映 zip 里到底有什么（否则回滚核对会误报）。
        _written = {}
        # ⚠️ 2026-11-17（实测发现）：只记大小挡不住「内容被等长度替换」——
        #   有人把快照里的 app.js 换成同样 8000 字节的恶意内容、CRC 合法，
        #   回滚会静默接受。所以同时记 sha256，回滚后逐个核对。
        _hashes = {}
        _skipped = []
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for ap, rel in files:
                try:
                    z.write(ap, rel)
                    _sz = os.path.getsize(ap)
                    total += _sz
                    _written[rel] = _sz
                    _h = _sha256_file(ap)
                    if _h:
                        _hashes[rel] = _h
                except Exception:
                    _skipped.append(rel)
                    _log("快照跳过（读不了）：%s" % rel)
            manifest["files"] = sorted(_written.keys())
            manifest["sizes"] = _written
            manifest["hashes"] = _hashes
            manifest["count"] = len(_written)
            # ⚠️ 2026-11-17（实测）：文件被独占/读不了时原来只打一行日志就跳过，
            #   快照缺了文件却照样 ok=True + healthy=True，用户完全不知道这份不完整。
            #   实测：独占 frontend/style.css 后建快照 → 只有 4 个文件、healthy=True，
            #   回滚时它不会被恢复，等于悄悄少还原一个文件。
            #   现在如实记进 manifest；若缺的是**源码**（backend/ frontend/ launcher/），
            #   这份快照不能用来回滚代码，直接 healthy=False（自动回滚会跳过它）。
            _src_missing = [r for r in _skipped
                            if any(r == d or r.startswith(d + "/") for d in SRC_DIRS)]
            if _src_missing:
                manifest["healthy"] = False
                healthy = False
            manifest["skipped"] = _skipped
            z.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        manifest["bytes_raw"] = total
        manifest["bytes_zip"] = os.path.getsize(path)

        # ⚠️⚠️ 2026-11-17（实测）：建快照时如果源文件正在被写、或磁盘出错，
        #   zip 里可能留下截断的条目，而当时没有任何人会发现 —— 等到真出事要回滚，
        #   才发现「恢复出来的文件是空的」。所以建完立刻自检一遍：
        #   条目数对得上、每个条目的大小和源文件一致，否则删掉这份废快照并报错。
        try:
            with zipfile.ZipFile(path) as _z:
                _bad = []
                _names = set(_z.namelist())
                for _rel, _sz in _written.items():
                    if _rel not in _names:
                        _bad.append("%s 缺失" % _rel)
                    elif _z.getinfo(_rel).file_size != _sz:
                        _bad.append("%s 大小不符（%d≠%d）"
                                    % (_rel, _z.getinfo(_rel).file_size, _sz))
                if _bad:
                    os.remove(path)
                    _log("建快照失败（自检未通过，已删除）：%s" % "；".join(_bad[:3]))
                    return {"ok": False,
                            "error": "快照自检未通过，已删除：%s" % "；".join(_bad[:3])}
        except Exception as exc:
            try:
                os.remove(path)
            except Exception:
                pass
            _log("建快照失败（自检异常，已删除）：%s" % exc)
            return {"ok": False, "error": "快照自检异常，已删除：%s" % exc}
        if _skipped:
            _log("⚠️ 快照不完整：%d 个文件读不了被跳过 → %s%s"
                 % (len(_skipped), "、".join(_skipped[:3]),
                    "（含源码，已标为不健康）" if _src_missing else "（仅状态文件）"))
        _log("建快照 %s：%d 个文件，原始 %.1f MB → 压缩 %.1f MB，healthy=%s"
             % (name, manifest["count"], total / 1048576.0,
                manifest["bytes_zip"] / 1048576.0, healthy))
        prune_snapshots(keep_path=keep_path)
        _prune_exe_pool()
        _ret = {"ok": True, "path": path, "name": name, **{k: manifest[k] for k in
                ("count", "bytes_raw", "bytes_zip", "healthy", "created", "git_head")}}
        if _skipped:
            _ret["skipped"] = _skipped
            _ret["incomplete"] = True
        return _ret
    except Exception as exc:
        # O_EXCL 占名会先留一个 0 字节文件；写失败必须删掉，否则列表里多个坏条目
        try:
            if path and os.path.isfile(path) and os.path.getsize(path) == 0:
                os.remove(path)
        except Exception:
            pass
        _log("建快照失败：%s: %s" % (type(exc).__name__, exc))
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def list_snapshots():
    """列出快照，**新的在前**。"""
    out = []
    if not os.path.isdir(SNAP_DIR):
        return out
    for n in os.listdir(SNAP_DIR):
        if not n.lower().endswith(".zip"):
            continue
        p = os.path.join(SNAP_DIR, n)
        man = {}
        try:
            with zipfile.ZipFile(p) as z:
                if "_manifest.json" in z.namelist():
                    man = json.loads(z.read("_manifest.json").decode("utf-8"))
        except Exception:
            man = {}
        try:
            st = os.stat(p)
        except OSError:
            # 并发时可能刚好被 prune 删掉，或正被别的线程创建中 —— 跳过即可
            continue
        if st.st_size == 0:
            # O_EXCL 占名后还没写完的中间态，别当快照列出来
            continue
        out.append({
            "name": n,
            "path": p,
            "mtime": st.st_mtime,
            "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "size": st.st_size,
            "healthy": bool(man.get("healthy")),
            "tag": man.get("tag", ""),
            "created": man.get("created", ""),
            "count": man.get("count", 0),
            "git_head": man.get("git_head", ""),
            "bad": n.startswith("bad_"),
            "man": n.startswith("man_"),
            "iter": n.startswith("it_"),
            "app_exe": man.get("app_exe") if isinstance(man.get("app_exe"), dict) else None,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def prune_snapshots(keep_path=""):
    """三类分开计数：自动 KEEP_SNAPS 个 / 坏状态 KEEP_BAD 个 / 手动 KEEP_MAN 个。

    为什么手动要单独算：自动快照每次启动都会建一个，如果混在一个池子里，
    一天重启 9 次就把你亲手存的「改代码前」基线挤掉了 —— 那正是最需要留的那个。

    keep_path：本次必须保住的那份快照。回滚时用它 —— 实测回滚到最旧的 bad_ 时，
    新建的「回滚前状态」会触发清理把目标自己删掉，解压直接 FileNotFoundError。
    """
    snaps = list_snapshots()
    man = [s for s in snaps if s.get("man")]
    bad = [s for s in snaps if s["bad"]]
    itr = [s for s in snaps if s.get("iter")]
    normal = [s for s in snaps if not s["bad"] and not s.get("man") and not s.get("iter")]
    keep_abs = os.path.abspath(keep_path) if keep_path else ""
    for group, keep in ((normal, KEEP_SNAPS), (bad, KEEP_BAD),
                        (man, KEEP_MAN), (itr, KEEP_ITER)):
        if keep_abs:
            pinned = [s for s in group if os.path.abspath(s["path"]) == keep_abs]
            rest = [s for s in group if os.path.abspath(s["path"]) != keep_abs]
            drop = rest[max(0, keep - len(pinned)):]
        else:
            drop = group[keep:]
        for s in drop:
            try:
                os.remove(s["path"])
                _log("清理旧快照：%s" % s["name"])
            except Exception:
                pass
    # 顺带清掉「占名后进程被杀」留下的 0 字节占位（list_snapshots 看不到它们，
    # 不主动清就会永久累积）
    _clean_orphans()


def _clean_orphans(max_age=600):
    """清理「建快照时进程被杀」留下的 0 字节占位文件。

    为什么需要：create_snapshot 用 O_CREAT|O_EXCL 原子占名，占完名才开始写 zip。
    如果这中间进程被强杀（或磁盘写失败），会留下一个 0 字节的 .zip。
    而 list_snapshots 会主动跳过 0 字节文件（避免把「写了一半的中间态」列给用户），
    于是它永远进不了清理队列 —— 实测：造 20 个残留 + 建 12 个快照触发 4 轮清理，
    一个都没被删，永久累积。

    只删 mtime 超过 max_age 秒的：别的线程可能正占着名在写，别抢它的文件。
    """
    removed = 0
    if not os.path.isdir(SNAP_DIR):
        return removed
    now = time.time()
    for n in os.listdir(SNAP_DIR):
        if not n.lower().endswith(".zip"):
            continue
        p = os.path.join(SNAP_DIR, n)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) != 0:
                continue
            if now - os.path.getmtime(p) < max_age:
                continue
            os.remove(p)
            removed += 1
            _log("清理残留占位文件：%s" % n)
        except OSError:
            pass
    return removed


def _resolve_snap(name):
    """把用户给的快照名解析成路径。

    ⚠️ 2026-11-17 实测：原先用 `startswith` 第一个命中就返回。当两个快照共享前缀时
    （`--snap snap_` 会命中任意一个），会**静默回滚到别的版本**，而用户以为滚的是自己
    点的那份。现在：精确名优先；前缀匹配只在唯一命中时才用；多个命中直接返回 None，
    让上层明确报错，不猜。
    """
    if not name:
        return None
    if os.path.isfile(name):
        return name
    snaps = list_snapshots()
    for s in snaps:
        if s["name"] == name:
            return s["path"]
    hits = [s["path"] for s in snaps if s["name"].startswith(name)]
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------- 回滚
def rollback(snap_path=None, clean=True, deep=False, force=False, allow_mass_delete=False,
             allow_foreign=False):
    """回滚到某个快照（对外入口）。

    ⚠️ 2026-11-17 实测：两个线程同时回滚到不同快照，一个报「成功」而磁盘实际是另一个
    版本（被对方的自动退回盖掉）。所以这里先抢互斥锁，让回滚**串行执行**；
    同时检查上次回滚是否被强杀过（半回滚状态），如实提示。
    """
    if not force and http_health(PORT, timeout=1.0)[0]:
        return {"ok": False, "error": "主程序当前是**健康**的（8000 端口正常），"
                                     "不该回滚。若确定要回滚请加 --force。"}
    global _LOCK_OWNER_TID
    try:
        fd, interrupted = _acquire_lock()
    except RollbackBusy as exc:
        return {"ok": False, "error": str(exc)}
    _LOCK_OWNER_TID = threading.get_ident()
    try:
        res = _rollback_impl(snap_path, clean=clean, deep=deep,
                             force=force, allow_mass_delete=allow_mass_delete,
                             allow_foreign=allow_foreign)
    finally:
        _LOCK_OWNER_TID = None
        _release_lock(fd)
    if interrupted:
        warn = ("检测到上一次回滚没有正常结束（进程被中断/强杀），磁盘可能停在半回滚状态。"
                "本次已完整重新执行；如结果异常请再回滚一次。")
        res.setdefault("warnings", [])
        if isinstance(res.get("warnings"), list):
            res["warnings"].append(warn)
        res["interrupted_before"] = True
        _log("⚠️ " + warn)
    return res


def _rollback_impl(snap_path=None, clean=True, deep=False, force=False, allow_mass_delete=False,
                   allow_foreign=False):
    """回滚的实际实现（不加锁，由 rollback() 串行化后调用）。

    流程（顺序不能变）：
      1) 当前状态先存成 bad_ 快照 —— 回滚本身也可能选错版本，得留退路
      2) 解压覆盖
      3) clean：删掉源码目录里「该快照没有」的文件（否则旧文件残留会继续报错）
      4) 验证：import backend.main；deep=True 时再真起服务
      5) 验证失败 → 自动退回回滚前的 bad_ 快照，并如实报错
    """
    if not force and http_health(PORT, timeout=1.0)[0]:
        return {"ok": False, "error": "主程序当前是**健康**的（8000 端口正常），"
                                     "不该回滚。若确定要回滚请加 --force。"}

    path = _resolve_snap(snap_path)
    if not path:
        if snap_path:
            # ⚠️ 2026-11-18 实测：两个快照共享前缀时，_resolve_snap 返回 None（拒绝猜，正确），
            # 但下面那句「可能已被清理策略删除」是**误导**——快照明明还在列表里，用户会以为
            # 快照丢了、甚至去重建一份。所以先区分「歧义」和「真找不到」。
            _hits = [s["name"] for s in list_snapshots()
                     if s["name"].startswith(snap_path) and s["name"] != snap_path]
            if len(_hits) > 1:
                return {"ok": False, "error": "快照名「%s」有歧义：同时匹配到 %d 份"
                                             "（%s）。请给出更完整的名字，或在界面上点具体那一行"
                                             "（本次未改动任何文件）。"
                                             % (snap_path, len(_hits), "、".join(_hits[:3]))}
            # 用户明确点了某个版本却解析不到（被清理策略删了 / 名字传错）——
            # 绝不能静默换成别的版本，否则提示「回滚成功」而实际回滚的是另一份。
            return {"ok": False, "error": "找不到快照「%s」：可能已被清理策略删除。"
                                         "请刷新列表后重试（本次未改动任何文件）。" % snap_path}
        snaps = [s for s in list_snapshots() if s["healthy"] and not s["bad"]]
        if not snaps:
            return {"ok": False, "error": "没有任何可用快照。请先跑一次 --snapshot。"}
        path = snaps[0]["path"]

    # 读清单
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            man = {}
            if "_manifest.json" in names:
                man = json.loads(z.read("_manifest.json").decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "快照损坏，读不了：%s: %s" % (type(exc).__name__, exc)}

    # ⚠️⚠️ 2026-11-17 实测：把一个快照 zip 里的 _manifest.json 删掉，回滚仍判「成功」——
    #   因为下面第 990 行的完整性核对会退回用「zip 条目自身大小」比对，
    #   等于自己跟自己比，永远通过。等于删掉 manifest 就能绕过整条完整性保护。
    #   建快照时 manifest 是必写的（create_snapshot 里 z.writestr("_manifest.json", ...)），
    #   所以缺失只可能是被改动或损坏 —— 这里 fail-closed，直接拒绝。
    #   ⚠️ 但老快照（sizes 字段引入之前建的）manifest 里**没有** sizes，那是正常的，
    #   不能因此拒绝，否则现有全部历史快照都会失效。所以这里只要求 manifest 存在。
    snap_files = set(n for n in names if n != "_manifest.json")

    # ⚠️⚠️ 2026-11-17 实测踩到：回滚到一个**空 zip**（0 个文件）时，
    #   原实现返回 ok=True、restored=0，却把 clean 逻辑跑了一遍——
    #   于是 backend/ frontend/ 里的源码被**全部删掉**，界面还提示「回滚成功」。
    #   快照被截断、被拷成 0 字节、或从别的项目误拷过来，都会触发。
    #   这里做两道闸：① 空快照直接拒绝；② 删除量异常（远大于恢复量）也拒绝。
    if not snap_files:
        return {"ok": False, "error": "这个快照是空的（0 个文件），已拒绝回滚——"
                                     "继续下去会把 backend/ frontend/ 里的源码全部删掉。"
                                     "请换一个正常快照。"}

    # ⚠️⚠️ 2026-11-17 实测：把快照 zip 里的 _manifest.json 删掉，回滚仍判「成功」——
    #   因为下面的完整性核对会退回用「zip 条目自身大小」比对，等于自己跟自己比，永远通过。
    #   删掉 manifest 就等于绕过整条完整性保护。
    #   建快照时 manifest 是必写的，缺失只可能是被改动或损坏 —— fail-closed 直接拒绝。
    #   ⚠️ 但老快照（sizes 字段引入之前建的）manifest 里没有 sizes，那是正常的，
    #   不能因此拒绝，否则现有全部历史快照都会失效。所以这里只要求 manifest 存在。
    if "_manifest.json" not in names:
        return {"ok": False, "error": "快照缺少 _manifest.json（完整性清单），"
                                     "疑似被改动或损坏，已拒绝回滚。请换一个正常快照。"}

    # ⚠️ 2026-11-17 实测：把 A 项目的快照传给 B 项目回滚，A 的文件被写进了 B，
    #   还返回 ok=True —— 因为 manifest 里记了 root，但回滚从不比对。
    #   为什么不用 root 比对：项目一移动/改名，路径就变了，会把「自己搬了个家」
    #   误判成外来快照，导致移动项目后全部历史快照作废。
    #   改用 project_id（存在 data/project_id，跟着项目目录走）：
    #     · 项目移动 → id 跟着 data/ 一起走 → 不变 → 放行；
    #     · 别人的快照 → id 不同 → 拒绝。
    #   老快照没有 project_id 字段，按「无法判定」放行，不能让历史快照失效。
    _snap_pid = str(man.get("project_id") or "")
    _cur_pid = _project_id()
    if _snap_pid and _cur_pid and _snap_pid != _cur_pid and not allow_foreign:
        return {"ok": False,
                "error": "这个快照来自另一个项目（项目标识 %s… ≠ 当前 %s…），已拒绝回滚 —— "
                         "否则会用别的项目的代码覆盖本项目。确认要强制恢复请加 --allow-foreign。"
                         % (_snap_pid[:8], _cur_pid[:8])}

    if clean and not allow_mass_delete:
        _cur = 0
        for _d in SRC_DIRS:
            _b = os.path.join(SRC_ROOT, _d)
            if os.path.isdir(_b):
                for _dp, _dn, _fn in os.walk(_b):
                    _dn[:] = [x for x in _dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
                    _cur += len([f for f in _fn if not _excluded_file(f)])
        _will_del = _cur - len([n for n in snap_files if "/" in n and not n.endswith("/")])
        if _will_del > 20 and _will_del > _cur * 0.5:
            return {"ok": False, "error": "拒绝回滚：这个快照只有 %d 个源码文件，"
                                         "而当前有 %d 个 —— 回滚会删掉 %d 个文件（超过一半）。"
                                         "疑似快照来自别的项目或被截断。确要执行请加 --allow-mass-delete。"
                                         % (len([n for n in snap_files if "/" in n]), _cur, _will_del)}

    # 1) 回滚前的坏状态留档
    bad = create_snapshot(tag="回滚前状态", healthy=False, prefix="bad", keep_path=path)
    bad_path = bad.get("path") if bad.get("ok") else ""

    # 2) 解压覆盖
    restored, failed, tampered = 0, [], []
    _total = 0
    try:
        with zipfile.ZipFile(path) as z:
            for n in snap_files:
                if n.endswith("/"):      # 目录条目，不是文件
                    continue
                try:
                    dst = _dst_path(n)
                except ValueError as exc:
                    # 非法路径（Zip Slip）：拒绝写盘。这说明快照被篡改过，整次回滚判失败。
                    failed.append(str(exc))
                    tampered.append(str(exc))
                    continue
                # zip bomb 闸门：先看声明大小，超限直接拒，一个字节都不写
                _big, _sz = _entry_too_big(z, n, _total)
                if _big:
                    failed.append(_big)
                    tampered.append(_big)
                    continue
                _total += _sz
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _pok, _pmsg = _prepare_dst(dst)
                    if not _pok:
                        raise OSError("腾不出写入位置：%s" % _pmsg)
                    with z.open(n) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    restored += 1
                except Exception as exc:
                    failed.append("%s（%s）" % (n, exc))
    except Exception as exc:
        return {"ok": False, "error": "解压失败：%s: %s" % (type(exc).__name__, exc),
                "bad_snapshot": bad_path}

    # 3) 删掉「快照里没有」的源码文件（只动 SRC_DIRS，绝不动 data/ 和根目录）
    removed = []
    if clean:
        for d in SRC_DIRS:
            base = os.path.join(SRC_ROOT, d)
            if not os.path.isdir(base):
                continue
            for dp, dn, fn in os.walk(base):
                dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
                for f in fn:
                    ap = os.path.join(dp, f)
                    rel = os.path.relpath(ap, SRC_ROOT).replace("\\", "/")
                    if rel in snap_files or _excluded_file(f):
                        continue
                    # ⚠️ 2026-11-17 实测：残留文件是只读属性时 os.remove 抛 PermissionError，
                    #   原来直接 except: pass —— 文件没删掉、回滚却报「成功」，
                    #   项目里还留着那个多余的坏文件（可能是 .py，会影响启动）。
                    #   现在：先加可写位再删；仍失败就计入 failed，让整次回滚如实判失败。
                    try:
                        try:
                            os.chmod(ap, 0o666)
                        except Exception:
                            pass
                        os.remove(ap)
                        removed.append(rel)
                    except Exception as exc:
                        failed.append("删除残留 %s 失败（%s）" % (rel, exc))

    # 4) 验证
    #    打包版没有独立 python，只能靠文件完整性判定；把快照里的真实大小传进去，
    #    才能发现「解压出来是空的 / 被截断」这类问题（不依赖硬编码阈值）。
    # 期望大小：优先用 manifest 里记录的「建快照时的真实大小」——
    # 有人把 zip 里的文件截断了，manifest 不会跟着变，这样才查得出来。
    # 老快照没有 sizes 字段时，退回用 zip 条目自身的大小。
    _expected = {}
    _msz = man.get("sizes") if isinstance(man, dict) else None
    if isinstance(_msz, dict) and _msz:
        _expected = {k: int(v) for k, v in _msz.items() if isinstance(v, int)}
    if not _expected:
        try:
            with zipfile.ZipFile(path) as _z:
                for _n in snap_files:
                    if not _n.endswith("/"):
                        _expected[_n] = _z.getinfo(_n).file_size
        except Exception:
            _expected = {}

    # 4a) 两种形态都做一次「恢复出来的文件大小对不对」的核对
    _size_bad = []
    for _rel, _exp in _expected.items():
        if _exp <= 0:
            continue
        try:
            _ap = _dst_path(_rel)
        except ValueError:
            continue
        if not os.path.isfile(_ap):
            continue
        try:
            _act = os.path.getsize(_ap)
        except OSError:
            continue
        if _act < _exp * 0.9:
            _size_bad.append("%s 只有 %d B（快照里是 %d B）" % (_rel, _act, _exp))

    # 4b) 内容指纹核对：大小对得上、CRC 也合法，但内容被换掉了 —— 只有 sha256 能发现。
    #     老快照（没有 hashes 字段）跳过这一步，只做大小核对，不误判成失败。
    _hash_bad = []
    _hman = man.get("hashes") if isinstance(man, dict) else None
    if isinstance(_hman, dict) and _hman:
        for _rel, _hexp in _hman.items():
            if not isinstance(_hexp, str) or len(_hexp) != 64:
                continue
            try:
                _ap = _dst_path(_rel)
            except ValueError:
                continue
            if not os.path.isfile(_ap):
                continue
            _hact = _sha256_file(_ap)
            if _hact and _hact != _hexp:
                _hash_bad.append(_rel)

    ok_i, msg_i = verify_import(expected=_expected or None)
    if tampered:
        # 快照里有越界条目 —— 这份快照不可信，即使剩下的文件能启动也不能算成功
        ok_i = False
        msg_i = ("快照含 %d 个非法路径条目（疑似被篡改），已全部拒绝写入：%s"
                 % (len(tampered), "；".join(tampered[:3])))
    elif failed:
        # ⚠️ 2026-11-17 实测：某个文件写不进去（被别的进程独占 / 权限不足）时，
        #   原来只把它塞进 failed 列表展示，**不参与 ok 判定**。
        #   被占用的是前端文件时，后端 import 照样成功 → 回滚报「成功」而前端还是旧的。
        #   这里补上：有文件没写成就不能算回滚成功，让它走自动退回。
        ok_i = False
        msg_i = ("有 %d 个文件没能写入（被占用或无权限），回滚不完整：%s"
                 % (len(failed), "；".join(failed[:3])))
    elif _size_bad:
        # 恢复出来的文件比快照里记录的小很多 —— 快照被截断，或解压没写全。
        # 这种「看着成功、实际残缺」的状态必须判失败，让它自动退回。
        ok_i = False
        msg_i = ("回滚后文件不完整（疑似快照被截断），共 %d 个：%s"
                 % (len(_size_bad), "；".join(_size_bad[:3])))
    elif _hash_bad:
        # 大小一样、CRC 也过，但内容指纹对不上 —— 快照被等长度替换过，不可信。
        ok_i = False
        msg_i = ("回滚后文件内容与快照不一致（疑似快照被篡改），共 %d 个：%s"
                 % (len(_hash_bad), "；".join(_hash_bad[:3])))
    result = {
        "ok": bool(ok_i),
        "snapshot": os.path.basename(path),
        "snapshot_healthy": bool(man.get("healthy")),
        "restored": restored,
        "removed": len(removed),
        "removed_list": removed[:40],
        "failed": failed[:20],
        "hash_mismatch": _hash_bad[:20],
        "bad_snapshot": bad_path,
        "verify": "import",
        "detail": msg_i,
    }
    if ok_i and deep:
        ok_d, msg_d = verify_deep()
        result["deep_ok"] = bool(ok_d)
        result["deep_detail"] = msg_d
        # deep 失败不一定要撤销：可能是端口/资源问题。但必须如实报出来。
        result["ok"] = bool(ok_d)
        result["detail"] = msg_d if not ok_d else (msg_i + "；" + msg_d)

    # 6) 打包版：快照里带了 exe 就一并恢复（backend 编译在 exe 里，换 exe 才等于换后端）。
    #    放在所有校验之后：代码没通过就一个字节都不动 exe，免得把能跑的那半也弄坏。
    if result["ok"] and isinstance(man.get("app_exe"), dict):
        try:
            result["exe"] = _restore_exe(man["app_exe"])
        except Exception as exc:
            result["exe"] = {"ok": False, "reason": "error",
                             "detail": "exe 恢复异常：%s: %s" % (type(exc).__name__, exc)}
        _ex = result["exe"] or {}
        if _ex and not _ex.get("ok"):
            result["partial"] = True
        _log("回滚 exe：%s（%s）" % (_ex.get("reason", "无"), _ex.get("detail", "")))

    if not result["ok"]:
        # 5) 回滚后还是坏的 → 退回回滚前状态，别把人留在更坏的地方
        undone = False
        if bad_path and os.path.isfile(bad_path):
            try:
                with zipfile.ZipFile(bad_path) as z:
                    for n in z.namelist():
                        if n == "_manifest.json" or n.endswith("/"):
                            continue
                        try:
                            dst = _dst_path(n)
                        except ValueError:
                            continue    # 自己建的快照不该有越界条目；有就跳过，别让退回中断
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        # ⚠️ 退回是最后一道保险，绝不能因为一个只读文件就失败
                        _prepare_dst(dst)
                        with z.open(n) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)
                undone = True
            except Exception:
                undone = False
        result["auto_undone"] = undone
        result["error"] = ("回滚后仍无法启动（%s）。%s"
                           % (result["detail"],
                              "已退回回滚前状态。" if undone else
                              "⚠️ 退回失败，请用 回滚救急.bat 选更早的快照。"))
        _log("回滚失败 %s：%s" % (os.path.basename(path), result["error"]))
        return result

    _log("回滚成功 → %s（恢复 %d 个文件，删除 %d 个残留）"
         % (os.path.basename(path), restored, len(removed)))
    result["msg"] = ("已回滚到 %s（%s）。恢复 %d 个文件，清掉 %d 个残留文件。验证：%s"
                     % (os.path.basename(path), man.get("created", ""),
                        restored, len(removed), result["detail"]))
    # exe 的结果接在最后：代码回滚成功但 exe 没换（或被占用待替换）必须让用户看见
    _exr = result.get("exe") or {}
    if _exr.get("detail") and _exr.get("reason") != "same":
        result["msg"] = (result["msg"] + " " +
                         ("⚠️ " if not _exr.get("ok") else "") + _exr["detail"]).strip()
    return result


def auto_rollback(deep=False, reason=""):
    """自动回滚：按时间从新到旧，找第一个「健康」快照，逐个试到能启动为止。"""
    snaps = [s for s in list_snapshots() if s["healthy"] and not s["bad"]]
    if not snaps:
        return {"ok": False, "error": "没有「健康」快照可用（快照都是在坏状态下建的）。"
                                      "请用 回滚救急.bat 手动选一个更早的版本。"}
    tried = []
    for s in snaps:
        _log("自动回滚尝试：%s（%s）reason=%s" % (s["name"], s["created"], reason))
        r = rollback(s["path"], clean=True, deep=deep, force=True)
        tried.append({"snap": s["name"], "ok": bool(r.get("ok")), "detail": r.get("detail", "")[:300]})
        if r.get("ok"):
            st = _state_read()
            st["last_auto"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "snap": s["name"], "reason": reason, "ok": True}
            st["fails"] = 0
            _state_write(st)
            r["tried"] = tried
            return r
    st = _state_read()
    st["last_auto"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "reason": reason, "ok": False, "tried": tried}
    _state_write(st)
    return {"ok": False, "error": "试过 %d 个健康快照，都起不来。" % len(snaps),
            "tried": tried}


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description="崩溃救急与全栈快照")
    ap.add_argument("--snapshot", action="store_true", help="创建快照")
    ap.add_argument("--tag", default="", help="快照说明")
    ap.add_argument("--list", action="store_true", help="列出快照")
    ap.add_argument("--precheck", action="store_true", help="检查 import backend.main")
    ap.add_argument("--health", action="store_true", help="探测健康接口")
    ap.add_argument("--wait-health", type=int, default=0, metavar="秒")
    ap.add_argument("--auto", action="store_true", help="自动回滚到最近健康快照")
    ap.add_argument("--rollback", action="store_true", help="回滚")
    ap.add_argument("--snap", default="", help="指定快照名（可只给前缀）")
    ap.add_argument("--deep", action="store_true", help="回滚后真起服务验证")
    ap.add_argument("--deep-check", action="store_true",
                    help="独立真起服务验证当前代码（临时端口 %d，不影响 %d）" % (VERIFY_PORT, PORT))
    ap.add_argument("--force", action="store_true", help="主程序健康时也强制回滚")
    ap.add_argument("--allow-mass-delete", action="store_true",
                    help="允许「回滚会删掉超过一半源码」的极端情况（默认拒绝，防清空项目）")
    ap.add_argument("--no-clean", action="store_true", help="不删除快照里没有的源码文件")
    ap.add_argument("--reason", default="", help="触发原因（写进日志）")
    ap.add_argument("--clean-snapshots", action="store_true", help="按保留策略清理")
    ap.add_argument("--state", action="store_true", help="打印救急状态")
    args = ap.parse_args(argv)

    did = False

    if args.snapshot:
        did = True
        r = create_snapshot(args.tag or "手动快照")
        if r.get("ok"):
            _say("快照完成：%s（%d 个文件，%.1f MB，healthy=%s）"
                 % (r["name"], r["count"], r["bytes_zip"] / 1048576.0, r["healthy"]))
            return 0
        _say("快照失败：%s" % r.get("error"))
        return 1

    if args.list:
        did = True
        snaps = list_snapshots()
        if not snaps:
            _say("还没有任何快照。跑一次 `py -3 rescue.py --snapshot` 就有了。")
            return 0
        _say("共 %d 个快照（新的在前）：" % len(snaps))
        for s in snaps:
            # 文案与前端统一：bad_ 是「回滚前自动存的退路」，不是「坏状态」。
            # 之前显示「未知/坏」让人以为代码坏了，其实内容就是当时能跑的版本。
            if s["bad"]:
                state = "回滚前备份"
            elif s["healthy"]:
                state = "健康"
            else:
                state = "未验证"
            _say("  %-46s %7.1f MB  %s  %s"
                 % (s["name"], s["size"] / 1048576.0, state,
                    s["created"] or s["mtime_str"]))
        return 0

    if args.state:
        did = True
        _say(json.dumps(_state_read(), ensure_ascii=False, indent=2))
        return 0

    if args.precheck:
        did = True
        ok, msg = verify_import()
        if ok:
            _say("预检通过：" + msg)
        else:
            lines = [l for l in str(msg).strip().splitlines() if l.strip()]
            tail = "\n".join(lines[-3:]) if lines else str(msg)
            _say("预检失败：\n" + tail)
        return 0 if ok else 1

    if args.health:
        did = True
        ok, msg = http_health()
        _say(("健康：" if ok else "不健康：") + msg)
        return 0 if ok else 1

    if args.deep_check:
        did = True
        _say("正在用临时端口 %d 真起一次服务（主程序不受影响，最多等 120 秒）..." % VERIFY_PORT)
        ok, msg = verify_deep(allow_main_running=True)
        _say(("真启动验证通过：" if ok else "真启动验证失败：") + msg)
        return 0 if ok else 1

    if args.wait_health:
        did = True
        ok, msg = wait_health(args.wait_health)
        _say(("服务已就绪：" if ok else "等不到服务：") + msg)
        return 0 if ok else 1

    if args.auto:
        did = True
        r = auto_rollback(deep=args.deep, reason=args.reason or "自动救急")
        if r.get("ok"):
            _say("自动回滚成功：%s" % r.get("msg", r.get("snapshot")))
            return 0
        _say("自动回滚失败：%s" % r.get("error"))
        for t in r.get("tried", []):
            _say("  试过 %s → %s" % (t["snap"], "成功" if t["ok"] else t["detail"]))
        return 1

    if args.rollback:
        did = True
        r = rollback(args.snap or None, clean=not args.no_clean,
                     deep=args.deep, force=args.force,
                     allow_mass_delete=args.allow_mass_delete)
        if r.get("ok"):
            _say("回滚成功：%s" % r.get("msg"))
            return 0
        _say("回滚失败：%s" % r.get("error"))
        return 1

    if args.clean_snapshots:
        did = True
        prune_snapshots()
        _say("已按保留策略清理（保留 %d 个自动 + %d 个坏状态 + %d 个手动）"
             % (KEEP_SNAPS, KEEP_BAD, KEEP_MAN))
        return 0

    if not did:
        ap.print_help()
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                       # noqa: BLE001
        _log("救急脚本自身异常：%s: %s" % (type(exc).__name__, exc))
        import traceback
        _log(traceback.format_exc()[-1500:])
        sys.exit(1)
