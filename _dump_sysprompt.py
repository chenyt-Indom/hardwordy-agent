# -*- coding: utf-8 -*-
"""临时脚本：导出 _SystemPrompt.build() 的真实拼装结果。
用户要「完整看系统提示词原文」——它就在本机源码里拼装，这里把真实产出落成文件。
跑完即可删除本脚本。
"""
import sys, os, io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import _SystemPrompt  # noqa: E402

variants = []

def try_build(name, **kw):
    try:
        txt = _SystemPrompt.build("你好", **kw)
        if isinstance(txt, (list, tuple)):
            txt = "\n".join(str(x) for x in txt)
        variants.append((name, str(txt)))
        return True
    except Exception as e:
        variants.append((name, "[构建失败] %s: %s" % (type(e).__name__, e)))
        return False

try_build("默认（无会话/无文档）")
try_build("带会话名", session="default")
try_build("无工具模式", no_tools=True)

out = []
for name, txt in variants:
    out.append("=" * 70)
    out.append("【变体】%s   （长度 %d 字符）" % (name, len(txt)))
    out.append("=" * 70)
    out.append(txt)
    out.append("")

body = "\n".join(out)
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sysprompt_dump.txt")
with io.open(path, "w", encoding="utf-8") as f:
    f.write(body)

print("已写入：", path)
for name, txt in variants:
    print("  · %s -> %d 字符" % (name, len(txt)))
