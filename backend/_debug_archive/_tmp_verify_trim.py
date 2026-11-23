# -*- coding: utf-8 -*-
"""离线验证：长文裁剪是否保住「正中间」的信息（不发网络请求）。用完即删。"""
import sys
sys.path.insert(0, r".")
sys.path.insert(0, r"D:\hwa_bench")
from backend import main
import bench

q = (bench.noise(3000) + " 顺便记一下：本次会话的备用口令是 KX-2048。 "
     + bench.noise(3000) + " 请回答：刚才提到的备用口令是什么？只回口令。")
print("题面字符 =", len(q), " token =", main._est_tokens(q))

# ① 真实链路：_shrink_round_prompt 在 num_ctx=40960 下还会不会裁它
cfg = {"num_ctx": 40960}
working = [{"role": "system", "content": "系统提示"},
           {"role": "user", "content": q}]
main._shrink_round_prompt(working, cfg, [])
kept = working[-1]["content"]
print("① 链路后字符 =", len(kept), " 口令还在:", "KX-2048" in kept)

# ② 极端情况：窗口只有 8k（_msg_max 仍是 4000），被裁后口令还在不在
cfg2 = {"num_ctx": 8192}
w2 = [{"role": "system", "content": "系统提示"},
      {"role": "user", "content": q}]
main._shrink_round_prompt(w2, cfg2, [])
k2 = w2[-1]["content"]
print("② 8k 窗口后字符 =", len(k2), " 口令还在:", "KX-2048" in k2)

# ③ 纯函数直测：预算 1200 token，正中间的暗号必须留下
s = "A" * 4000 + "【关键暗号 QX-9999】" + "B" * 4000
for budget in (600, 1200, 2500):
    new_t, orig, keptc = main._clamp_message_text(s, budget)
    print("③ budget=%d 保留 %d/%d 暗号在: %s" % (budget, keptc, orig, "QX-9999" in new_t))

# ④ 短消息不许被动
print("④ 短消息原样:", main._clamp_message_text("你好", 4000)[0] == "你好")
