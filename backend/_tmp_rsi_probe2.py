import sys

from backend import code_audit as CA

r1 = CA.audit()
hs = [f for f in (r1.get("findings") or []) if f.get("level") == "high"]
out = ["run1 high=%d total=%d" % (len(hs), len(r1.get("findings") or []))]
for f in hs[:10]:
    out.append("%s %s:%s %s" % (f["level"], f["file"], f["line"], f["msg"].encode("utf-8").decode("utf-8")))

r2 = CA.audit()
hs2 = [f for f in (r2.get("findings") or []) if f.get("level") == "high"]
out.append("run2 high=%d" % len(hs2))
for f in hs2[:10]:
    out.append("R2 %s %s:%s" % (f["level"], f["file"], f["line"]))

sys.stdout.write("\n".join(out) + "\n")
