# -*- coding: utf-8 -*-
"""临时自测：backend.tts 三个引擎路径 + 长文分段。跑完即删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend import tts  # noqa: E402

print("=== 1. 文本清洗 ===")
md = """## 标题
**重点**：这是正文，带 `代码` 和 [链接](https://x.com)。
```python
print("这段不该被念")
```
| 列A | 列B |
|---|---|
"""
print(repr(tts.clean_text(md)))

print("=== 2. 短文本 edge ===")
r = tts.synth("你好，这是语音合成自测。四次浪潮里的中国坐标。", filename="selftest_short")
print({k: r.get(k) for k in ("ok", "engine", "name", "bytes", "chars", "est_seconds", "warnings", "error")})

print("=== 3. 长文本（1500+ 字）分段拼接 ===")
long_text = "第一次工业革命，机器代替了手工。中国那时候还在闭关自守。\n" * 60
print("字数:", len(tts.clean_text(long_text)), "分段数:", len(tts.split_text(tts.clean_text(long_text))))
r2 = tts.synth(long_text, filename="selftest_long", rate=10)
print({k: r2.get(k) for k in ("ok", "engine", "name", "bytes", "chars", "est_seconds", "warnings", "error")})

print("=== 4. SAPI 离线 ===")
r3 = tts.synth("这是离线语音测试。", voice="sapi:Microsoft Huihui Desktop",
               engine="sapi", filename="selftest_sapi")
print({k: r3.get(k) for k in ("ok", "engine", "name", "bytes", "chars", "warnings", "error")})

print("=== 5. 音色清单 ===")
vs = tts.voice_list()
print("数量:", len(vs), "| 前两个:", vs[0]["id"], vs[1]["id"])
print("=== 6. 文件落地 ===")
for n in ("selftest_short.mp3", "selftest_long.mp3", "selftest_sapi.wav"):
    p = os.path.join(tts.OUT_DIR, n)
    print(n, os.path.getsize(p) if os.path.exists(p) else "MISSING")
