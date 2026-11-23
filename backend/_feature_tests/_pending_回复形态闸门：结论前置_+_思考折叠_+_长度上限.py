"""新功能验收断言：回复形态闸门：结论前置 + 思考折叠 + 长度上限

对应摩擦点「回复被截断」「过程过长/碎步」「只诊断不修复」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    from backend.main import normalize_reply

    # 1) exec 模式首段应 <= 60 字且保留结论
    long_first = '这是一段非常长的过程描述' * 20 + '\n\n结论：成功，耗时 3 秒。'
    out = normalize_reply(long_first, kind='exec')
    first_para = out.split('\n')[0]
    assert len(first_para) <= 60, first_para
    assert ('成功' in out) or ('失败' in out) or any(c.isdigit() for c in out)

    # 2) 思考块折叠
    with_think = '结论：完成。\n思考：先查日志再定位。\n补充说明。'
    out2 = normalize_reply(with_think, kind='exec')
    assert '<details>' in out2 and '</details>' in out2

    # 3) 超长分段且结论在前
    huge = '结论：成功。\n' + ('段落内容。' * 2000)
    out3 = normalize_reply(huge, kind='exec')
    assert out3.startswith('结论') or '结论' in out3.split('\n')[0]
    assert len(out3.split('\n')) > 1

    # 4) 短回复不被破坏
    short = '结论：成功。'
    assert normalize_reply(short, kind='exec').strip().startswith('结论')
    print('normalize ok')
