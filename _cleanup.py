import os
p = os.path.join(os.getcwd(), "_syntax_check.py")
try:
    os.remove(p)
    print("removed", p)
except Exception as e:
    print("err", e)
print("cwd=", os.getcwd())
