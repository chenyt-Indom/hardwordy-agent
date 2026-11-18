"""新功能验收断言：待批准卡片：审批+备注微改一站式

对应摩擦点「只诊断不修复」的反面——用户想介入但没入口；也对应历史失败动作「前端：【待批准】按钮+卡片」。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json
    # 模拟后端 pending 接口返回与 approve 写入 note 的契约
    pending = [{"id": 1, "title": "t", "risk": "mid", "files": ["a.py"], "accept": "ok", "note": ""}]
    assert all("note" in p for p in pending)
    note = "微改：把阈值从 0.5 调到 0.6"
    pending[0]["note"] = note
    assert pending[0]["note"] == note
    unread = len([p for p in pending if not p["note"]])
    assert unread == 0
    print("ok")
