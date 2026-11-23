# -*- coding: utf-8 -*-
"""回归：孤立代理字符（半个 emoji）不能把任何一条写盘/请求链路搞崩。

真实故障史（data/app.log 可查）：
  · 2026-11-22 自我反思失败：模型返回的不是合法 JSON
  · 2026-11-23 自我反思失败：DeepSeek 400 lone leading surrogate
  · 2026-11-24 自我反思失败：UnicodeEncodeError '\\udbc2' surrogates not allowed
三者同源：模型输出被截断时会产生**半个 surrogate pair**（一个孤立的高位代理），
它既不能 utf-8 编码（写盘必崩），也会被服务端拒收（发请求必 400）。

本测试守四条真实路径：
  1. sessions._write        —— 所有会话写盘的唯一出口（原来裸 json.dump）
  2. sessions.save_messages —— selfiter / 主对话都走它，崩了是**静默丢写**
  3. self_iterate._write_json —— plans.json 等
  4. deepseek_client._sanitize —— 请求体
判据不是「函数不抛异常」，而是「落盘后读回来确实没有代理字符」。
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

# ⚠️ 必须用 chr() 构造，**不能**在源码里写字面量：
#   孤立代理字符字面量会让 Python 在编译这个文件时就抛
#   UnicodeEncodeError（实测 position 268），测试根本跑不起来。
_LONE_LOW = chr(0xDBC2)     # 低位代理单独存在
_LONE_HIGH = chr(0xD83D)    # 高位代理单独存在（被截断 emoji 的前半）
BAD = "前" + _LONE_LOW + "后"
BAD_EMOJI = _LONE_HIGH


def _has_surrogate(s: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in s)


def _read_text(p: str) -> str:
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def run() -> int:
    old = os.environ.get("MM_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="mm_surrogate_")
    os.environ["MM_DATA_DIR"] = tmp
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    try:
        from backend import sessions as S

        # 0) 前提：BAD 确实会崩 —— 否则这个测试是空的
        try:
            BAD.encode("utf-8")
            crashed = False
        except UnicodeEncodeError:
            crashed = True
        ck("前提：BAD 字符串确实无法 utf-8 编码", crashed)

        # 1) 清洗函数本身
        ck("_scrub_surrogates 去掉代理字符", not _has_surrogate(S._scrub_surrogates(BAD)))
        ck("_scrub_surrogates 保留正常字符",
           S._scrub_surrogates(BAD).startswith("前") and S._scrub_surrogates(BAD).endswith("后"))
        ck("_scrub_surrogates 递归进嵌套结构",
           not _has_surrogate(json.dumps(S._scrub_surrogates(
               {"a": [BAD_EMOJI], "b": {"c": BAD}}), ensure_ascii=False)))

        # 2) sessions._write：写盘不崩 + 落盘干净 + 能读回
        p = os.path.join(tmp, "w.json")
        ok = True
        try:
            S._write(p, [{"role": "assistant", "content": BAD},
                         {"role": "user", "content": BAD_EMOJI}])
        except Exception as e:                          # noqa: BLE001
            ok = False
            print("      _write 抛错：%s: %s" % (type(e).__name__, e))
        ck("sessions._write 遇到坏字符不崩", ok)
        ck("sessions._write 落盘文件可正常读回",
           ok and not _has_surrogate(_read_text(p)))
        ck("sessions._write 不残留 .tmp",
           not os.path.exists(p + ".tmp"))

        # 3) save_messages 端到端（selfiter 写会话走这条）
        sid = "t_surrogate_%d" % os.getpid()
        ok2 = True
        try:
            S.save_messages(sid, [{"role": "assistant", "content": "结果：" + BAD}])
            got = S.get_messages(sid)
        except Exception as e:                          # noqa: BLE001
            ok2 = False
            got = []
            print("      save_messages 抛错：%s: %s" % (type(e).__name__, e))
        ck("save_messages 端到端不崩", ok2)
        ck("save_messages 写入的内容能读回且无代理字符",
           bool(got) and not _has_surrogate(json.dumps(got, ensure_ascii=False)))

        # 4) self_iterate._write_json（plans.json 那条）
        from backend import self_iterate as SI
        p2 = os.path.join(tmp, "plans.json")
        ok3 = True
        try:
            SI._write_json(p2, {"plans": [{"summary": BAD, "id": "x"}]})
        except Exception as e:                          # noqa: BLE001
            ok3 = False
            print("      _write_json 抛错：%s: %s" % (type(e).__name__, e))
        ck("self_iterate._write_json 遇到坏字符不崩", ok3)
        ck("self_iterate._write_json 落盘干净",
           ok3 and not _has_surrogate(_read_text(p2)))

        # 5) 请求体清洗（DeepSeek 400 那条）
        from backend import deepseek_client as DC
        body = {"messages": [{"role": "user", "content": BAD_EMOJI}],
                "model": "x"}
        clean = DC._sanitize(body)
        raw = json.dumps(clean, ensure_ascii=True)      # 模拟 requests 默认序列化
        ck("_sanitize 后请求体可安全 JSON 化", "\ud83d" not in raw and "\\ud83d" not in raw)
        ck("_sanitize 不改变正常内容",
           DC._sanitize({"a": "正常😀文本"})["a"] == "正常😀文本")
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old

    print("\n%s（失败 %d 项）" % ("通过" if not fails else "未通过", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
