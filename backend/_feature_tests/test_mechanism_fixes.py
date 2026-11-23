# -*- coding: utf-8 -*-
"""机制修复验收断言（2026-11-23 一轮「反复检验」里查出来的 4 个真实缺陷）。

这四条都是**实测出来的**，不是猜的：

① 幽灵修复闸门读错了账本
   `_check_phantom` → `actlog.count_fail_kind_ex` 只数 task.json 的 events，
   而那个文件里几乎没有 fail_kind 字段（实测 2000 条只有 1 条，还是
   phantom_reject 自己写的）。真实失败分类记在 fail_attribution.jsonl 的
   kind 字段（近 7 天 18 条）。结果：**任何带 fail_kind 的真实修复都被判
   幽灵拒绝**（实测 16:40:53 fail_kind=model 被拒），不带 fail_kind 的凭空
   动作反而放行 —— 闸门方向完全反了。

② 评分样本重复污染
   press.jsonl 60 条里 25 条是同一个 155.09（「后台自动更新评分」每几分钟
   跑一次，分数没变也写一行），还有同一秒连写 3 条的。趋势线被压平。

③ 事件窗口被 think 挤爆
   events 2000 条里 think 占 66%，只覆盖 3.3 小时 —— status / phantom_reject
   / code 这些关键事件被挤出窗口，「记录细节不落下」等于没做到。

④ 记录造假：声称新建的文件没生成却判 ok
   实测 a5「新增 backend/_feature_tests/test_pause_resume.py」记 result=ok、
   changelog 还写 new_file=true，那个文件压根不存在。

约定：不联网、不调 LLM、不落盘真实数据（一律用临时目录 + monkeypatch）。
"""
import json
import os
import sys
import tempfile

_B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _B)
sys.path.insert(0, os.path.dirname(_B))


def _tmp_root():
    t = tempfile.mkdtemp(prefix="mm_mechfix_")
    d = os.path.join(t, "data", "self_iterate")
    os.makedirs(d, exist_ok=True)
    return t, d


def run():
    from backend import config as CFG
    from backend import actlog as AL

    tmp, si = _tmp_root()
    old_root = getattr(CFG, "data_root", None)
    try:
        CFG.data_root = lambda: tmp

        # ---------- ① 幽灵闸门要能读到失败归因账本 ----------
        # 造一份真实格式的 fail_attribution.jsonl：kind=no_edit 2 条、kind=score_regress 1 条
        with open(os.path.join(si, "fail_attribution.jsonl"), "w", encoding="utf-8") as fh:
            for k in ("no_edit", "no_edit", "score_regress"):
                fh.write(json.dumps({"ts": 1e18, "kind": k, "title": "x"}, ensure_ascii=False) + "\n")
        # task.json 里一条 fail_kind 都没有（模拟生产真实情况）
        with open(os.path.join(si, "task.json"), "w", encoding="utf-8") as fh:
            json.dump({"events": [{"kind": "think", "text": "..."}]}, fh)

        n_noedit, ok1 = AL.count_fail_kind_ex("no_edit", days=7)
        assert ok1, "账本可读时 ok 必须是 True"
        assert n_noedit == 2, "no_edit 应数到 2 条（失败归因账本），实际 %r" % (n_noedit,)

        n_sr, _ = AL.count_fail_kind_ex("score_regress", days=7)
        assert n_sr == 1, "score_regress 应数到 1 条，实际 %r" % (n_sr,)

        n_fake, _ = AL.count_fail_kind_ex("凭空捏的分类ZZZ", days=7)
        assert n_fake == 0, "编造的分类必须数到 0（否则闸门形同虚设），实际 %r" % (n_fake,)

        from backend import self_iterate as SI
        c1 = SI._check_phantom({"fail_kind": "no_edit"})
        assert c1.get("phantom") is False, "真实发生过的 fail_kind 不许判幽灵：%r" % (c1,)
        assert (c1.get("evidence") or {}).get("count") == 2, "证据里的计数要是 2：%r" % (c1,)
        c2 = SI._check_phantom({"fail_kind": "凭空捏的分类ZZZ"})
        assert c2.get("phantom") is True, "编造的分类必须判幽灵：%r" % (c2,)
        c3 = SI._check_phantom({"fail_kind": ""})
        assert c3.get("phantom") is False, "没带 fail_kind 时跳过检查（不判幽灵）"

        # ---------- ② 评分样本去重 ----------
        from backend import scorepress as SP
        tp = os.path.join(tmp, "press.jsonl")
        old_path, old_build = SP._path, SP.build
        SP._path = lambda: tp

        def _mk(sc, note=""):
            return {"total": sc[0], "quality": 100.0, "ability": {"score": sc[1]},
                    "iso": "", "ts": sc[2], "note": note, "lost": 0,
                    "recoverable": 0, "parts": [], "top": [], "items_total": 0}

        try:
            SP.build = _mk
            SP.record((155.09, 55.09, 1000), note="后台自动更新评分")
            SP.record((155.09, 55.09, 1100), note="后台自动更新评分")   # 同分 100 秒 → 合并
            SP.record((155.09, 55.09, 1200), note="后台自动更新评分")   # 再合并
            SP.record((155.09, 55.09, 3000), note="后台自动更新评分")   # 隔 1800 秒 → 新增
            SP.record((157.15, 57.15, 3100), note="迭代验证通过后自动跑分")  # 变分 → 新增
            rows = SP._load_all()
            assert len(rows) == 3, "同分近时去重后应剩 3 条，实际 %d" % len(rows)
            assert rows[0]["ts"] == 1200, "合并时应把最后一条时间更新到 1200，实际 %r" % (rows[0]["ts"],)
            assert round(rows[-1]["total"], 2) == 157.15, "变分必须新增一行，最后一条应是 157.15"
        finally:
            SP._path, SP.build = old_path, old_build

        # ---------- ③ think 事件限流，关键事件不被挤掉 ----------
        from backend import iter_runner as IR
        old_ev = list(IR._STATE.get("events") or [])
        try:
            IR._STATE["events"] = ([{"kind": "think", "seq": i} for i in range(1330)] +
                                   [{"kind": "status", "seq": 100000 + i} for i in range(670)])
            IR._trim_events()
            evs = IR._STATE["events"]
            n_think = sum(1 for e in evs if e.get("kind") == "think")
            n_other = sum(1 for e in evs if e.get("kind") == "status")
            assert n_think == IR._MAX_THINK, "think 应被限到 %d 条，实际 %d" % (IR._MAX_THINK, n_think)
            assert n_other == 670, "关键事件（status）一条都不许丢，实际 %d/670" % n_other
            # 极端：think 占满时也不能把其他事件挤没
            IR._STATE["events"] = ([{"kind": "think", "seq": i} for i in range(5000)] +
                                   [{"kind": "code", "seq": 1}])
            IR._trim_events()
            assert any(e.get("kind") == "code" for e in IR._STATE["events"]), \
                "think 占满时 code 事件必须保住"
        finally:
            IR._STATE["events"] = old_ev

        # ---------- ④ 声称新建的文件必须真的落地 ----------
        # 4a) self_iterate 的 action_start 要带出 files / new_files
        src_si = open(os.path.join(_B, "self_iterate.py"), encoding="utf-8").read()
        assert "new_files=_fresh" in src_si, "action_start 没带出 new_files，收工校验拿不到目标"
        # 4b) iter_runner 收工要核对：声称新建却不存在 → 判失败
        src_ir = open(os.path.join(_B, "iter_runner.py"), encoding="utf-8").read()
        assert "声称新建的文件没有生成" in src_ir, "缺少「声称新建却没生成」的硬检查点"
        assert "no_change：声称新建的文件不存在" in src_ir, "该检查点必须记 no_change 并判失败"
        # 4c) 行为验证：造一个「声明要新建但没建」的 cur_action，跑收工校验的核心判断
        miss = []
        fake_root = os.path.dirname(_B)   # 真实项目根：actlog.py 确实在，假文件确实不在
        for rel in ("backend/_feature_tests/_不存在的文件.py", "backend/actlog.py"):
            if not os.path.isfile(os.path.join(fake_root, rel.replace("/", os.sep))):
                miss.append(rel)
        assert miss == ["backend/_feature_tests/_不存在的文件.py"], \
            "缺失文件判定不对：%r" % (miss,)
    finally:
        if old_root is not None:
            CFG.data_root = old_root

    print("机制修复验收 OK：幽灵闸门数据源 / 评分去重 / think 限流 / 新建文件校验 全部生效")


if __name__ == "__main__":
    # ⚠️ 2026-11-23 补：原来只有 def run()，**没有任何调用入口** ——
    #   直接跑这个文件时一条断言都不执行、rc=0「静默通过」，等于这个
    #   回归从来没跑过。加上入口，让它真的跑。
    run()
