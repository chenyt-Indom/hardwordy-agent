import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

# 2026-11-15 修：原断言要求 `workspace.save` 或 `workspace.write` ——
# workspace 是**项目工作区**（建项目/删项目/导出 zip），本来就没有通用 save/write，
# 这个要求本身不成立。改成验「长任务的中间状态真能落盘并读回」这个真实意图。
def run():
    from backend import self_iterate
    assert hasattr(self_iterate, 'save_checkpoint'), '应有断点落盘入口'
    assert hasattr(self_iterate, 'load_checkpoint'), '应有断点读回入口'
    before = self_iterate.load_checkpoint() or {}
    try:
        self_iterate.save_checkpoint({'phase': 'longtask', 'step': 3, 'total': 10})
        cp = self_iterate.load_checkpoint()
        assert cp.get('step') == 3 and cp.get('total') == 10, '中间状态应可完整读回'
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
