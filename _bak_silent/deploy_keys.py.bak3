"""部署凭据管理 + 一键部署（2026-11-14）。

管两样东西，都在本机、都不外传：

    data/deploy/server.json   服务器连接信息（host / port / user / relay_port）
    data/deploy/id_rsa        SSH 私钥（权限 600，内容只在「点眼睛」时才回前端）
    data/hive/relay.json      中继密钥（由 hive_client 管，这里只做读写透传）

设计原则
--------
· 私钥默认打码，只有显式 reveal=1 才返回全文 —— 防止截图 / 投屏泄露。
· 私钥校验用 paramiko 真解析一遍，格式不对当场拒绝，别等部署时才炸。
· 一键部署在后台线程跑 deploy/hive_relay_deploy.py，输出进环形缓冲，
  前端按行号增量拉取，不用长连接。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_DIR = os.path.join(ROOT, "data", "deploy")
SERVER_CFG = os.path.join(DEPLOY_DIR, "server.json")
SSH_KEY = os.path.join(DEPLOY_DIR, "id_rsa")
RELAY_CFG = os.path.join(ROOT, "data", "hive", "relay.json")
DEPLOY_SCRIPT = os.path.join(ROOT, "deploy", "hive_relay_deploy.py")

DEFAULT_RELAY_PORT = 18765


# ------------------------------------------------------------------ 工具

def _ensure_dir() -> None:
    os.makedirs(DEPLOY_DIR, exist_ok=True)


def _harden(path: str) -> None:
    """尽力收紧权限。Windows 上 chmod 只能改只读位，失败就算了。"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _mask(secret: str, head: int = 6, tail: int = 4) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= head + tail + 3:
        return s[:2] + "*" * max(1, len(s) - 3) + s[-1:]
    return s[:head] + "*" * 6 + s[-tail:]


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    _harden(path)


# ------------------------------------------------------------------ 服务器信息

def server_config() -> dict:
    """服务器连接信息（不含任何密钥内容）。"""
    d = _read_json(SERVER_CFG)
    return {
        "host": str(d.get("host") or ""),
        "port": int(d.get("port") or 22),
        "user": str(d.get("user") or "root"),
        "relay_port": int(d.get("relay_port") or DEFAULT_RELAY_PORT),
        "configured": bool(d.get("host")),
    }


def save_server_config(host: str, port=22, user: str = "root",
                       relay_port=DEFAULT_RELAY_PORT) -> dict:
    host = (host or "").strip()
    if not host:
        raise ValueError("服务器地址不能为空")
    # 去掉用户可能一起粘进来的 ssh:// 前缀和端口
    for pre in ("ssh://", "http://", "https://"):
        if host.startswith(pre):
            host = host[len(pre):]
    if "@" in host:
        user, host = host.split("@", 1)
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host, p2 = host.rsplit(":", 1)
        port = int(p2)
    host = host.strip().strip("/")
    if not host:
        raise ValueError("服务器地址不合法")

    d = _read_json(SERVER_CFG)
    d.update({
        "host": host,
        "port": int(port or 22),
        "user": (user or "root").strip() or "root",
        "key": "data/deploy/id_rsa",
        "relay_port": int(relay_port or DEFAULT_RELAY_PORT),
    })
    _write_json(SERVER_CFG, d)
    return server_config()


def clear_server_config() -> dict:
    try:
        if os.path.exists(SERVER_CFG):
            os.remove(SERVER_CFG)
    except Exception:
        pass
    return server_config()


# ------------------------------------------------------------------ SSH 私钥

_PEM_MARKS = ("BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
              "BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY")


def _parse_ssh_key(text: str):
    """真解析一遍。返回 (ok, 说明, 指纹)。"""
    body = (text or "").strip()
    if not body:
        return False, "内容是空的", ""
    if "PRIVATE KEY" not in body:
        # 常见误拖：拖成了公钥（.pub）或者 known_hosts
        if "PUBLIC KEY" in body:
            return False, "这是公钥（.pub），要的是私钥文件（没有 .pub 后缀的那个）", ""
        return False, "看起来不是私钥文件（找不到 PRIVATE KEY 标记）", ""
    if not any(m in body for m in _PEM_MARKS):
        return False, "私钥格式不认识", ""
    try:
        import paramiko
    except Exception:
        # 没装 paramiko 就只做格式检查，别卡住保存
        return True, "格式看起来正确（未装 paramiko，跳过深度校验）", ""
    import io
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey,
                getattr(paramiko, "DSSKey", None)):
        if cls is None:
            continue
        try:
            k = cls.from_private_key(io.StringIO(body if body.endswith("\n") else body + "\n"))
            fp = ""
            try:
                import hashlib, base64
                raw = k.asbytes()
                fp = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")[:16]
            except Exception:
                fp = ""
            return True, "已通过校验（%s）" % cls.__name__.replace("Key", ""), fp
        except Exception:
            continue
    return False, "私钥解析失败（可能被截断、或带了口令）", ""


def ssh_key_status() -> dict:
    if not os.path.exists(SSH_KEY):
        return {"set": False, "hint": "", "fingerprint": "", "size": 0,
                "mtime": 0, "path": SSH_KEY}
    try:
        st = os.stat(SSH_KEY)
        with open(SSH_KEY, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except Exception as e:
        return {"set": False, "hint": "读取失败：%s" % e, "fingerprint": "",
                "size": 0, "mtime": 0, "path": SSH_KEY}
    ok, msg, fp = _parse_ssh_key(body)
    lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    return {
        "set": True,
        "valid": ok,
        "hint": msg,
        "preview": first[:46] + ("…" if len(first) > 46 else ""),
        "fingerprint": fp,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "path": SSH_KEY,
    }


def save_ssh_key(text: str) -> dict:
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        raise ValueError("私钥内容是空的")
    ok, msg, fp = _parse_ssh_key(body)
    if not ok:
        raise ValueError(msg)
    _ensure_dir()
    tmp = SSH_KEY + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body + "\n")
    os.replace(tmp, SSH_KEY)
    _harden(SSH_KEY)
    out = ssh_key_status()
    out["saved"] = True
    return out


def reveal_ssh_key() -> str:
    if not os.path.exists(SSH_KEY):
        return ""
    with open(SSH_KEY, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def clear_ssh_key() -> dict:
    try:
        if os.path.exists(SSH_KEY):
            os.remove(SSH_KEY)
    except Exception:
        pass
    return ssh_key_status()


# ------------------------------------------------------------------ 中继密钥

def relay_token_status() -> dict:
    d = _read_json(RELAY_CFG)
    tok = str(d.get("token") or "").strip()
    return {
        "set": bool(tok),
        "hint": _mask(tok) if tok else "",
        "url": str(d.get("url") or ""),
        "path": RELAY_CFG,
    }


def reveal_relay_token() -> str:
    return str(_read_json(RELAY_CFG).get("token") or "").strip()


def save_relay_token(token: str) -> dict:
    tok = (token or "").strip()
    if not tok:
        raise ValueError("中继密钥不能为空")
    if len(tok) < 16:
        raise ValueError("中继密钥太短（至少 16 位），确认是不是复制全了")
    if any(c in tok for c in " \t\n\r"):
        raise ValueError("中继密钥里不能有空格或换行")
    d = _read_json(RELAY_CFG)
    d["token"] = tok
    _write_json(RELAY_CFG, d)
    return relay_token_status()


def clear_relay_token() -> dict:
    d = _read_json(RELAY_CFG)
    d.pop("token", None)
    _write_json(RELAY_CFG, d)
    return relay_token_status()


# ------------------------------------------------------------------ 总览

def overview() -> dict:
    srv = server_config()
    key = ssh_key_status()
    tok = relay_token_status()
    ready = bool(srv["host"] and key.get("set") and key.get("valid", True) and tok["set"])
    missing = []
    if not srv["host"]:
        missing.append("服务器地址")
    if not key.get("set"):
        missing.append("SSH 私钥")
    elif not key.get("valid", True):
        missing.append("SSH 私钥（格式不对，需重新配置）")
    if not tok["set"]:
        missing.append("中继密钥")
    return {
        "server": srv,
        "ssh_key": key,
        "relay_token": tok,
        "ready": ready,
        "missing": missing,
        "deploy": deploy_state(),
    }


# ------------------------------------------------------------------ 一键部署

class _Job:
    """后台部署任务（同一时间只允许一个）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"          # idle / running / done / failed
        self.started = 0.0
        self.finished = 0.0
        self.exit_code = None
        self.lines = collections.deque(maxlen=1200)
        self.seq = 0                 # 已写入的行数（供前端增量拉取）
        self.proc = None

    # -- 日志
    def log(self, text: str) -> None:
        text = (text or "").rstrip("\n")
        if not text:
            return
        for ln in text.splitlines():
            with self.lock:
                self.seq += 1
                self.lines.append((self.seq, ln))

    def tail(self, since: int = 0) -> dict:
        with self.lock:
            items = [{"n": n, "t": t} for (n, t) in self.lines if n > since]
            return {"state": self.state, "seq": self.seq, "lines": items,
                    "exit_code": self.exit_code,
                    "started": self.started, "finished": self.finished}


_JOB = _Job()


def deploy_state() -> dict:
    with _JOB.lock:
        return {"state": _JOB.state, "seq": _JOB.seq,
                "exit_code": _JOB.exit_code,
                "started": _JOB.started, "finished": _JOB.finished}


def deploy_log(since: int = 0) -> dict:
    return _JOB.tail(since)


def _preflight() -> list:
    """部署前先自己查一遍，缺什么直接说清楚，别等 SSH 连上才炸。"""
    errs = []
    if not os.path.exists(SERVER_CFG):
        errs.append("还没填服务器地址")
    if not os.path.exists(SSH_KEY):
        errs.append("还没配置 SSH 私钥")
    if not os.path.exists(RELAY_CFG) or not _read_json(RELAY_CFG).get("token"):
        errs.append("还没有中继密钥（先在中继弹窗点「生成密钥」）")
    if not os.path.exists(DEPLOY_SCRIPT):
        errs.append("找不到 deploy/hive_relay_deploy.py")
    return errs


def start_deploy(restart_only: bool = False) -> dict:
    with _JOB.lock:
        if _JOB.state == "running":
            return {"ok": False, "error": "已经有一个部署在跑，等它结束"}
        errs = _preflight()
        if errs:
            return {"ok": False, "error": "；".join(errs), "missing": errs}

        _JOB.state = "running"
        _JOB.started = time.time()
        _JOB.finished = 0.0
        _JOB.exit_code = None
        _JOB.lines.clear()
        _JOB.seq = 0

    args = [sys.executable, "-u", DEPLOY_SCRIPT]
    if restart_only:
        args.append("--restart")

    def _run() -> None:
        _JOB.log("$ python deploy/hive_relay_deploy.py"
                 + (" --restart" if restart_only else ""))
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            p = subprocess.Popen(
                args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, bufsize=1,
                text=True, encoding="utf-8", errors="replace",
            )
            _JOB.proc = p
            assert p.stdout is not None
            for line in p.stdout:
                _JOB.log(line)
            code = p.wait()
            with _JOB.lock:
                _JOB.exit_code = code
                _JOB.finished = time.time()
                _JOB.state = "done" if code == 0 else "failed"
            _JOB.log("—— 部署%s（退出码 %s）——" % ("完成" if code == 0 else "失败", code))
        except Exception as e:
            with _JOB.lock:
                _JOB.finished = time.time()
                _JOB.exit_code = -1
                _JOB.state = "failed"
            _JOB.log("启动失败：%s: %s" % (type(e).__name__, e))

    threading.Thread(target=_run, daemon=True, name="deploy-job").start()
    return {"ok": True, "state": "running"}


def stop_deploy() -> dict:
    p = _JOB.proc
    if p and p.poll() is None:
        try:
            p.terminate()
            _JOB.log("—— 已手动中止 ——")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "当前没有在跑的部署"}
