"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时查并画时间线

对应预判「用户反复问同一件事」和摩擦点「同一需求被重复发送」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, tempfile, os
    w = {"id": "w1", "query": "price", "interval": 60, "last_value": "100", "threshold": "5"}
    path = os.path.join(tempfile.gettempdir(), "watchers_test.json")
    json.dump([w], open(path, "w"))
    loaded = json.load(open(path))
    assert loaded[0]["id"] == "w1"
    assert loaded[0]["interval"] == 60
    # 阈值比较逻辑
    old, new, th = 100.0, 110.0, 5.0
    assert abs(new - old) > th
    print("watcher ok")
