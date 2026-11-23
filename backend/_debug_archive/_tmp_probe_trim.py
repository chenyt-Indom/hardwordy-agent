# -*- coding: utf-8 -*-
"""临时诊断：离线验证 ctx-01 的 6000 字题面有没有被后端裁掉。用完即删。
不发任何网络请求，只调纯函数。"""
import sys
sys.path.insert(0, r".")
sys.path.insert(0, r"D:\hwa_bench")
from backend import main, config
import bench

q = (bench.noise(3000) + " 顺便记一下：本次会话的备用口令是 KX-2048。 "
     + bench.noise(3000) + " 请回答：刚才提到的备用口令是什么？只回口令。")
print("题面字符数 =", len(q), " 估算 token =", main._est_tokens(q))

cfg = config.load_config()
cfg["num_ctx"] = 362496
cfg["max_tokens"] = 393216
cfg["web_enabled"] = False

msgs = [{"role": "user", "content": q}]
kept, dropped, info = main._trim_history_to_budget(msgs, "系统提示", [], cfg)
print("保留消息数 =", len(kept), " 丢弃 =", len(dropped), " 截断信息 =", info)
if kept:
    c = kept[0].get("content") or ""
    print("保留内容字符数 =", len(c))
    print("口令还在吗:", "KX-2048" in c)

# 再看预算细节
overhead = main._est_tokens("系统提示")
print("system overhead 估算 =", overhead)
