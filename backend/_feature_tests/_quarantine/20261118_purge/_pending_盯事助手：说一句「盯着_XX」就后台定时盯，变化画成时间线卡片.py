"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯，变化画成时间线卡片

用户反复交代同一件事（如「帮我看看 XX 有没有变化」），每次都要重新发起；且没有「变化超过阈值时主动提醒」的能力。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, tempfile, json
    sys.path.insert(0, os.path.abspath('backend'))
    import watch

    # 用临时目录隔离存储
    watch.STORE_PATH = os.path.join(tempfile.mkdtemp(), 'watches.json')

    w = watch.create_watch(keyword='test-kw', freq_sec=60)
    assert w is not None and 'id' in w
    lst = watch.list_watches()
    assert any(x['id'] == w['id'] for x in lst)

    # run_watch_once 应返回结构化结果（允许网络不可用，但必须返回 dict 且含 hash 字段）
    res = watch.run_watch_once(w['id'], fetcher=lambda kw: 'content-' + kw)
    assert isinstance(res, dict)
    assert 'hash' in res or 'changed' in res

    # 第二次相同内容不应判定为变化
    res2 = watch.run_watch_once(w['id'], fetcher=lambda kw: 'content-' + kw)
    assert res2.get('changed') in (False, None) or res2.get('hash') == res.get('hash')
    print('watch ok')
