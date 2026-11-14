"""清理本轮临时验证脚本。"""
import os
ROOT = os.path.dirname(os.path.abspath(__file__))
names = ["_qa_same.py", "_qa_final_same.py", "_qa_e2e_same.py",
         "_qa_tool_same.py", "_qa_artifact.py", "_qa_local.py",
         "_qa_local2.py", "_qa_local3.py", "_qa_local4.py", "_qa_local5.py"]
gone = []
for n in names:
    p = os.path.join(ROOT, n)
    try:
        if os.path.isfile(p):
            os.remove(p)
            gone.append(n)
    except Exception as e:
        print("跳过 %s：%s" % (n, e))
print("已删除 %d 个：%s" % (len(gone), ", ".join(gone)))
left = [n for n in names if os.path.isfile(os.path.join(ROOT, n))]
print("剩余：", left or "（无）")
