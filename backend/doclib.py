# -*- coding: utf-8 -*-
"""生成文库：模型自己写、自己改的文件的存放地（**和知识库严格分开**）。

为什么要物理隔离（而不是靠"这个文件是谁写的"来判权限）
----------------------------------------------------
- **知识库**（`data/kb`）是**用户自己的资料**，语义是"只读参考"：
  模型只能检索，**绝不能改**。用户把一份重要合同放进去，
  被模型"顺手改一下"是不可接受的。
- **生成文库**（`data/library`）是**模型的产出物**：作文、方案、脚本、表格……
  语义是"可读可写可删" —— 用户明确要求模型能主动写入 / 修改 / 删除这里的文件。

两种用途、两套权限，混在一起迟早出错。**物理分开最省心。**

安全设计
--------
1. `_safe_rel()` 收敛所有路径：挡 `../`、挡绝对路径与盘符，
   最后**再校验一次落点确实在库内** —— 模型一次"手滑"就能写到库外面去，必须挡住。
2. 覆盖写之前**自动备份**到 `_备份/`；删除是**移进 `_回收站/`** 而不是真删。
   模型是自动执行的，必须给用户留后悔的余地。
"""
import os
import re
import json
import time
import shutil

from . import config
from . import doc_extract

LIB_DIR = config.data("data", "library")   # 注意：和图片库 image_library.DIR 是两回事
TRASH_DIR = os.path.join(LIB_DIR, "_回收站")
BACKUP_DIR = os.path.join(LIB_DIR, "_备份")

TEXT_EXTS = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".csv",
             ".html", ".css", ".sql", ".sh", ".bat", ".ps1", ".yml", ".yaml",
             ".ini", ".cfg", ".log", ".java", ".c", ".cpp", ".go", ".rs", ".r"}


def ensure_dir() -> str:
    os.makedirs(LIB_DIR, exist_ok=True)
    return LIB_DIR


def _safe_rel(name: str) -> str:
    """把模型/用户给的文件名收敛成**库内**的安全相对路径。"""
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("文件名不能为空")
    # ⚠️ 带盘符要**明确拒绝**，不能悄悄剥掉。
    # 原来是把 `D:/笔记.md` 的 "D:" 静默删掉、存成库里的 `笔记.md` ——
    # 用户会以为文件写到了 D 盘，实际落在库里且改了名，这种"静默改写输入"
    # 比直接报错危险得多。
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError("不要写盘符（如 D:）—— 只能填生成文库内的相对路径")
    raw = raw.lstrip("/")
    parts = []
    for p in raw.split("/"):
        p = p.strip()
        if not p or p == ".":
            continue
        if p == "..":
            raise ValueError("路径里不允许出现 ..")
        parts.append(re.sub(r'[<>:"|?*\x00-\x1f]', "_", p))
    if not parts:
        raise ValueError("文件名不合法")
    rel = "/".join(parts)
    full = os.path.abspath(os.path.join(LIB_DIR, rel))
    root = os.path.abspath(LIB_DIR) + os.sep
    if not full.startswith(root):
        raise ValueError("路径越界（只能写在生成文库目录里）")
    return rel


def _abs(rel: str) -> str:
    return os.path.join(LIB_DIR, rel.replace("/", os.sep))


def _is_hidden(rel: str) -> bool:
    return any(p.startswith("_") for p in rel.split("/"))


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# =====================================================================
#  列举 / 读取
# =====================================================================
def list_files() -> list:
    """列出文库里的文件（跳过 `_回收站` / `_备份` 这类下划线目录）。"""
    ensure_dir()
    out = []
    for root, dirs, files in os.walk(LIB_DIR):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, LIB_DIR).replace(os.sep, "/")
            if _is_hidden(rel):
                continue
            try:
                st = os.stat(full)
            except Exception:
                continue
            ext = os.path.splitext(fn)[1].lower()
            out.append({"name": fn, "rel": rel, "size": st.st_size,
                        "ext": ext, "mtime": st.st_mtime,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                        "folder": os.path.dirname(rel) or ""})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def read_file(rel: str, limit: int = 200_000) -> dict:
    """读文库里的一个文件（PDF/Word/Excel 也能读，走 doc_extract）。"""
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = _abs(rel)
    if not os.path.exists(full):
        return {"ok": False, "error": "文件不存在：%s" % rel, "missing": True}
    text, note = doc_extract.extract_text(full, limit=limit)
    if not text and note:
        return {"ok": False, "error": note}
    return {"ok": True, "rel": rel, "name": os.path.basename(rel),
            "text": text, "chars": len(text), "note": note}


def tree_text(max_items: int = 200) -> str:
    """给模型看的文库目录树（按文件夹分组）。

    原来是一串平铺的 `- 路径（时间，大小）`：文件名里虽然带着 `文件夹/`，
    但混在几十行里**看不出有哪几个文件夹**，模型自然也不会去"进文件夹"。
    """
    files = list_files()
    if not files:
        return "（生成文库还是空的）"
    groups: dict = {}
    for f in files:
        groups.setdefault(f.get("folder") or "", []).append(f)
    lines = []
    shown = 0
    for folder in sorted(groups, key=lambda x: (x == "", x)):
        items = groups[folder]
        lines.append("【%s】%d 个文件" % (folder or "根目录", len(items)))
        for f in items:
            if shown >= max_items:
                break
            lines.append("  · %s  （%s，%d 字节）"
                         % (f["name"], f["modified"], f["size"]))
            shown += 1
    if len(files) > shown:
        lines.append("…… 还有 %d 个文件" % (len(files) - shown))
    return "\n".join(lines)


# =====================================================================
#  写入 / 备份 / 删除 / 复制
# =====================================================================
def _backup(full: str, rel: str) -> str:
    """覆盖前把原文件拷一份到 `_备份/`（按时间戳分目录，不覆盖旧备份）。"""
    if not os.path.exists(full):
        return ""
    safe = rel.replace("/", "__")
    d = os.path.join(BACKUP_DIR, _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, safe)
        shutil.copy2(full, dst)
        return os.path.relpath(dst, LIB_DIR).replace(os.sep, "/")
    except Exception:
        return ""


# 二进制办公文件的扩展名：**禁止**用文本方式写入（详见 write_file 的说明）
_OFFICE_TEXT_EXTS = (".pptx", ".docx", ".xlsx", ".ppt", ".doc", ".xls")


def write_file(rel: str, text: str, mode: str = "overwrite") -> dict:
    """写文本文件。mode=append 时追加。覆盖前自动备份。

    ⚠️⚠️ **办公文件（.pptx/.docx/.xlsx）一律拒绝**（2026-09-25 修）。
    现象：用户拿到一个"打不开的 PPT" —— 文件只有 1.5KB，里面其实是
    `<p style=...>…</table>` 这样的 **HTML**。
    原因：模型把内容当文本写进了一个 .pptx 名字里，而这里对扩展名**毫不校验**，
    于是静默成功；模型还据此宣称"已完成，共 12 页"（全是幻觉）。
    真产物是**二进制**、必须走 `save_bytes`（见 tools.py 里 make_pptx/docx/xlsx）。
    在这里一刀拦住，模型会收到明确指引，从而改用真正的生成工具。
    """
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if str(rel).lower().endswith(_OFFICE_TEXT_EXTS):
        return {"ok": False, "error": (
            "「%s」是二进制办公文件，不能用文本方式写（写进去的文件打不开）。\n"
            "要做 PPT / Word / Excel 请改用对应工具：\n"
            "  · PPT   → make_pptx(title + slides)\n"
            "  · Word  → make_docx(title + blocks)\n"
            "  · Excel → make_xlsx(sheets)\n"
            "这几个工具会生成真正能打开的文件并存进生成文库。"
            % os.path.basename(str(rel)))}
    ensure_dir()
    full = _abs(rel)
    os.makedirs(os.path.dirname(full) or LIB_DIR, exist_ok=True)
    backup = _backup(full, rel) if (os.path.exists(full) and mode != "append") else ""
    try:
        with open(full, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rel": rel, "chars": len(text or ""),
            "mode": mode, "backup": backup}


def save_bytes(rel: str, data: bytes) -> dict:
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ensure_dir()
    full = _abs(rel)
    os.makedirs(os.path.dirname(full) or LIB_DIR, exist_ok=True)
    backup = _backup(full, rel) if os.path.exists(full) else ""
    try:
        with open(full, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rel": rel, "bytes": len(data), "backup": backup}


def delete_file(rel: str) -> dict:
    """删除 → 实际是**移进 `_回收站/`**（模型是自动执行的，得留后悔的余地）。"""
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = _abs(rel)
    if not os.path.exists(full):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    d = os.path.join(TRASH_DIR, _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, rel.replace("/", "__"))
        shutil.move(full, dst)
        return {"ok": True, "rel": rel,
                "trash": os.path.relpath(dst, LIB_DIR).replace(os.sep, "/")}
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s: %s" % (type(e).__name__, e)}


def copy_file(rel: str, new_rel: str) -> dict:
    try:
        rel, new_rel = _safe_rel(rel), _safe_rel(new_rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    src, dst = _abs(rel), _abs(new_rel)
    if not os.path.exists(src):
        return {"ok": False, "error": "源文件不存在：%s" % rel}
    if os.path.isdir(src):
        return {"ok": False, "error": "只能复制文件，不能复制文件夹：%s" % rel}
    # ⚠️ 不覆盖同名文件。复制本来是"留个后路"的动作，允许静默覆盖的话，
    # 用户输入一个已存在的名字就会把那份文件**直接冲掉** ——
    # 而 write_file 覆盖前是会自动备份的，两者必须同样"不让人丢东西"。
    if os.path.exists(dst):
        return {"ok": False,
                "error": "已存在同名文件：%s（换个名字，或先去删掉它）" % new_rel}
    os.makedirs(os.path.dirname(dst) or LIB_DIR, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        return {"ok": False, "error": "复制失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "from": rel, "to": new_rel}


def backup_all() -> dict:
    """整库快照（用户或模型都可以主动调用）。"""
    ensure_dir()
    d = os.path.join(BACKUP_DIR, "全量-" + _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        n = 0
        for f in list_files():
            src = _abs(f["rel"])
            dst = os.path.join(d, f["rel"].replace("/", "__"))
            shutil.copy2(src, dst)
            n += 1
        return {"ok": True, "count": n,
                "path": os.path.relpath(d, LIB_DIR).replace(os.sep, "/")}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def stats() -> dict:
    files = list_files()
    return {"count": len(files),
            "bytes": sum(f["size"] for f in files),
            "dir": LIB_DIR,
            "latest": files[0]["name"] if files else ""}

def file_path(rel: str) -> str:
    """取库内文件的绝对路径（下载/导出用）。越界会抛 ValueError。"""
    return _abs(_safe_rel(rel))


# =====================================================================
#  全量视图（2026-11-11 重构）
#  ------------------------------------------------------------------
#  用户要求：「生成文库不仅要有生成的文本内容，PPT、Excel、Word 等所有内容
#  以及软件都要有，并且支持前端双击打开，然后还要按不同类别展开与收起」。
#
#  原来面板只扫 data/library 一个目录，于是：
#    · 工作区里做出来的软件（抽奖助手 exe / 打包 zip）根本看不见；
#    · 生成的图片也看不见；
#    · 列表只能按文件夹平铺，文件名一长就糊成一片。
#
#  这里做的是**只读聚合**：几个来源合并成一张带类别的清单，
#  **不动磁盘上的任何文件**（用户明确要求"虚拟分组"）。
#  写/删/改仍然只作用于生成文库本体（lib），别处的文件只读。
# =====================================================================

CATS = [
    ("recent",   "🕒 最近生成"),
    ("doc",      "📄 文档"),
    ("ppt",      "📊 演示文稿"),
    ("sheet",    "📈 表格"),
    ("text",     "📝 文本"),
    ("code",     "💻 代码"),
    ("image",    "🖼 图片 / 视频"),
    ("software", "📦 软件 / 压缩包"),
    ("other",    "📎 其它"),
]
CAT_LABEL = dict(CATS)

# 扩展名 → 类别。视频/音频归到 image 那一组（用户要"图片、视频一起收"）
_CAT_EXT = {
    "doc": {"doc", "docx", "pdf", "rtf", "odt", "wps", "pages", "epub"},
    "ppt": {"ppt", "pptx", "key", "odp"},
    "sheet": {"xls", "xlsx", "csv", "ods", "numbers"},
    "text": {"txt", "md", "markdown", "log", "ini", "cfg", "conf",
             "yaml", "yml", "toml", "env", "properties"},
    "code": {"py", "js", "mjs", "ts", "jsx", "tsx", "html", "htm", "css",
             "json", "xml", "sql", "sh", "bat", "ps1", "java", "c", "cpp",
             "h", "hpp", "go", "rs", "rb", "php", "vue", "ipynb", "lua"},
    "image": {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico",
              "tif", "tiff", "mp4", "mov", "avi", "mkv", "webm", "flv",
              "mp3", "wav", "m4a", "ogg", "flac"},
    "software": {"exe", "msi", "zip", "7z", "rar", "tar", "gz", "bz2",
                 "apk", "app", "dmg", "iso", "whl", "jar"},
}
_EXT_CAT = {}
for _c, _es in _CAT_EXT.items():
    for _e in _es:
        _EXT_CAT[_e] = _c

# 聚合时跳过的目录：缓存、版本库、构建产物、回收站 —— 否则工作区里
# 一个 .git 就能刷出几千条无关文件，面板直接卡死。
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv", "env",
              "_回收站", "_备份", "build", "dist", ".idea", ".vscode",
              "site-packages", ".pytest_cache", ".mypy_cache", "obj", "bin"}
_MAX_FILE_BYTES = 300 * 1024 * 1024      # 单文件上限，超过的多半是镜像/模型，不该进列表


def _cat_of(ext: str) -> str:
    return _EXT_CAT.get(str(ext or "").lower().lstrip("."), "other")


def _mk(src: str, rel: str, name: str, size: int, mtime: float,
        folder: str = "", project: str = "") -> dict:
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return {
        "id": "%s|%s" % (src, rel),
        "src": src,
        "rel": rel,
        "name": name,
        "ext": ext,
        "cat": _cat_of(ext),
        "folder": folder or "",
        "project": project or "",
        "size": int(size),
        "mtime": float(mtime),
        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
        # 只有生成文库本体的文件可以编辑 / 删除，别处一律只读
        "editable": src == "lib",
    }


def _walk(root: str, max_files: int = 4000):
    """遍历 root 下的文件，跳过缓存/版本库目录。"""
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.startswith("prof_")]
        for fn in files:
            # 跳过隐藏文件与临时/备份垃圾 —— .selftest_xxx.tmp 这类
            # 自检残留不该出现在面板里（实测 other 类被它们塞满了）
            if fn.startswith(".") or fn.lower().endswith((".tmp", ".bak", ".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except Exception:
                continue
            if st.st_size > _MAX_FILE_BYTES:
                continue
            yield full, st
            n += 1
            if n >= max_files:
                return


def _extra_roots() -> list:
    """除生成文库、工作区之外的聚合来源：生成的图片 / 视频。"""
    out = []
    for sub in ("chat_images", "image_library", "saved_images"):
        try:
            d = config.data(sub)
        except Exception:
            continue
        if d and os.path.isdir(d):
            out.append(("img", d))
    return out


def list_all(recent_hours: int = 48, max_files: int = 4000) -> dict:
    """把所有产出物聚合成一张带类别的清单（只读，不动磁盘）。"""
    ensure_dir()
    items = []

    # ① 生成文库本体
    for f in list_files():
        items.append(_mk("lib", f["rel"], f["name"], f["size"], f["mtime"],
                         folder=f.get("folder") or ""))

    # ② 工作区里的软件项目（exe / zip / 源码都算）
    try:
        from . import workspace as _ws
        wroot = _ws.base()
    except Exception:
        wroot = ""
    if wroot and os.path.isdir(wroot):
        for full, st in _walk(wroot, max_files):
            rel = os.path.relpath(full, wroot).replace(os.sep, "/")
            parts = rel.split("/")
            if len(parts) < 2:
                continue
            items.append(_mk("ws", rel, parts[-1], st.st_size, st.st_mtime,
                             folder="/".join(parts[:-1]), project=parts[0]))

    # ③ 生成的图片 / 视频
    for src, root in _extra_roots():
        for full, st in _walk(root, max_files):
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            parts = rel.split("/")
            if _cat_of(os.path.splitext(parts[-1])[1]) not in ("image", "other"):
                continue
            items.append(_mk(src, rel, parts[-1], st.st_size, st.st_mtime,
                             folder="/".join(parts[:-1])))

    # 去重（同一物理文件可能被多个来源扫到）
    seen, uniq = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    items = uniq
    items.sort(key=lambda x: x["mtime"], reverse=True)

    counts = {}
    for it in items:
        counts[it["cat"]] = counts.get(it["cat"], 0) + 1

    # 「最近生成」：按时间取前 12 条，让用户一打开就看见刚做出来的东西
    recent_cut = time.time() - recent_hours * 3600
    recent = [it["id"] for it in items if it["mtime"] >= recent_cut][:12]
    if not recent:                      # 最近没有新的，就取最新 5 条兜底
        recent = [it["id"] for it in items[:5]]

    return {"ok": True, "files": items, "counts": counts,
            "recent": recent, "total": len(items),
            "dir": LIB_DIR, "stats": stats()}


def resolve_id(ident: str) -> dict:
    """把前端给的 `来源|相对路径` 解析成磁盘绝对路径（严格校验，防越界）。"""
    s = str(ident or "").strip()
    if "|" not in s:
        raise ValueError("id 格式不对（应为 来源|相对路径）")
    src, rel = s.split("|", 1)
    rel = rel.replace("\\", "/").strip()
    if not rel:
        raise ValueError("路径为空")
    if any(p in ("", ".", "..") for p in rel.split("/")):
        raise ValueError("路径里不允许出现空段或 ..")

    if src == "lib":
        p = _abs(_safe_rel(rel))
    elif src == "ws":
        from . import workspace as _ws
        parts = rel.split("/")
        if len(parts) < 2:
            raise ValueError("工作区文件路径要带项目名")
        proj = _ws.safe_project(parts[0])
        root = os.path.abspath(os.path.join(_ws.base(), proj)) + os.sep
        p = os.path.abspath(os.path.join(root, "/".join(parts[1:]).replace("/", os.sep)))
        if not p.startswith(root):
            raise ValueError("路径越界（只能打开工作区项目里的文件）")
    elif src == "img":
        root = ""
        for name, d in _extra_roots():
            cand = os.path.abspath(os.path.join(d, rel.replace("/", os.sep)))
            if cand.startswith(os.path.abspath(d) + os.sep) and os.path.isfile(cand):
                root, p = os.path.abspath(d) + os.sep, cand
                break
        if not root:
            raise ValueError("图片不存在（可能已被清理）")
    else:
        raise ValueError("未知来源：%s" % src)

    if not os.path.isfile(p):
        raise ValueError("文件不存在（可能已被删除或改名）")
    return {"ok": True, "id": s, "src": src, "rel": rel, "path": p}
