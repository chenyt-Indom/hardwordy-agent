"""临时探针：打印静态评分明细（用完即删）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_audit as CA  # noqa: E402


def main():
    a = CA.audit() or {}
    a = CA.audit() or {}
    out = {"counts": a.get("counts"), "counts_scored": a.get("counts_scored")}
    finds = []
    for f in a.get("findings") or []:
        finds.append(f)
    out["findings"] = finds
    try:
        import iterloop as IL
        s = IL.static_part()
        out["static"] = {k: v for k, v in (s or {}).items()
                         if k not in ("findings", "keys")}
    except Exception as e:                     # noqa: BLE001
        out["static_err"] = repr(e)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_score_probe.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    sys.stderr.write("WROTE %s\n" % p)


main()
