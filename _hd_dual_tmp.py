# -*- coding: utf-8 -*-
"""验证 @hd 双通道：云端 deepseek-flash（默认配置）+ 本地 Ollama。"""
import os, sys, json, time, threading, shutil

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import backend.main as M
from backend import hive, hive_client, deepseek_client

PASS, FAIL = [], []
def chk(n, c, e=""):
    (PASS if c else FAIL).append(n)
    print(("  [OK] " if c else "  [XX] ") + n + (("  " + str(e)) if e and not c else ""))

frames = []
M._broadcast_hive = lambda ev: frames.append(ev)
hive_client.client().send_stream = lambda *a, **k: True
hive_client.client().send_cancel = lambda *a, **k: True
M._ds_key = lambda h="": "sk-fake-key-for-test"

g = hive.create_group("双通道测试", "群主")
gid = g["gid"]

print("\n[1] 云端通道（默认模型 deepseek-flash）")
M.config.load_config = lambda: {"default_model": "deepseek-flash"}
def fake_ds(messages, model, api_key, temperature=0.7, max_tokens=8192,
            thinking=None, reasoning_effort=None):
    assert api_key == "sk-fake-key-for-test", api_key
    assert thinking is True, "必须强制开思考（否则看不到思考过程）"
    for t in ["先看需求", "再想实现"]:
        yield ("thinking", t)
    for c in ["方案是", "这样写。"]:
        yield ("content", c)
deepseek_client.chat_stream = fake_ds
frames.clear()
M._hive_run_model_now(gid, "写个登录页", {}, "hd_ds1", "张三", "u_zhangsan")
ph = [f["frame"]["phase"] for f in frames if f.get("type") == "hd_stream"]
chk("云端通道有 start/thinking/content/done", set(["start","thinking","content","done"]).issubset(set(ph)), ph)
th = "".join(f["frame"]["delta"] for f in frames if f.get("type")=="hd_stream" and f["frame"]["phase"]=="thinking")
ct = "".join(f["frame"]["delta"] for f in frames if f.get("type")=="hd_stream" and f["frame"]["phase"]=="content")
chk("云端思考完整", th == "先看需求再想实现", th)
chk("云端正文完整", ct == "方案是这样写。", ct)
ai = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("云端结果落盘且带思考", ai and ai[-1].get("thinking") == "先看需求再想实现", ai[-1].get("thinking") if ai else None)
chk("模型名记的是 deepseek-flash", ai and ai[-1].get("model") == "deepseek-flash", ai[-1].get("model") if ai else None)
chk("发起人正确", ai and ai[-1].get("by") == "张三", ai[-1].get("by") if ai else None)

print("\n[2] 云端但没配 Key")
M._ds_key = lambda h="": ""
frames.clear()
M._hive_run_model_now(gid, "随便", {}, "hd_ds2", "张三", "u_zhangsan")
ai2 = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("提示要配 DeepSeek Key", "DeepSeek Key" in (ai2[-1].get("text") or ""), (ai2[-1].get("text") or "")[:80])

print("\n[3] 本地 Ollama 通道仍然正常")
M._ds_key = lambda h="": "sk-fake"
M.config.load_config = lambda: {"default_model": "qwen3:8b"}
class FR:
    def iter_lines(self, chunk_size=64, decode_unicode=False):
        for t in ["本地想", "本地写"]:
            yield json.dumps({"message": {"thinking": t, "content": ""}}, ensure_ascii=False).encode()
        yield json.dumps({"message": {"content": "本地正文"}, "done": True}).encode()
    def close(self): pass
M.client.chat = lambda *a, **k: FR()
frames.clear()
M._hive_run_model_now(gid, "本地跑", {}, "hd_local1", "李四", "u_lisi")
th3 = "".join(f["frame"]["delta"] for f in frames if f.get("type")=="hd_stream" and f["frame"]["phase"]=="thinking")
chk("本地通道思考推送", th3 == "本地想本地写", th3)
ai3 = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("本地结果落盘", "本地正文" in (ai3[-1].get("text") or ""), ai3[-1].get("text"))

print("\n[4] 云端流式报错也要有反馈")
def boom(*a, **k):
    raise RuntimeError("余额不足")
    yield
deepseek_client.chat_stream = boom
M.config.load_config = lambda: {"default_model": "deepseek-flash"}
frames.clear()
M._hive_run_model_now(gid, "会失败", {}, "hd_ds3", "张三", "u_zhangsan")
ai4 = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("云端报错写进消息", "余额不足" in (ai4[-1].get("text") or ""), (ai4[-1].get("text") or "")[:80])

shutil.rmtree(hive._gdir(gid), ignore_errors=True)
print("\n测试群已清理:", not os.path.exists(hive._gdir(gid)))
print("\n" + "="*50)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL: print("失败项:", FAIL)
