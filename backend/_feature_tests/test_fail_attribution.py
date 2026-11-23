# -*- coding: utf-8 -*-
"""回归：RSI 的失败归因必须「可执行」，且与动作成功率口径一致。

为什么有这个测试（2026-11-24 实测）：
  · 原来 rsi_weakest 对「动作成功率」的建议是「挑最近失败最多的动作类型」——
    但 actlog 的 `action` 是**计划内序号**（a1 / f6），同一编号在不同计划里指
    完全不同的事。按它聚合只会得到「a1 失败 18 次」这种没法行动的结论。
  · 加了 failure_breakdown 按「文件 / 原因」归因后，又踩了一个坑：
    `（已自动回滚）` 是**结果后缀**，几乎每条失败都带，分类表把它排在中间，
    结果 43% 的失败被归成「回滚」，真原因（锚点找不到 / 语法校验失败）全被吃掉。

本测试守四条：
  1. 分类顺序：「已自动回滚」不得抢走具体原因；
  2. 兜底：确实只有回滚、没有其它特征时才归「回滚（原因未分类）」；
  3. 护栏拦截优先于一切（它是安全机制，不是能力失败）；
  4. **口径一致**：failure_breakdown 的 n 必须等于 rsi_index 里的真失败数
     （ok/(ok+bad) 的 bad），否则「最该修」会指向一个和分数对不上的数字。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass


def run() -> int:
    old = os.environ.get("MM_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="mm_failattr_")
    os.environ["MM_DATA_DIR"] = tmp
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    try:
        from backend import lessonloop as LL

        # 1) 分类顺序：真原因不能被「已自动回滚」抢走
        cases = [
            ("backend/bootaudit.py：find 在文件里找不到（可能已被改过）（已自动回滚）",
             "锚点没命中"),
            ("backend/reply_gate.py 语法校验失败：SyntaxError（已自动回滚）",
             "语法校验失败"),
            ("行为回归新增失败 1 条：xxx（已自动回滚）", "行为回归失败"),
            ("模型请求补段后仍未给出合法 JSON（已自动回滚）", "模型没给出补丁"),
            ("目标文件不在允许范围：data/hack.json（已自动回滚）", "越界写入"),
            ("（已自动回滚）", "回滚（原因未分类）"),
            ("完全认不出来的原因", "其他"),
        ]
        for detail, want in cases:
            got = LL._fail_kind(detail)
            ck("归因「%s…」→ %s" % (detail[:22], want), got == want,
               "" if got == want else "实际=%s" % got)

        # 2) 护栏拦截必须优先（哪怕同时含「回滚」）
        ck("护栏拦截优先于回滚",
           LL._fail_kind("护栏文件被修改（不允许）：x.py（已自动回滚）") == "护栏拦截")

        # 3) 口径一致：造一份确定的 actlog，比对两个函数
        d = LL._data_dir()
        os.makedirs(d, exist_ok=True)
        rows = [
            {"ts": "t1", "action": "a1", "result": "ok", "files": ["backend/a.py"]},
            {"ts": "t2", "action": "a2", "result": "fail",
             "detail": "find 在文件里找不到（已自动回滚）", "files": ["backend/a.py"]},
            {"ts": "t3", "action": "a3", "result": "fail",
             "detail": "语法校验失败：SyntaxError（已自动回滚）", "files": ["backend/b.py"]},
            {"ts": "t4", "action": "a4", "result": "fail",
             "detail": "护栏文件被修改（不允许）（已自动回滚）", "files": ["backend/c.py"]},
            {"ts": "t5", "action": "a5", "result": "ok", "files": ["backend/b.py"]},
            {"ts": "t6", "action": "a6", "result": "fail", "probe": True,
             "detail": "探针失败", "files": ["backend/_probe_x.py"]},
            {"ts": "t7", "kind": "review_approve", "result": "fail", "detail": "审批"},
        ]
        with open(os.path.join(d, "actlog.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        LL._RSI_CACHE = {"ts": 0.0, "val": None}
        idx = LL.rsi_index()
        fb = LL.failure_breakdown()
        part = [p for p in idx["parts"] if p.get("name") == "动作成功率"][0]
        # detail 形如 "2/3（另 1 次护栏拦截…）"
        num = str(part.get("detail") or "").split("（")[0]
        # ⚠️ detail 是「ok / (ok+bad)」，不是「ok / bad」—— 失败数要减出来
        ok_n, total_n = [int(x) for x in num.split("/")]
        bad_n = total_n - ok_n
        ck("口径一致：failure_breakdown.n == 动作成功率的 bad",
           fb["n"] == bad_n, "fb=%s bad=%s" % (fb["n"], bad_n))
        ck("口径一致：ok/bad 符合造的数据（2 ok / 2 真失败 / 1 护栏）",
           (ok_n, bad_n) == (2, 2), "实际 %s/%s" % (ok_n, bad_n))
        ck("按文件归因排第一的是 backend/a.py",
           bool(fb["by_file"]) and fb["by_file"][0]["name"] == "backend/a.py",
           str(fb["by_file"][:1]))
        ck("归因里有「锚点没命中」和「语法校验失败」",
           {"锚点没命中", "语法校验失败"} <= {x["name"] for x in fb["by_kind"]},
           str([x["name"] for x in fb["by_kind"]]))

        # 4) 建议里不能再出现「按动作类型挑」这种不可执行的措辞
        w = LL.rsi_weakest()
        if w.get("name") == "动作成功率":
            ck("建议里带真实归因、且不按 action 序号挑",
               ("次" in w.get("advice") or "先修重复" in w.get("advice"))
               and "动作类型" not in w.get("advice", ""),
               w.get("advice", "")[:60])
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old

    print("\n%s（失败 %d 项）" % ("通过" if not fails else "未通过", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
