# -*- coding: utf-8 -*-
"""RSI 基准（施工单 v1.1）接入验收 · 离线回归。

用户 2026-11-23 原话：
  「你仔细看看PDF并比对目前还差什么……一定要确保RSI的所有内容都完全完成
    且内容完整且完全正常」

这里把施工单第 3~9 章的「完成定义」八条 + 第 8 章双轨红线，全部固化成断言。
不联网、不重启、不改任何代码、不写任何文件（只读）。

跑法：python backend/_feature_tests/test_rsi_bench.py
"""
import hashlib
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import iterloop as IL      # noqa: E402

BENCH = r"D:\hwa_bench"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _read(rel):
    p = os.path.join(_ROOT, rel)
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def main():
    print("=" * 62)
    print("RSI 基准接入 · 验收回归（施工单 v1.1）")
    print("=" * 62)

    # ---------- 1) 完成定义 #1：ABILITY_PER_FILE 归零 ----------
    print("\n[1] T1-1 关闭「按文件给分」")
    check("ABILITY_PER_FILE == 0.0（不再按文件数白拿分）",
          float(IL.ABILITY_PER_FILE) == 0.0, str(IL.ABILITY_PER_FILE))
    src_il = _read("backend/iterloop.py")
    check("ability_part 里仍在引用它（口径一致，没删变量）",
          "ABILITY_PER_FILE" in src_il.split("def ability_part")[1][:4000])

    # ---------- 2) 完成定义 #2：回退倒扣 ----------
    print("\n[2] T1-2 修复回退")
    check("_fix_regressed_rows 存在", hasattr(IL, "_fix_regressed_rows"))
    check("已接进 ability_part 的行循环",
          "for _row in (_fix_regressed_rows(rg)" in src_il)
    # 真行为：失败 → 0；翻绿 → +3；**复发 → 自动退回 0**（施工单要的倒扣）
    # ⚠️ 必须把读写快照都换成内存里的假对象：否则这条断言会拿假用例名
    #    （t1）覆盖掉磁盘上真实的 ability_snapshot.json，污染能力分。
    _old_load, _old_save = IL._load_ability_snap, IL._save_ability_snap
    _box = {"snap": {"failed": ["fake.t1"], "fixed": [], "lessons": []}}
    IL._load_ability_snap = lambda: dict(_box["snap"])
    IL._save_ability_snap = lambda s: _box.__setitem__("snap", dict(s))
    try:
        r0 = IL._fix_landed_rows({"all_names": ["fake.t1"],
                                  "failed_names": ["fake.t1"]})
        r1 = IL._fix_landed_rows({"all_names": ["fake.t1"], "failed_names": []})
        r2 = IL._fix_landed_rows({"all_names": ["fake.t1"],
                                  "failed_names": ["fake.t1"]})
    finally:
        IL._load_ability_snap, IL._save_ability_snap = _old_load, _old_save
    check("一直失败 → 计 0（不白给分）", float(r0["score"]) == 0.0, str(r0["score"]))
    check("翻绿 → +3.0", float(r1["score"]) == 3.0, str(r1["score"]))
    check("复发 → 自动退回 0（不是「只发不退」）",
          float(r2["score"]) == 0.0, str(r2["score"]))

    # ---------- 3) 完成定义 #3：README 标签 ----------
    print("\n[3] T1-3 README 注明「这是资产账本」")
    readme = _read("README.md")
    check("README 含「这是**资产账本**」", "这是**资产账本**" in readme)
    check("README 指向 D:\\hwa_bench\\scores.jsonl 才是尺子",
          "D:\\hwa_bench\\scores.jsonl" in readme)

    # ---------- 4) 完成定义 #4：基准目录 ----------
    print("\n[4] T2 基准目录五文件 + 封印 + 探针样板")
    need = ["bench.py", "cases.json", "cases.seal", "runner.json", "scores.jsonl"]
    miss = [f for f in need if not os.path.isfile(os.path.join(BENCH, f))]
    check("五个文件齐全", not miss, "缺 %s" % miss)
    check("probe\\ 样板齐全（probe.txt / num.txt）",
          os.path.isfile(os.path.join(BENCH, "probe", "probe.txt"))
          and os.path.isfile(os.path.join(BENCH, "probe", "num.txt")))
    check("真实探针文件在位（D:\\hwa_probe）",
          os.path.isfile(r"D:\hwa_probe\probe.txt")
          and os.path.isfile(r"D:\hwa_probe\num.txt"))
    if not miss:
        raw = open(os.path.join(BENCH, "cases.json"), "rb").read()
        seal = open(os.path.join(BENCH, "cases.seal"), encoding="utf-8").read().strip()
        check("封印 == cases.json 原始字节哈希（改一个空格都会失配）",
              hashlib.sha256(raw).hexdigest() == seal)
        check("cases.json / bench.py 无 BOM",
              raw[:3] != b"\xef\xbb\xbf"
              and open(os.path.join(BENCH, "bench.py"), "rb").read()[:3]
              != b"\xef\xbb\xbf")
        # 链完整性：每行 prev/hash 可复算
        rows = [json.loads(x) for x in
                open(os.path.join(BENCH, "scores.jsonl"), encoding="utf-8")
                if x.strip()]
        prev, ok = "", True
        for r in rows:
            body = {k: v for k, v in r.items() if k != "hash"}
            want = hashlib.sha256(
                (prev + json.dumps(body, ensure_ascii=False, sort_keys=True))
                .encode("utf-8")).hexdigest()
            if r.get("prev") != prev or r.get("hash") != want:
                ok = False
                break
            prev = r.get("hash")
        check("scores.jsonl 哈希链完整（%d 行）" % len(rows), ok)

    # ---------- 5) 8.4：读不到就是 None，绝不补 0 ----------
    print("\n[5] 8.4 尺子读数：读不到 → None（不补 0、不估算）")
    old = IL.BENCH_SCORES
    try:
        IL.BENCH_SCORES = r"D:\__no_such_bench_dir__\scores.jsonl"
        check("文件不存在 → None（不是 0）", IL.read_bench_last() is None,
              str(IL.read_bench_last()))
        check("序列读不到 → 空列表（不造假点）",
              IL.read_bench_series() == [])
    finally:
        IL.BENCH_SCORES = old
    check("真实读数存在且带 pass/total",
          isinstance(IL.read_bench_last(), dict)
          and IL.read_bench_last().get("total"))

    # ---------- 6) 8.4：账本与尺子分开存 ----------
    print("\n[6] 8.4 两个数写进同一条记录，但字段分开")
    check('commit_round 记录里有 "ledger": cum', '"ledger": cum' in src_il)
    check('commit_round 记录里有 "bench": read_bench_last()',
          '"bench": read_bench_last()' in src_il)
    led = IL.read_ledger_series()
    check("账本序列可读（%d 点）" % len(led),
          isinstance(led, list) and all("v" in x for x in led))

    # ---------- 7) 8.2 唯一红线：禁止合成 ----------
    print("\n[7] 8.2 禁止合成（相加/加权/平均/归一化）")
    bad = []
    for rel in ("backend/iterloop.py", "backend/main.py",
                "frontend/iterjournal.js"):
        s = _read(rel)
        for pat in (r"ledger\s*[+*]\s*bench", r"bench\s*[+*]\s*ledger",
                    r"0\.\d\s*\*\s*ledger", r"\(ledger\s*\+\s*bench"):
            if re.search(pat, s):
                bad.append((rel, pat))
    check("没有任何把账本与尺子相加/加权的写法", not bad, str(bad))
    check("/api/iter/dual 只给两列原始点（没有合成字段）",
          'def iter_dual' in _read("backend/main.py")
          and '"ledger": led' in _read("backend/main.py")
          and '"bench": ben' in _read("backend/main.py"))

    # ---------- 8) 8.5：界面双轨 ----------
    print("\n[8] 8.5 界面：尺子在前、账本在后、两条线两个独立 Y 轴")
    js = _read("frontend/iterjournal.js")
    check("有尺子行（benchNode）", "function benchNode" in js)
    check("有账本行（ledgerNode）", "function ledgerNode" in js)
    check("有双轴曲线（dualChart）", "function dualChart" in js)
    check("尺子排在账本之前（首屏第一眼是尺子）",
          js.find("var _b0 = benchNode()") < js.find("var ln = ledgerNode()"))
    check("取不到数据时不画点（显示 —）", "账本（累计只增）：—" in js)
    check("双轴各按各自 min/max 画（不归一化到同一刻度）",
          "var lr = mm(L, \"v\")" in js and "var br = [0, tot > 0 ? tot : 15]" in js)

    # ---------- 9) C4：触发器受保护 ----------
    print("\n[9] C4 基准调用不许被自迭代删掉")
    check("commit_round 里的触发块有 🔒LOCK 标记",
          "🔒LOCK:rsi-bench-trigger" in src_il
          and "🔒END:rsi-bench-trigger" in src_il)
    i = src_il.find("# 🔒LOCK:rsi-bench-trigger")
    j = src_il.find("# 🔒END:rsi-bench-trigger")
    check("锁区里确实装着 subprocess.Popen 调用",
          "subprocess.Popen" in src_il[i:j])
    check("跑分用 CREATE_NO_WINDOW（不弹黑框）", "0x08000000" in src_il[i:j])

    # ---------- 收尾 ----------
    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    print("=" * 62)
    return 0 if not FAIL else 1


def run():
    return main()


if __name__ == "__main__":
    sys.exit(main())
