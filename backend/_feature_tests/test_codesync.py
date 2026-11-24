# -*- coding: utf-8 -*-
"""回归：codesync 代码指纹对齐 —— 「改了」必须等于「生效了」。

真实故障史（2026-11-24）：
  · 用户报「新更新的功能没成功运行」。查下来**不是缓存**，而是没人核对
    「磁盘上的代码」和「进程里跑着的代码」是不是同一份 —— 改了没重启，
    界面和日志都一切正常，只能靠"感觉没变化"；
  · 前端自更新清单写死 4 个文件，后来加的 livecard / taskcard 改了不触发刷新。

本测试守六条：
  1. 指纹只看运行时代码：.bak / 测试目录 / 隐藏文件 不参与
  2. 同一份代码两次采样必须一致（不能自己抖）
  3. 漂移要**连续 2 次采样一致**才算落定（防止写盘中途误判）
  4. 验证通过 → 排队重启；同一份改动最多试 2 次（防无限重启）
  5. 验证失败 → 走恢复（能定位就局部回滚），且**不重启**
  6. 正在生成回复（_chat_busy>0）时，空闲判定必须是 0（绝不能打断对话）
"""
import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                           # noqa: BLE001
    pass

from backend import codesync as cs                          # noqa: E402

_OK = []
_BAD = []


def check(name, cond, extra=""):
    (_OK if cond else _BAD).append(name)
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  " + str(extra)) if (extra and not cond) else ""))


class _Iso(object):
    """把状态文件指到临时目录，避免测试污染真实 data/。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="codesync_")
        self.state = os.path.join(self.tmp, "codesync_state.json")
        self._p = None

    def __enter__(self):
        self._p = mock.patch.object(cs, "_state_path", lambda: self.state)
        self._p.start()
        return self

    def __exit__(self, *a):
        try:
            self._p.stop()
        except Exception:                                   # noqa: BLE001
            pass
        return False


# ---------------------------------------------------------------- 1/2
def test_scan_rules():
    """指纹只认运行时代码：.bak、隐藏文件、测试目录都不算。"""
    d = tempfile.mkdtemp(prefix="cs_scan_")
    open(os.path.join(d, "a.py"), "w", encoding="utf-8").write("x = 1\n")
    open(os.path.join(d, "b.py.bak-2026"), "w", encoding="utf-8").write("old\n")
    open(os.path.join(d, "c.py.bak"), "w", encoding="utf-8").write("old\n")
    open(os.path.join(d, ".hidden.py"), "w", encoding="utf-8").write("h\n")
    open(os.path.join(d, "notes.txt"), "w", encoding="utf-8").write("t\n")
    os.makedirs(os.path.join(d, "_feature_tests"), exist_ok=True)
    open(os.path.join(d, "_feature_tests", "test_x.py"), "w",
         encoding="utf-8").write("x = 2\n")

    items = cs._scan(d, (".py",))
    names = [it[0] for it in items]
    check("只收 .py", names == ["a.py"], names)
    check(".bak 被排除", not any(".bak" in n for n in names), names)
    check("测试目录被排除", not any("_feature_tests" in n for n in names), names)
    check("隐藏文件被排除", not any(n.startswith(".") for n in names), names)


def test_fingerprint_stable():
    """真实目录连扫两次必须一致（指纹不能自己抖）。"""
    a = cs.fingerprint()
    b = cs.fingerprint()
    check("两次采样指纹一致", a["fp"] == b["fp"] and bool(a["fp"]), a["fp"])
    check("指纹扫到了后端 .py", a["backend"] > 10, a["backend"])
    check("指纹扫到了前端文件", a["frontend"] > 0, a["frontend"])


def test_fingerprint_changes():
    """内容/时间变了，指纹必须变（这是"有没有新改动"的唯一依据）。"""
    d = tempfile.mkdtemp(prefix="cs_fp_")
    p = os.path.join(d, "m.py")
    open(p, "w", encoding="utf-8").write("v = 1\n")
    fp1 = cs._digest(cs._scan(d, (".py",)))
    import time
    time.sleep(0.01)
    open(p, "w", encoding="utf-8").write("v = 22\n")
    fp2 = cs._digest(cs._scan(d, (".py",)))
    check("改动后指纹变化", fp1 != fp2, "%s -> %s" % (fp1, fp2))


# ---------------------------------------------------------------- 3/4/5
def test_stable_samples_required():
    """漂移第 1 次只观察，第 2 次才落定 —— 防止文件写到一半就判成新版本。"""
    with _Iso() as iso:
        old_rt = cs._RUNTIME["fp"]
        try:
            cs._RUNTIME["fp"] = "runtime0000000"
            calls = []

            def _fake_handle(fp, st):
                calls.append(fp)
                return {"action": "queued"}

            with mock.patch.object(cs, "fingerprint",
                                   lambda: {"fp": "disk111111111", "files": [],
                                            "backend": 1, "frontend": 0,
                                            "at": "now"}), \
                 mock.patch.object(cs, "_handle", _fake_handle):
                r1 = cs._tick()
                check("第 1 次采样不处理", not calls and r1.get("wait", "").startswith("观察中"),
                      r1)
                r2 = cs._tick()
                check("第 2 次采样才落定", len(calls) == 1 and calls[0] == "disk111111111",
                      (calls, r2))
        finally:
            cs._RUNTIME["fp"] = old_rt


def test_no_drift_noop():
    """磁盘和运行中一致时，什么都不做。"""
    with _Iso():
        old_rt = cs._RUNTIME["fp"]
        try:
            cs._RUNTIME["fp"] = "same00000000000"
            with mock.patch.object(cs, "fingerprint",
                                   lambda: {"fp": "same00000000000", "files": [],
                                            "backend": 1, "frontend": 0, "at": "now"}):
                r = cs._tick()
            check("无漂移不处理", r.get("drifted") is False, r)
        finally:
            cs._RUNTIME["fp"] = old_rt


def test_verify_ok_queues_restart():
    """验证通过 + 自动迭代任务在跑 → 排队重启，并记一次尝试。"""
    with _Iso():
        st = cs._blank_state()
        with mock.patch.object(cs, "verify", lambda: {"ok": True, "why": "ok"}), \
             mock.patch.object(cs, "_auto_restart_allowed",
                               lambda: (True, "自动迭代任务正在跑")), \
             mock.patch.object(cs, "_save_state", lambda d: None):
            r = cs._handle("fp_ok", st)
        check("通过后排队重启", r.get("action") == "queued", r)
        check("记下待重启指纹", (st.get("pending_restart") or {}).get("fp") == "fp_ok",
              st.get("pending_restart"))
        check("尝试次数 +1", (st.get("attempts") or {}).get("fp_ok") == 1,
              st.get("attempts"))


def test_non_iteration_only_records():
    """★ 2026-11-25 用户要求：非迭代任务不自动重启，只记待重启等用户点按钮。

    用户原话：「为什么又是在非自动迭代任务时自动重启了？不应该要有一个
    自动重启的按钮吗」。所以这里断言：闸门关着时
      · 动作是 manual_only（不是 queued）；
      · 待重启带 manual 标记（_tick 不会自动执行它）；
      · 尝试次数**不增加**（没真的重启过，不该消耗配额）。
    """
    with _Iso():
        st = cs._blank_state()
        with mock.patch.object(cs, "verify", lambda: {"ok": True, "why": "ok"}), \
             mock.patch.object(cs, "_auto_restart_allowed",
                               lambda: (False, "非迭代任务：只记录待重启")), \
             mock.patch.object(cs, "_save_state", lambda d: None):
            r = cs._handle("fp_manual", st)
        check("非迭代不排队自动重启", r.get("action") == "manual_only", r)
        check("待重启带 manual 标记",
              (st.get("pending_restart") or {}).get("manual") is True,
              st.get("pending_restart"))
        check("不消耗重启配额", not (st.get("attempts") or {}).get("fp_manual"),
              st.get("attempts"))
        check("告诉前端需要手动重启", r.get("need_manual_restart") is True, r)


def test_attempt_cap():
    """同一份改动已试过 2 次 → 放弃，绝不无限重启。"""
    with _Iso():
        st = cs._blank_state()
        st["attempts"] = {"fp_cap": cs.MAX_ATTEMPTS}
        with mock.patch.object(cs, "verify", lambda: {"ok": True, "why": "ok"}), \
             mock.patch.object(cs, "_save_state", lambda d: None):
            r = cs._handle("fp_cap", st)
        check("超过次数就放弃", r.get("action") == "give_up", r)
        check("放弃时不留待重启", not st.get("pending_restart"), st.get("pending_restart"))


def test_verify_fail_recovers():
    """验证失败 → 走恢复，且**不排队重启**（绝不把坏代码带上线）。"""
    with _Iso():
        st = cs._blank_state()
        called = {}

        def _fake_recover(bad, s):
            called["bad"] = bad
            return {"ok": True, "mode": "partial"}

        bad = [{"rel": "backend/x.py", "error": "SyntaxError"}]
        with mock.patch.object(cs, "verify",
                               lambda: {"ok": False, "why": "语法错", "bad": bad}), \
             mock.patch.object(cs, "_recover", _fake_recover), \
             mock.patch.object(cs, "_save_state", lambda d: None):
            r = cs._handle("fp_bad", st)
        check("失败走恢复", r.get("action") == "recover", r)
        check("恢复拿到坏文件清单", called.get("bad") == bad, called)
        check("失败不排队重启", not st.get("pending_restart"), st.get("pending_restart"))
        check("失败也标记已处理", st.get("done_fp") == "fp_bad", st.get("done_fp"))


def test_recover_partial_vs_full():
    """坏文件 ≤3 个 → 只回滚这几个；说不清 → 整轮回滚。"""
    fake_v = types.ModuleType("backend.version")

    class _V(object):
        def __init__(self):
            self.calls = []

        def available(self):
            return {"ok": True}

        def current(self):
            return {"hash": "abc1234"}

        def list_versions(self, limit=1):
            return {"versions": [{"hash": "abc1234"}]}

        def rollback(self, h, files=None, note=""):
            self.calls.append({"hash": h, "files": files, "note": note})
            return {"ok": True}

    v = _V()
    fake_v.available, fake_v.current = v.available, v.current
    fake_v.list_versions, fake_v.rollback = v.list_versions, v.rollback
    with mock.patch.dict(sys.modules, {"backend.version": fake_v}):
        r1 = cs._recover([{"rel": "backend/a.py"}], {})
        check("单个坏文件走局部回滚", r1.get("mode") == "partial", r1)
        check("局部回滚只带坏文件",
              (v.calls[-1].get("files") or []) == ["backend/a.py"], v.calls[-1])
        r2 = cs._recover([], {})
        check("说不清就走整轮", r2.get("mode") == "full", r2)
        check("整轮回滚 files 为空", v.calls[-1].get("files") is None, v.calls[-1])


# ---------------------------------------------------------------- 6
def test_idle_respects_chat_busy():
    """正在生成回复时，空闲秒数必须是 0（重启会打断这一轮）。"""
    fake_main = types.ModuleType("backend.main")
    fake_main._chat_busy = 1
    fake_main._LAST_REQUEST_TS = 0.0
    with mock.patch.dict(sys.modules, {"backend.main": fake_main}):
        v = cs._idle_sec()
    check("有回复在生成 → 不空闲", v == 0.0, v)

    fake_main._chat_busy = 0
    import time as _t
    fake_main._LAST_REQUEST_TS = _t.time() - 120
    with mock.patch.dict(sys.modules, {"backend.main": fake_main}):
        v2 = cs._idle_sec()
    check("空闲 120 秒 → 可重启", v2 > cs.IDLE_NEED_SEC, v2)


def test_pending_restart_when_idle():
    """有排队重启 + 空闲 → 真的调重启；不空闲 → 继续等（绝不打断对话）。"""
    import time as _t
    with _Iso():
        old_rt = cs._RUNTIME["fp"]
        try:
            cs._RUNTIME["fp"] = "rt0000000000000"
            st = cs._blank_state()
            st["done_fp"] = "disk2222222222"
            st["pending_restart"] = {"fp": "disk2222222222",
                                     "since": _t.time() - 5, "why": "ok"}
            cs._save_state(st)

            hits = []
            fake_fp = lambda: {"fp": "disk2222222222", "files": [],     # noqa: E731
                               "backend": 1, "frontend": 0, "at": "now"}

            with mock.patch.object(cs, "fingerprint", fake_fp), \
                 mock.patch.object(cs, "_idle_sec", lambda: 0.0), \
                 mock.patch.object(cs, "_do_restart",
                                   lambda fp: hits.append(fp) or {"ok": True}):
                cs._tick()
            check("不空闲时绝不重启", not hits, hits)

            with mock.patch.object(cs, "fingerprint", fake_fp), \
                 mock.patch.object(cs, "_idle_sec", lambda: 999.0), \
                 mock.patch.object(cs, "_do_restart",
                                   lambda fp: hits.append(fp) or {"ok": True}):
                r = cs._tick()
            check("空闲时执行自动重启", hits == ["disk2222222222"], (hits, r))
        finally:
            cs._RUNTIME["fp"] = old_rt


def test_do_restart_marks_post_verify():
    """重启前必须先打「重启后复核」标记 —— 否则重启后没人验收。"""
    calls = []
    fake_main = types.ModuleType("backend.main")

    def _apply(body=None):
        calls.append(("restart", body))
        return {"ok": True}

    fake_main.selfdev_apply_restart = _apply
    fake_il = types.ModuleType("backend.iterloop")
    fake_il.mark_post_verify = lambda pid="": calls.append(("mark", pid))

    with mock.patch.dict(sys.modules, {"backend.main": fake_main,
                                       "backend.iterloop": fake_il}):
        r = cs._do_restart("fpX")

    check("先打复核标记再重启", [c[0] for c in calls] == ["mark", "restart"], calls)
    check("重启请求带来源 codesync",
          (calls[-1][1] or {}).get("src") == "codesync", calls)
    check("重启返回透传", r.get("ok") is True, r)


def test_status_shape():
    """status 必须给前端/排查用的字段都在。"""
    with _Iso():
        s = cs.status()
    for k in ("runtime", "disk", "drifted", "history", "params", "pending_restart"):
        check("status 有 %s" % k, k in s, list(s.keys()))
    check("runtime 带指纹", "fp" in (s.get("runtime") or {}), s.get("runtime"))
    check("params 参数齐全", "poll" in (s.get("params") or {}), s.get("params"))


def test_routes_registered():
    """路由必须**真的注册到 app 上** —— 光有函数不算。

    真实事故（就是本轮自己踩的）：插入接口时缩进跟了上一行，整块被缩进进
    `frontend_version()` 函数体内。语法合法、导入也过、日志一切正常，
    但 `return` 之后的代码永远执行不到 → 路由根本没注册 → 前端 404。
    只有静态自检的 unreachable 才抓出来。所以这里必须锁住。
    """
    try:
        import backend.main as M
    except Exception as e:                                  # noqa: BLE001
        check("import backend.main", False, str(e)[:200])
        return

    paths = set()
    for r in getattr(M.app, "routes", []) or []:
        p = getattr(r, "path", "")
        if p:
            paths.add(p)
    check("注册了 /api/codesync/status", "/api/codesync/status" in paths)
    check("注册了 /api/codesync/check", "/api/codesync/check" in paths)

    # startup 钩子必须真挂在 app 上，否则巡检线程根本不会起
    names = [getattr(fn, "__name__", "") for fn in
             (getattr(getattr(M.app, "router", None), "on_startup", []) or [])]
    check("startup 挂上了代码巡检", "_codesync_watch" in names, names[:20])

    # 前端自更新必须走动态清单（写死 4 个文件时，新加的 js 改了不刷新）
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    except OSError:
        src = ""
    check("前端版本走动态清单", "for name in _fe_files()" in src)
    check("前端清单含 livecard/taskcard",
          all(n in src for n in ("livecard", "taskcard")) or "_FE_ROOT_EXT" in src)


def main() -> int:
    for fn in (test_scan_rules, test_fingerprint_stable, test_fingerprint_changes,
               test_stable_samples_required, test_no_drift_noop,
               test_verify_ok_queues_restart, test_attempt_cap,
               test_verify_fail_recovers, test_recover_partial_vs_full,
               test_idle_respects_chat_busy, test_pending_restart_when_idle,
               test_do_restart_marks_post_verify, test_status_shape,
               test_routes_registered):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 未抛异常" % fn.__name__, False, str(e)[:200])

    print("\n通过 %d / 失败 %d" % (len(_OK), len(_BAD)))
    if _BAD:
        print("失败项：" + "；".join(_BAD[:12]))
    return 0 if not _BAD else 1


if __name__ == "__main__":
    sys.exit(main())
