# -*- coding: utf-8 -*-
"""Does the cloud model actually produce long output when max_tokens is raised?

Before: hive @hd hard-coded max_tokens=2400 (~1200-1500 Chinese chars).
Now: default 8000.  This test proves the model can and does go past 3000
chars, which is the threshold for auto-attaching a .md file.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import config, keyvault          # noqa: E402
from backend import deepseek_client           # noqa: E402

key = os.environ.get("DEEPSEEK_API_KEY") or keyvault.get_key()
if not key:
    print("SKIP: no cloud key available")
    sys.exit(2)
print("key: present (%d chars)" % len(key))

cfg = config.load_config() or {}
model = cfg.get("default_model") or "deepseek-flash"
print("model:", model)

prompt = ("请写一篇完整的文章，主题是「为什么大型软件项目需要分层架构」。"
          "要求：结构完整，包含引言、至少 6 个分论点（每个分论点要有解释和例子）、"
          "以及结论。全文不少于 3500 个汉字，不要只给提纲，不要写「其余同理」。")

for cap in (2400, 8000):
    t0 = time.time()
    n_think = 0
    n_text = 0
    tail = ""
    err = ""
    try:
        for kind, val in deepseek_client.chat_stream(
                [{"role": "user", "content": prompt}],
                model=model, api_key=key, temperature=0.3,
                max_tokens=cap, thinking=False):
            if not val:
                continue
            if kind == "thinking":
                n_think += len(val)
            else:
                n_text += len(val)
                tail = (tail + val)[-40:]
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    dur = time.time() - t0
    print("cap=%-5d chars=%-5d think=%-5d dur=%.1fs err=%s"
          % (cap, n_text, n_think, dur, err[:100]))
    print("   tail: %r" % tail)
