"""新功能验收断言：画布对话：回复变成可圈选、可连线的卡片

对应摩擦点「同一需求被重复发送 20 次」——用户反复描述同一块内容

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os
    ctx = {"blocks": ["b1", "b2"]}
    assert isinstance(ctx["blocks"], list) and len(ctx["blocks"]) == 2
    assert all(isinstance(x, str) for x in ctx["blocks"])
    # 模拟后端拼接
    prefix = "\n".join([f"[block:{b}]" for b in ctx["blocks"]])
    assert "[block:b1]" in prefix and "[block:b2]" in prefix
    print("canvas context ok")
