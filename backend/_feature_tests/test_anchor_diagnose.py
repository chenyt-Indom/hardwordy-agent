"""验证 patch_with_fallback 的锚点诊断（2026-11-24）。

对应 actlog 里的真实失败：
  · 「find 为空」被误报成「命中 48607 处，不唯一」；
  · 「命中 N 处，不唯一」只回一句废话，下一轮照样瞎猜；
  · 不带 fail_kind 调 selfdev.patch 先被幽灵闸门拦成 reason=phantom，
    把「锚点命中多处」误诊成「幽灵修复」。
本文件逐条断言这些行为，且断言**失败时文件绝不被改动**。
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import lessonloop as L      # noqa: E402
from backend import selfdev as SD        # noqa: E402

# 夹具文件名每次进程唯一：固定名字 60 分钟内反复改会触发熔断，测试会假失败
REL = "backend/_feature_tests/_tmp_anchor_demo_%d.py" % os.getpid()


def _utf8():
    """只改编码，**不新建** sys.stdout 对象。

    ⚠️ 2026-11-24 实测：原来在模块顶层 `sys.stdout = TextIOWrapper(sys.stdout.buffer)`，
    模块被回收时 wrapper 连带关闭了底层缓冲 → regress 里后续用例全报
    `ValueError: I/O operation on closed file`（regress.py:644 的注释就是这条坑）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                    # noqa: BLE001
        pass
SRC = '''def alpha():
    return out


def beta():
    return out


def gamma():
    x = 1
    return out
'''

_results = []


def check(name, cond, extra=""):
    _results.append((name, bool(cond), extra))
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))


def reset():
    r = SD.write(REL, SRC, note="测试夹具")
    if not r.get("ok") and "熔断" in str(r.get("error")):
        # 同一个夹具反复跑会撞「60 分钟内改成功 10 次」的熔断，这是护栏在正常工作，
        # 不该让测试因此失败 —— 删掉重来，计数清空。
        try:
            os.remove(os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), REL.replace("/", os.sep)))
        except OSError:
            pass
        r = SD.write(REL, SRC, note="测试夹具")
    assert r.get("ok"), r


def body():
    return SD.read(REL).get("raw") or ""


def main():
    _utf8()
    reset()

    # ① find 为空 → 必须报 find_empty，不能报「不唯一」
    r = L.patch_with_fallback(REL, "", "X", note="t")
    check("空锚点 -> reason=find_empty", r.get("reason") == "find_empty", r.get("error"))
    check("空锚点不再误报不唯一", "不唯一" not in str(r.get("error")), r.get("error"))
    check("空锚点不动文件", body() == SRC)

    # ② 空白锚点同理
    r = L.patch_with_fallback(REL, "   \n  ", "X", note="t")
    check("空白锚点 -> find_empty", r.get("reason") == "find_empty", r.get("reason"))

    # ③ 太短的锚点 → 提前拦下，而不是撞「不唯一」
    r = L.patch_with_fallback(REL, "}", "X", note="t")
    check("短锚点 -> anchor_too_short", r.get("reason") == "anchor_too_short", r.get("reason"))
    check("短锚点不动文件", body() == SRC)

    # ④ 命中多处 → reason=ambiguous（不是 phantom），且必须给出候选行号
    r = L.patch_with_fallback(REL, "    return out\n", "    return 0\n", note="t")
    cands = r.get("candidates") or []
    check("多处命中 -> reason=ambiguous", r.get("reason") == "ambiguous", r.get("reason"))
    check("多处命中不再误报 phantom", r.get("reason") != "phantom" and not r.get("phantom"),
          r.get("reason"))
    check("多处命中给出候选", len(cands) >= 2, "candidates=%d" % len(cands))
    check("候选带行号", all(isinstance(c.get("line"), int) for c in cands), cands[:2])
    check("候选带上下文", all("prev" in c and "next" in c for c in cands), cands[:1])
    check("多处命中绝不动文件", body() == SRC, "文件被改了！")
    check("候选行号真实（含第2行和第6行）",
          any(c["line"] == 2 for c in cands) and any(c["line"] == 6 for c in cands),
          [c["line"] for c in cands])
    check("候选上下文指对了函数", any(c["prev"] == "def alpha():" for c in cands),
          [c["prev"] for c in cands])

    # ⑤ 没命中 → 给出最相近的真实行，且 reason 不是 phantom
    r = L.patch_with_fallback(REL, "    return result_value\n", "    return 0\n", note="t")
    near = r.get("near") or []
    check("未命中 -> ok=False", not r.get("ok"))
    check("未命中 -> reason=anchor_miss", r.get("reason") == "anchor_miss", r.get("reason"))
    check("未命中给出相近行", len(near) >= 1, near[:2])
    check("未命中不动文件", body() == SRC)

    # ⑥ 唯一命中 → 必须真的改成功（证明没把正常路径改坏）
    r = L.patch_with_fallback(REL, "    x = 1\n    return out\n", "    x = 2\n    return out\n",
                              note="t")
    check("唯一锚点 -> 改成功", r.get("ok"), r.get("error"))
    check("唯一锚点 -> 文件真的变了", "x = 2" in body(), body()[-60:])

    # ⑦ 降级路径仍然可用（缩进/空白不一致）
    reset()
    r = L.patch_with_fallback(REL, "def alpha():\nreturn out", "def alpha():\n    return 1",
                              note="t")
    check("空白不一致 -> 降级命中", r.get("ok") and r.get("fallback") == "loose",
          (r.get("ok"), r.get("fallback"), r.get("error")))
    check("降级后文件正确", "return 1" in body())

    # 收尾：删掉夹具
    try:
        os.remove(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), REL.replace("/", os.sep)))
    except OSError:
        pass

    bad = [x for x in _results if not x[1]]
    print("\n通过 %d / 失败 %d" % (len(_results) - len(bad), len(bad)))
    for n, _, e in bad:
        print("  FAILED:", n, e)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
