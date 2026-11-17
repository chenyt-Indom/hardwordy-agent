import glob
import importlib.util
import sys

sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

pats = ['*回复形态*.py', '*回复收尾*.py', '*结论前置*.py', '*空输出*.py']
seen = []
for p in pats:
    for f in glob.glob('backend/_feature_tests/' + p):
        if f not in seen:
            seen.append(f)

for f in seen:
    spec = importlib.util.spec_from_file_location('m_probe', f)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
        m.run()
        print('PASS', f)
    except Exception as e:  # noqa: BLE001
        print('FAIL', f, type(e).__name__, e)
