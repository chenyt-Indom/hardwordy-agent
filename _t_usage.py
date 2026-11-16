# -*- coding: utf-8 -*-
"""临时探针：验证积压闸门真实生效值。用完即删。"""
import sys, os
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend import selfloop as sl, self_iterate as si

d = sl.cfg()
print("backlog_gate 生效值:", d.get("backlog_gate"))
print("min_gap_sec 生效值:", d.get("min_gap_sec"))
print("daily_plans 生效值:", d.get("daily_plans"))
print("今日已用 plans:", d.get("plans"), "剩余额度:", sl._quota_left(d))

# 关键：_backlog() 真实返回
sl._BACKLOG_CACHE["at"] = 0.0          # 强制重算，绕开缓存
n = sl._backlog()
print("-" * 50)
print("_backlog() 真实返回:", n)
print("闸门会拦住自动触发吗:", n >= int(d.get("backlog_gate") or 0))

# 分开看两个来源
a = si.list_pending_actions() or {}
b = si.list_pending_features() or {}
print("  改进类 total:", a.get("total"))
print("  新功能 total:", b.get("total"))
print("  两者之和:", int(a.get("total") or 0) + int(b.get("total") or 0))

# 模拟一次自动触发（不真开线程，只调 _maybe_think 的闸门分支）
print("-" * 50)
r = sl._maybe_think("探针测试", source="chat", gap=0, sig="")
print("自动触发结果:", r)
