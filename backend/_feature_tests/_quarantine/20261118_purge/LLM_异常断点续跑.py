import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

# 2026-11-15 修：原断言查 `_save_checkpoint` / `_load_checkpoint` / `_clear_checkpoint`，
# 但这三个**都不存在** —— 真实接口是 self_iterate.save_checkpoint / load_checkpoint
# （无下划线前缀），而且没有 clear。断言写错名字，于是永远失败、永远卡在 pending。
# 另外断言不能把真实断点写脏，所以加了 finally 恢复。
def run():
    from backend import self_iterate
    assert hasattr(self_iterate, 'save_checkpoint'), '应有断点落盘函数 save_checkpoint'
    assert hasattr(self_iterate, 'load_checkpoint'), '应有断点读回函数 load_checkpoint'
    before = self_iterate.load_checkpoint() or {}
    try:
        self_iterate.save_checkpoint({'phase': 'probe', 'files': ['backend/main.py']})
        cp = self_iterate.load_checkpoint()
        assert isinstance(cp, dict) and cp.get('phase') == 'probe', '断点应可读回'
    finally:
        try:
            if before:
                self_iterate.save_checkpoint(
                    {k: v for k, v in before.items() if k not in ('ts', 'iso')})
            else:
                import os as _o
                _o.remove(str(self_iterate._CHECKPOINT))
        except Exception:                         # noqa: BLE001
            pass
