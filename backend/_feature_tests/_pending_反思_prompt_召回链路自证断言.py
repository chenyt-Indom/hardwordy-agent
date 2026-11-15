"""新功能验收断言：反思 prompt 召回链路自证断言

无法证明工作过程向量真的进了反思 prompt

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, inspect
    sys.path.insert(0, os.path.abspath('backend'))
    import workvec, self_iterate
    assert hasattr(workvec, 'build_reflect_block'), 'workvec 缺少 build_reflect_block'
    fn = workvec.build_reflect_block
    sig = inspect.signature(fn)
    assert len(sig.parameters) >= 1, 'build_reflect_block 至少需要一个 query 参数'
    block = fn('工作效率低，任务经常延期', 5)
    assert isinstance(block, str), 'build_reflect_block 必须返回字符串'
    assert len(block) > 0, '反思段落不能为空'
    assert ('工作效率' in block) or ('过往任务经验' in block), '反思段落缺少关键词'
    src = inspect.getsource(self_iterate)
    assert 'build_reflect_block' in src, 'self_iterate 未调用 build_reflect_block'
    print('OK')
