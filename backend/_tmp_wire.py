import os
import re
import sys

sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_be = os.path.abspath('backend')
for mod, fns in [('backend', ['reply_gate']), ('reply_gate', ['format_reply', 'post_filter', 'enforce_conclusion', 'sanitize'])]:
    hit = False
    hits = []
    for fn in fns:
        for f in os.listdir(_be):
            if not f.endswith('.py') or f == mod + '.py':
                continue
            s = open(os.path.join(_be, f), encoding='utf-8', errors='ignore').read()
            if re.search(r'\b' + re.escape(fn) + r'\s*\(', s):
                hits.append((fn, f))
    print('WIRING', mod, fns, 'HIT' if hits else 'MISS', hits[:6])
