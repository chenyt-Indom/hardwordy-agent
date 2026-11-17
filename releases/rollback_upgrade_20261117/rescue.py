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
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SNAP_DIR = os.path.join(DATA, "rescue_snapshots")
STATE_PATH = os.path.join(DATA, "rescue_state.json")
LOG_PATH = os.path.join(DATA, "rescue.log")

PORT = 8000
VERIFY_PORT = 8011          # 验证用的临时端口，避开主程序的 8000
KEEP_SNAPS = 8              # 保留最近几个自动快照
KEEP_BAD = 2                # 「回滚前坏状态」额外保留几个
KEEP_MAN = 5                # 手动快照（man_）单独计数，不被自动快照挤掉

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

    # 1) 源码目录
    for d in SRC_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
            for f in fn:
                if _excluded_file(f):
                    continue
                ap = os.path.join(dp, f)
                add(ap, os.path.relpath(ap, ROOT))

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
    return (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "for _m in [k for k in list(sys.modules) if k == 'backend' or k.startswith('backend.')]:\n"
        "    del sys.modules[_m]\n"
        % ROOT
    ) + body


def verify_import(timeout=180):
    """真跑一次 `import backend.main`。比语法检查强：能抓住缺依赖 / 循环导入 / 名字错误。"""
    code = _clean_import_code("import backend.main\nprint('IMPORT_OK')\n")
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


def verify_deep(timeout=120, allow_main_running=False):
    """真起服务 + 访问健康接口（最可靠，但慢、且会占内存）。

    默认只在 8000 没被占用时做 —— 主程序正在跑就别再起一个抢资源。
    allow_main_running=True 时不受这条限制：用临时端口 8011 另起一个，
    验证的是「磁盘上的代码」能否真启动，主程序照常跑不受影响。
    """
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
    os.makedirs(SNAP_DIR, exist_ok=True)
    if healthy is None:
        healthy, _msg = http_health(PORT, timeout=1.5)

    files = collect_files()
    if not files:
        return {"ok": False, "error": "没有收集到任何文件（路径异常？）"}

    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_tag = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", tag or "")[:28].strip("_")
    suffix = ("_" + safe_tag) if safe_tag else ""
    name = "%s_%s%s.zip" % (prefix, ts, suffix)
    path = os.path.join(SNAP_DIR, name)
    # 同一秒内可能连建两个快照（实测：回滚完立刻再回滚）——同名会互相覆盖，
    # 把「回滚前状态」这份退路吃掉，导致想回到回滚前却回不去。冲突就补毫秒+序号。
    if os.path.exists(path):
        ms = "%03d" % int((time.time() * 1000) % 1000)
        name = "%s_%s_%s%s.zip" % (prefix, ts, ms, suffix)
        path = os.path.join(SNAP_DIR, name)
        _n = 2
        while os.path.exists(path):
            name = "%s_%s_%s_%d%s.zip" % (prefix, ts, ms, _n, suffix)
            path = os.path.join(SNAP_DIR, name)
            _n += 1

    manifest = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": tag or "",
        "healthy": bool(healthy),
        "git_head": _git_head(),
        "count": len(files),
        "files": [rel for _ap, rel in files],
        "root": ROOT,
        "python": sys.version.split()[0],
    }
    try:
        total = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for ap, rel in files:
                try:
                    z.write(ap, rel)
                    total += os.path.getsize(ap)
                except Exception:
                    _log("快照跳过（读不了）：%s" % rel)
            z.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        manifest["bytes_raw"] = total
        manifest["bytes_zip"] = os.path.getsize(path)
        _log("建快照 %s：%d 个文件，原始 %.1f MB → 压缩 %.1f MB，healthy=%s"
             % (name, len(files), total / 1048576.0,
                manifest["bytes_zip"] / 1048576.0, healthy))
        prune_snapshots(keep_path=keep_path)
        return {"ok": True, "path": path, "name": name, **{k: manifest[k] for k in
                ("count", "bytes_raw", "bytes_zip", "healthy", "created", "git_head")}}
    except Exception as exc:
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
        st = os.stat(p)
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
    normal = [s for s in snaps if not s["bad"] and not s.get("man")]
    keep_abs = os.path.abspath(keep_path) if keep_path else ""
    for group, keep in ((normal, KEEP_SNAPS), (bad, KEEP_BAD), (man, KEEP_MAN)):
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


def _resolve_snap(name):
    if not name:
        return None
    if os.path.isfile(name):
        return name
    for s in list_snapshots():
        if s["name"] == name or s["name"].startswith(name):
            return s["path"]
    return None


# ---------------------------------------------------------------- 回滚
def rollback(snap_path=None, clean=True, deep=False, force=False):
    """回滚到某个快照。

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

    snap_files = set(n for n in names if n != "_manifest.json")

    # 1) 回滚前的坏状态留档
    bad = create_snapshot(tag="回滚前状态", healthy=False, prefix="bad", keep_path=path)
    bad_path = bad.get("path") if bad.get("ok") else ""

    # 2) 解压覆盖
    restored, failed = 0, []
    try:
        with zipfile.ZipFile(path) as z:
            for n in snap_files:
                dst = os.path.join(ROOT, n.replace("/", os.sep))
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
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
            base = os.path.join(ROOT, d)
            if not os.path.isdir(base):
                continue
            for dp, dn, fn in os.walk(base):
                dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES and not x.startswith(".")]
                for f in fn:
                    ap = os.path.join(dp, f)
                    rel = os.path.relpath(ap, ROOT).replace("\\", "/")
                    if rel in snap_files or _excluded_file(f):
                        continue
                    try:
                        os.remove(ap)
                        removed.append(rel)
                    except Exception:
                        pass

    # 4) 验证
    ok_i, msg_i = verify_import()
    result = {
        "ok": bool(ok_i),
        "snapshot": os.path.basename(path),
        "snapshot_healthy": bool(man.get("healthy")),
        "restored": restored,
        "removed": len(removed),
        "removed_list": removed[:40],
        "failed": failed[:20],
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

    if not result["ok"]:
        # 5) 回滚后还是坏的 → 退回回滚前状态，别把人留在更坏的地方
        undone = False
        if bad_path and os.path.isfile(bad_path):
            try:
                with zipfile.ZipFile(bad_path) as z:
                    for n in z.namelist():
                        if n == "_manifest.json":
                            continue
                        dst = os.path.join(ROOT, n.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
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
            _say("  %-46s %7.1f MB  %s  %s"
                 % (s["name"], s["size"] / 1048576.0,
                    "健康" if s["healthy"] else "未知/坏",
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
                     deep=args.deep, force=args.force)
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
