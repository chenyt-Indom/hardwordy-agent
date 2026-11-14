import subprocess, sys, os
r = subprocess.run(["node", "--check", "frontend/app.js"],
                   capture_output=True, text=True, shell=True)
print("node --check app.js rc=", r.returncode)
print((r.stdout or "")[-500:])
print((r.stderr or "")[-1500:])
r2 = subprocess.run([sys.executable, "-m", "py_compile", "backend/main.py"],
                    capture_output=True, text=True)
print("py_compile main.py rc=", r2.returncode)
print((r2.stderr or "")[-1500:])
# 额外检查：hdAddCode / codeRowsHtml / codeChangesHtml / bindCodeChanges 都有定义
src = open("frontend/app.js", encoding="utf-8").read()
for fn in ["hdAddCode", "codeRowsHtml", "codeChangesHtml", "bindCodeChanges"]:
    print(fn, "定义次数=", src.count("function " + fn + "("),
          "调用次数=", src.count(fn + "("))
