# -*- coding: utf-8 -*-
"""以服务真实方式（项目根 + 包导入）验证 3D 模块能否导入。"""
import sys
import traceback

print("cwd:", __import__("os").getcwd())
try:
    from backend import avatar3d, face3d
    print("import OK")
    print("face3d.available():", face3d.available())
    print("avatar3d.render exists:", hasattr(avatar3d, "render"))
    print("A2:", avatar3d.A2.__name__)
except Exception:
    traceback.print_exc()
    sys.exit(1)
