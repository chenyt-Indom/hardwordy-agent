import json
import sys

from backend import main as M

r = M.selfiter_rsi(limit=20)
out = {"keys": sorted(r.keys()),
       "ok": r.get("ok"),
       "index_score": (r.get("index") or {}).get("score"),
       "index_ok": (r.get("index") or {}).get("ok"),
       "speed_score": (r.get("speed") or {}).get("score"),
       "speed_ok": (r.get("speed") or {}).get("ok"),
       "trend_n": (r.get("trend") or {}).get("n"),
       "trend_shape": (r.get("trend") or {}).get("shape"),
       "n_points": len(((r.get("trend") or {}).get("points") or [])),
       "weakest_ok": (r.get("weakest") or {}).get("ok"),
       "weakest_name": (r.get("weakest") or {}).get("name"),
       "line_len": len(r.get("line") or "")}
sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
