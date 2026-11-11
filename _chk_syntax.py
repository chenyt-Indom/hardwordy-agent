import subprocess, os, sys
r = subprocess.run(["node", "--check", "frontend/app.js"], capture_output=True, text=True)
print("cwd:", os.getcwd())
print("rc:", r.returncode)
print(r.stdout[-1500:])
print(r.stderr[-3000:])
