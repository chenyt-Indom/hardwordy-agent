import os, subprocess, sys

root = os.getcwd()
print("CWD:", root)

for f in ["frontend/app.js", "frontend/livecard.js"]:
    r = subprocess.run(["node", "--check", f], capture_output=True, text=True)
    print(f, "rc=", r.returncode, (r.stderr or "").strip()[:500])

for f in ["frontend/style.css", "frontend/index.html", "frontend/app.js"]:
    if os.path.exists(f):
        n = sum(1 for _ in open(f, encoding="utf-8", errors="ignore"))
        print(f, "lines=", n)
    else:
        print(f, "MISSING")

# 关键结构核对：思考节点是否已与正文节点平级
src = open("frontend/app.js", encoding="utf-8", errors="ignore").read()
checks = [
    ('hd-think-card', '思考节点 class'),
    ('thinkNode._home = node', '归位锚点'),
    ('hvDock(thinkNode)', '只浮思考节点'),
    ('hdCards[k].thinkNode', '遍历用 thinkNode'),
    ('c.thinkNode', 'c 对象带 thinkNode'),
    ('home.parentNode.insertBefore(node, home)', '归位插回原位'),
]
for k, label in checks:
    print(("OK  " if k in src else "FAIL"), label, "|", k)

# 旧写法必须消失
bad = ['hdCards[k] && hdCards[k].node', 'hvDock(node);']
for b in bad:
    print(("FAIL(残留)" if b in src else "OK(已清除)"), b)
