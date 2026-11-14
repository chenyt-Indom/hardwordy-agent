import subprocess, os, re, sys

root = r"D:\local-multimodal-src"
ok = True

# 1) JS 语法
r = subprocess.run(["node", "--check", os.path.join(root, "frontend", "app.js")],
                   capture_output=True, text=True)
print("[1] node --check rc=", r.returncode, (r.stderr or "").strip()[:300])
ok = ok and r.returncode == 0

# 2) HTML 元素
h = open(os.path.join(root, "frontend", "index.html"), encoding="utf-8").read()
for eid in ["av2Res", "av2MouthBox", "av2Stage", "av2Img", "av2MY", "av2MW"]:
    hit = ('id="%s"' % eid) in h
    print("[2] html #%s: %s" % (eid, hit))
    ok = ok and hit

# 3) CSS
c = open(os.path.join(root, "frontend", "style.css"), encoding="utf-8").read()
for cls in [".av2-mbox", ".av2-mbox span"]:
    hit = cls in c
    print("[3] css %s: %s" % (cls, hit))
    ok = ok and hit
print("[3b] css 括号配平:", c.count("{"), c.count("}"))
ok = ok and c.count("{") == c.count("}")

# 4) JS 关键逻辑
j = open(os.path.join(root, "frontend", "app.js"), encoding="utf-8").read()
for kw in ["paintMouthBox", "mouthState", "base.max_side", "avatar2_res"]:
    hit = kw in j
    print("[4] js %s: %s" % (kw, hit))
    ok = ok and hit

# 5) 后端编译
r2 = subprocess.run([sys.executable, "-m", "py_compile",
                     os.path.join(root, "backend", "avatar_lipsync.py"),
                     os.path.join(root, "backend", "main.py")],
                    capture_output=True, text=True)
print("[5] py_compile rc=", r2.returncode, (r2.stderr or "").strip()[:300])
ok = ok and r2.returncode == 0

print("ALL_OK" if ok else "HAS_FAIL")
