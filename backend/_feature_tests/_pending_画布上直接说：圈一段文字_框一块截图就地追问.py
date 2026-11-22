"""新功能验收断言：画布上直接说：圈一段文字/框一块截图就地追问

对应摩擦点「过程过长/碎步」38 次——用户要花很多字描述「我说的那段是哪个」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import base64, io
    from PIL import Image
    from backend import canvas

    img = Image.new('RGB', (200, 120), (255, 0, 0))
    buf = io.BytesIO(); img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode()

    res = canvas.annotate_region(b64, {'x': 10, 'y': 20, 'w': 50, 'h': 30}, '这是什么颜色')
    assert isinstance(res, dict), 'annotate_region 应返回 dict'
    assert 'answer' in res and 'region' in res, '返回缺 answer/region'

    crop = res.get('crop_b64') or res.get('image_b64')
    assert crop, '应返回裁剪子图'
    cimg = Image.open(io.BytesIO(base64.b64decode(crop)))
    assert cimg.size == (50, 30), f'裁剪尺寸应为 50x30，实际 {cimg.size}'

    # 越界 region 不抛异常且被夹到边界
    res2 = canvas.annotate_region(b64, {'x': 180, 'y': 100, 'w': 100, 'h': 100}, 'q')
    crop2 = res2.get('crop_b64') or res2.get('image_b64')
    c2 = Image.open(io.BytesIO(base64.b64decode(crop2)))
    assert c2.size[0] <= 200 and c2.size[1] <= 120, '越界裁剪未夹到边界'
    print('ok')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['canvas']),
    ]
    _bad = []
    for _mod, _fns in _pairs:
        if not _wo.path.isfile(_wo.path.join(_be, _mod + '.py')):
            continue
        _hit = False
        for _fn in _fns:
            for _f in _wo.listdir(_be):
                if not _f.endswith('.py') or _f == _mod + '.py':
                    continue
                try:
                    _s = open(_wo.path.join(_be, _f), encoding='utf-8', errors='ignore').read()
                except Exception:
                    continue
                if _wr.search(r'\b' + _wr.escape(_fn) + r'\s*\(', _s):
                    _hit = True
                    break
            if _hit:
                break
        if not _hit:
            _bad.append('%s 里的 %s 没有被任何其他模块调用（只 import 不算接线）'
                        % (_mod, '/'.join(_fns)))
    assert not _bad, '功能未接入主链路：' + '；'.join(_bad)

def _with_wiring(fn):
    def _wrapped():
        fn()
        _wiring_check()
    return _wrapped

# ⚠️ 用**装饰器包装**而不是再写一个 def run：
#   2026-11-15 实测，重定义 run 会被静态自检判成「同名函数定义两次」（mid），
#   而且模型自己若也写了 _orig_run 会互相覆盖。包装法只保留模型那一个 run。
if callable(globals().get('run')):
    run = _with_wiring(run)
else:
    def run():
        _wiring_check()
