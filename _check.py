"""收尾检查：编译 + 前端语法 + 结构配平。"""
import os, sys, subprocess, py_compile

ROOT = os.path.dirname(os.path.abspath(__file__))
ok = True

for f in ["backend/avatar25d.py", "backend/avatar_lipsync.py", "backend/main.py"]:
    p = os.path.join(ROOT, f)
    try:
        py_compile.compile(p, doraise=True)
        print(f"[OK] {f}")
    except Exception as e:
        ok = False
        print(f"[FAIL] {f}: {e}")

# 前端语法
js = os.path.join(ROOT, "frontend", "app.js")
r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
print(f"[{'OK' if r.returncode == 0 else 'FAIL'}] app.js node --check rc={r.returncode}")
if r.returncode != 0:
    ok = False
    print(r.stderr[:600])

# HTML / CSS 配平
html = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
dv = html.count("<div") - html.count("</div>")
cb = css.count("{") - css.count("}")
print(f"[{'OK' if dv == 0 else 'FAIL'}] index.html div 配平 {html.count('<div')}/{html.count('</div>')} (差 {dv})")
print(f"[{'OK' if cb == 0 else 'FAIL'}] style.css 花括号 {css.count('{')}/{css.count('}')} (差 {cb})")
if dv or cb:
    ok = False

# 关键改动是否在位
checks = [
    ("avatar25d.py", "_head_region", "头部区域函数"),
    ("avatar25d.py", "zn_out = cv2.GaussianBlur", "深度场平滑"),
    ("avatar25d.py", "(x - cx) * cyw", "_shift_of 正向变换"),
    ("avatar25d.py", "math.sin(math.pi * 0.5 * (p / 0.4))", "眨眼 sin 曲线"),
    ("avatar25d.py", "bl = _lp(bl, sec=0.09)", "眨眼低通"),
    ("avatar_lipsync.py", "yaw, pit, tx, ty, rol, scl", "眨眼传 roll/scl"),
    ("main.py", 'body.get("blink", False)', "眨眼默认关"),
]
for f, key, desc in checks:
    src = open(os.path.join(ROOT, "backend", f), encoding="utf-8").read()
    hit = key in src
    print(f"[{'OK' if hit else 'FAIL'}] {desc}")
    if not hit:
        ok = False

idx = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
app = open(js, encoding="utf-8").read()
for key, desc in [('id="av2Blink"', "前端眨眼复选框"),
                  ("avatar2_blink", "眨眼状态持久化"),
                  ("base.blink = !!blChk.checked", "眨眼参数透传")]:
    src = idx if key.startswith('id=') else app
    hit = key in src
    print(f"[{'OK' if hit else 'FAIL'}] {desc}")
    if not hit:
        ok = False

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
