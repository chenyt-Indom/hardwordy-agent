# -*- coding: utf-8 -*-
"""临时诊断：查当前上下文/生成配置，判断 ctx-01 是配置问题还是能力问题。用完即删。"""
import sys
sys.path.insert(0, r".")
from backend import perf, config

cfg = config.load_config()
print("default_model =", cfg.get("default_model"))
print("num_ctx(config) =", cfg.get("num_ctx"))
print("max_tokens(config) =", cfg.get("max_tokens"))
try:
    pf = perf.get(with_detect=False)
    print("perf.num_ctx =", pf.get("num_ctx"))
    print("perf.max_tokens =", pf.get("max_tokens"))
    print("perf keys =", sorted(pf.keys()))
except Exception as e:
    print("perf 读取失败:", type(e).__name__, e)

# ctx-01 的题面有多大
sys.path.insert(0, r"D:\hwa_bench")
import bench
q = (bench.noise(3000) + " 顺便记一下：本次会话的备用口令是 KX-2048。 "
     + bench.noise(3000) + " 请回答：刚才提到的备用口令是什么？只回口令。")
print("ctx-01 题面字符数 =", len(q))
print("其中汉字噪声约 =", 6000)
