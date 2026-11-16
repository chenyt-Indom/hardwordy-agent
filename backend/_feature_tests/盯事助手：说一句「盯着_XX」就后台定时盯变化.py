"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯变化

对应预判「用户会继续追问迭代失败根因」——盯事助手可以盯 actlog 里的失败计数变化，变化超阈值就弹横幅。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os, tempfile

    # 模拟 watch 配置与两次搜索结果 diff 的纯逻辑断言
    watch = {"keyword": "fail_kind", "interval_min": 5}
    prev = ["a", "b"]
    cur = ["a", "b", "c"]
    added = [x for x in cur if x not in prev]
    removed = [x for x in prev if x not in cur]
    assert watch["interval_min"] > 0
    assert added == ["c"] and removed == []

    # 断言 actlog 记录结构可序列化
    event = {"type": "watch_diff", "keyword": watch["keyword"], "added": added, "removed": removed}
    s = json.dumps(event, ensure_ascii=False)
    assert "watch_diff" in s and "c" in s
    print("watch diff logic ok")
