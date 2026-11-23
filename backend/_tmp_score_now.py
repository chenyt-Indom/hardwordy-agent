"""临时探针：跑一次完整评分并落 json（用完即删）。"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import iterloop as IL  # noqa: E402


def main():
    sc = IL.score(full=True) or {}
    keep = {
        "total": sc.get("total"),
        "quality": sc.get("quality"),
        "ability": (sc.get("ability") or {}).get("score"),
        "static": {k: (sc.get("static") or {}).get(k)
                   for k in ("score", "max", "high", "mid", "info")},
        "static_findings": (sc.get("static") or {}).get("findings"),
        "regress": {k: (sc.get("regress") or {}).get(k)
                    for k in ("score", "max", "passed", "failed", "failed_items")},
        "func": {k: (sc.get("func") or {}).get(k) for k in ("score", "max")},
    }
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_score_now.json")
    with io.open(p, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=1)
    sys.stderr.write("WROTE %s\n" % p)


main()
