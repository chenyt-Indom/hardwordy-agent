import json
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
d = json.load(open('data/self_iterate/plans.json', encoding='utf-8'))
for it in d['items']:
    for a in (it.get('actions') or []):
        if a.get('id') in ('f5', 'f4') and it.get('id') == '20261117_162133':
            print(json.dumps(a, ensure_ascii=False, indent=1)[:4000])
