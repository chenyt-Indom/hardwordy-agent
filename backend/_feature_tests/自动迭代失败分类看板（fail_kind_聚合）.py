"""新功能验收断言：自动迭代失败分类看板（fail_kind 聚合）

自动迭代失败原因不透明，用户反复追问

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, json, tempfile, os
    from backend import actlog

    fake_events = (
        [{'fail_kind': 'patch_mismatch'} for _ in range(3)]
        + [{'fail_kind': 'syntax'} for _ in range(2)]
        + [{'fail_kind': 'timeout'} for _ in range(1)]
        + [{'ok': True} for _ in range(5)]
    )
    tmp = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    json.dump({'events': fake_events}, tmp)
    tmp.close()
    try:
        stats = actlog.fail_stats(window=100, path=tmp.name)
        assert stats.get('patch_mismatch') == 3, stats
        assert stats.get('syntax') == 2, stats
        assert stats.get('timeout') == 1, stats
        assert 'ok' not in stats, stats
        keys = list(stats.keys())
        assert keys == sorted(keys, key=lambda k: -stats[k]), keys
        stats2 = actlog.fail_stats(window=4, path=tmp.name)
        assert sum(stats2.values()) <= 4, stats2
    finally:
        os.unlink(tmp.name)
    print('fail_stats assertions passed')
