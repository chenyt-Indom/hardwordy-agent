"""临时：删掉自己和一个校验脚本。"""
import os

here = os.path.dirname(os.path.abspath(__file__))
for name in ("_check_run_syntax.py", "_cleanup_tmp.py"):
    p = os.path.join(here, name)
    try:
        if os.path.exists(p):
            os.remove(p)
            print("removed", name)
    except Exception as e:
        print("skip", name, e)
