import subprocess, sys, os
for f in ["frontend/app.js"]:
    p = os.path.join(os.getcwd(), f)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    print(f, "rc=", r.returncode)
    print((r.stdout or "") + (r.stderr or ""))
