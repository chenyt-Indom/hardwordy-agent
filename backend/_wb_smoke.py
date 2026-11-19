# -*- coding: utf-8 -*-
"""临时自测（跑完即删）：工作台纯逻辑。"""
import io
import sys
import zipfile

from backend import canvas as C

fails = []


def ck(name, cond, extra=""):
    if cond:
        print("  OK   " + name)
    else:
        fails.append(name)
        print("  FAIL " + name + ("  " + str(extra) if extra else ""))


it = C.normalize_artifact({"path": "data/a.pptx", "title": "  方案  一版 "})
ck("ppt kind", it["kind"] == "ppt", it["kind"])
ck("title 单行", it["title"] == "方案 一版", it["title"])
ck("id stable", it["id"].startswith("a"), it["id"])
ck("ts", float(it["ts"]) > 0)
ck("status done", it["status"] == "done")
img = C.normalize_artifact({"path": "data/x.png"})
ck("title from path", img["title"] == "x.png", img["title"])
ck("thumb", img["thumb"].startswith("/api/doclib/download?rel="), img["thumb"])
ck("no thumb for doc", C.normalize_artifact({"path": "data/x.docx"})["thumb"] == "")
ck("data uri dropped",
   not C.normalize_artifact(
       {"path": "a.png", "thumb": "data:image/png;base64,AA"})["thumb"].startswith("data:"))
ck("sheet", C.guess_kind("t.xlsx") == "sheet")
ck("other", C.guess_kind("f.zzz") == "other")
ck("none safe", isinstance(C.normalize_artifact(None), dict))

items = []
r1 = C.add_artifact({"path": "data/img/a.png", "title": "狐狸"}, items=items)
ck("add ok", r1["ok"] and r1["count"] == 1, r1)
ck("pure not persisted", r1["persisted"] is False)
ck("input untouched", items == [], items)
r2 = C.add_artifact({"path": "data/a.pptx", "title": "方案.pptx"}, items=r1["items"])
ck("newest first", [x["title"] for x in r2["items"]] == ["方案.pptx", "狐狸"])
r3 = C.add_artifact({"path": "data/img/a.png", "title": "狐狸"}, items=r2["items"])
ck("dedupe", r3["count"] == 2, r3["count"])
ck("dedupe to front", r3["items"][0]["id"] == r1["item"]["id"])
ck("ts kept", r3["items"][0]["ts"] == r1["item"]["ts"])
ck("empty rejected", C.add_artifact({}, items=[])["ok"] is False)

half = C.add_artifact({"path": "data/draft.mp4", "title": "剪辑草稿",
                       "status": "draft"}, items=r3["items"])
lst = C.list_artifacts(items=half["items"])
ck("count", lst["count"] == 3, lst["count"])
ck("unfinished", [x["title"] for x in lst["unfinished"]] == ["剪辑草稿"], lst["unfinished"])

rm = C.remove_artifact(r2["items"][0]["id"], items=r2["items"])
ck("remove ok", rm["ok"] and rm["count"] == 1, rm)
ck("remove right one", [x["title"] for x in rm["items"]] == ["狐狸"])
ck("remove unknown err", C.remove_artifact("a_not_exist", items=r2["items"])["ok"] is False)
ck("remove none err", C.remove_artifact([], items=r2["items"])["ok"] is False)
keep = [dict(x) for x in r2["items"]]
C.remove_artifact("a_not_exist", items=keep)
ck("remove fail keeps input", keep == r2["items"])

ro = C.reorder_artifacts([r2["items"][1]["id"], r2["items"][0]["id"]], items=r2["items"])
ck("reorder", [x["title"] for x in ro["items"]] == ["狐狸", "方案.pptx"],
   [x["title"] for x in ro["items"]])
partial = C.reorder_artifacts([r2["items"][0]["id"]], items=r2["items"])
ck("partial reorder keeps all",
   partial["count"] == 2 and partial["items"][0]["title"] == "方案.pptx",
   [x["title"] for x in partial["items"]])
ck("empty reorder", C.reorder_artifacts([], items=r2["items"])["count"] == 2)

ex = C.export_artifacts(items=r2["items"])
ck("export ok", ex["ok"] is True, ex.get("error"))
ck("zip magic", ex["blob"][:2] == b"PK", ex["blob"][:4])
names = zipfile.ZipFile(io.BytesIO(ex["blob"])).namelist()
ck("manifest md", "清单.md" in names, names)
ck("missing listed", len(ex["missing"]) == 2, ex["missing"])
ck("empty board zip", C.export_artifacts(items=[])["blob"][:2] == b"PK")
ck("real file packed", C.export_artifacts(
    items=[{"path": "backend/canvas.py", "title": "canvas.py"}])["count"] == 1)

ck("safe inside", C.safe_abs("backend/canvas.py") != "")
ck("safe outside", C.safe_abs("../../etc/passwd") == "", C.safe_abs("../../etc/passwd"))
ck("safe abs outside", C.safe_abs("C:/Windows/system32/calc.exe") == "")
ck("safe api rel", C.safe_abs("/api/doclib/download?rel=backend/canvas.py") != "")
evil = C.export_artifacts(items=[{"path": "../../etc/passwd", "title": "坏"}])
ck("evil not packed", len(evil["missing"]) == 1 and b"passwd" not in evil["blob"])

print("FAILS:", fails)
sys.exit(1 if fails else 0)
