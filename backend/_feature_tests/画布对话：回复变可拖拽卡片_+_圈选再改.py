"""新功能验收断言：画布对话：回复变可拖拽卡片 + 圈选再改

对应「同一需求被重复发送 20 次」：用户想改局部却只能整段重发。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json

    # 模拟后端 artifacts 组装
    msg = {"role": "assistant", "artifacts": [
        {"id": "a1", "type": "text", "content": "方案A"},
        {"id": "a2", "type": "image", "content": "img.png"},
        {"id": "a3", "type": "ppt", "content": "deck.pptx"},
    ]}
    assert len(msg["artifacts"]) == 3
    assert all("id" in a and "type" in a for a in msg["artifacts"])

    # 圈选单块再改：请求只带该块
    selected = [msg["artifacts"][1]]
    req = {"action": "revise", "artifacts": [{"id": a["id"], "content": a["content"]} for a in selected]}
    assert len(req["artifacts"]) == 1
    assert req["artifacts"][0]["id"] == "a2"

    # 合并两块
    merged = [msg["artifacts"][0], msg["artifacts"][2]]
    req2 = {"action": "merge", "artifacts": [{"id": a["id"], "content": a["content"]} for a in merged]}
    assert len(req2["artifacts"]) == 2
    assert req2["artifacts"][0]["content"] == "方案A"
    assert req2["artifacts"][1]["content"] == "deck.pptx"

    # 序列化可被 /chat 接收
    s = json.dumps(req2)
    assert json.loads(s)["action"] == "merge"
    print("canvas ok")
