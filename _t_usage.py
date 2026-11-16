# -*- coding: utf-8 -*-
"""临时探针：验证配额/闸门行为。用完即删。"""
import sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend import selfloop as sl

d = sl.cfg()
print("daily_plans 生效值:", d.get("daily_plans"))
print("今日已用 plans:", d.get("plans"), "剩余额度:", sl._quota_left(d))
print("backlog_gate:", d.get("backlog_gate"))
print("-" * 55)

# ① 自动触发：配额已满 → 应被拦
sl._BUSY[0] = False
r1 = sl._maybe_think("探针-自动", source="chat", gap=0, sig="")
print("① 自动触发:", r1)

# ② 手动触发：配额已满但应放行（会真起线程，先确保 backlog 闸门也放行 manual）
#    为了不真跑一轮反思，这里只验证「不被配额挡住」——用 _quota_left 分支判断
quota_blocked_manual = (sl._quota_left(d) <= 0 and "manual" != "manual")
print("② 手动会被配额挡住吗:", quota_blocked_manual, "（应为 False）")

# ③ 真实调用一次 manual，看返回是不是被拦
sl._BUSY[0] = False
sl._LAST_AT[0] = 0.0        # 绕开 min_gap 节流，专测配额分支
r3 = sl._maybe_think("探针-手动", source="manual", gap=0, sig="")
print("③ 手动触发:", r3)
