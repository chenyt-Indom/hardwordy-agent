# -*- coding: utf-8 -*-
"""本地文件读取能力。

让 AI 能够读取用户本地文件内容（文本 / 图片 / 常见文档）。
- 文本类：.txt .md .py .json .csv .log .ini .cfg .html .htm .xml .js .ts 等
- 图片类：交给 Qwen-VL 理解（返回 base64）
- 常见文档：.pdf（若装 pypdf 可读文本）
完全本地，用户可以传入任意合法的本机路径。
"""
import os
import re
import base64

TEXT_EXTS = {
    ".txt", ".md", ".py", ".json", ".csv", ".log", ".ini", ".cfg",
    ".html", ".htm", ".xml", ".js", ".ts", ".css", ".sh", ".bat",
    ".yaml", ".yml", ".toml", ".conf", ".sql", ".r", ".go", ".java",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".rb", ".php",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MAX_READ = 200_000  # 单文件最多读取字符，防止超大文件卡死


def read_text(path: str, max_chars: int = MAX_READ):
    encodings = ["utf-8", "gbk", "latin-1"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read(max_chars)
            return text
        except (UnicodeDecodeError, PermissionError):
            continue
    return None


def read_image_b64(path: str, max_side: int = 1024):
    """读取图片并压缩为 base64，供 Qwen-VL 理解。"""
    raw = open(path, "rb").read()
    img = __import__("PIL.Image", fromlist=["Image"]).open(os.fsdecode(path))
    img = img.convert("RGB")
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _read_pdf(path):
    """尝试用 pypdf 读取 PDF 文本；未安装则返回提示。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[提示] 未安装 pypdf，无法解析 PDF。"
    text = []
    try:
        reader = PdfReader(path)
        for page in reader.pages[:20]:
            text.append(page.extract_text() or "")
    except Exception as e:
        return f"[解析PDF出错: {e}]"
    return "\n".join(text)


def read_file(path: str) -> dict:
    """统一入口：按扩展名决定读取方式，返回结构化内容。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if os.path.isdir(path):
        # 目录则列出内容
        try:
            items = os.listdir(path)[:200]
            return {"ok": True, "type": "dir", "content": "\n".join(items), "path": path}
        except PermissionError as e:
            return {"ok": False, "error": f"无法读取目录: {e}"}
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        try:
            b64 = read_image_b64(path)
            return {"ok": True, "type": "image", "b64": b64, "path": path}
        except Exception as e:
            return {"ok": False, "error": f"图片读取失败: {e}"}
    if ext == ".pdf":
        return {"ok": True, "type": "text", "content": _read_pdf(path)[:MAX_READ], "path": path}
    if ext in TEXT_EXTS or True:  # 其他一律按文本尝试
        text = read_text(path)
        if text is None:
            return {"ok": False, "error": f"无法解码文件编码: {path}"}
        return {"ok": True, "type": "text", "content": text, "path": path}
    return {"ok": False, "error": f"不支持的文件类型: {ext}"}


SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "AppData", "Windows", "Program Files", "Program Files (x86)",
    "$Recycle.Bin", "System Volume Information", ".cache", "site-packages",
    "dist", "build", ".next", "target", ".idea", ".vscode",
    # 2026-11-14 实测：home 下递归会把 conda 的包目录全扫一遍
    #（`tools` 会误命中 aioitertools.conda，还慢到 3 秒），必须剪掉
    "anaconda3", "miniconda3", "pkgs", ".conda", ".condarc",
    "node_modules", ".npm", ".gradle", ".m2", "Library", "OneDrive",
}
SEARCH_MAX_DEPTH = 5        # 最多往下钻 5 层
SEARCH_MAX_SECONDS = 4.0    # 总耗时上限，防止在大盘上卡死
SEARCH_MAX_HITS = 20
SEARCH_COLLECT_CAP = 200    # 收集阶段的上限（够排序，又不会把内存撑爆）


def _default_roots() -> list:
    """模糊查找的默认起点。

    ⚠️ 顺序有讲究（2026-11-14 实测）：项目根和常用目录放前面，
    整个 home 放**最后**。原来 home 在 proj 之前，扫 home 时就把
    收集上限占满了，`tools` 反而找不到项目里的 backend/tools.py。
    """
    home = os.path.expanduser("~")
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = []
    if os.path.isdir(proj):
        roots.append(proj)
    for sub in ("Desktop", "Documents", "Downloads", "桌面", "文档", "下载"):
        p = os.path.join(home, sub)
        if os.path.isdir(p) and p not in roots:
            roots.append(p)
    if os.path.isdir(home) and home not in roots:
        roots.append(home)
    return roots


def _score(name_low: str, rel_low: str, want_low: str) -> int:
    """命中程度：文件名完全相等 100，去后缀相等 90，包含 70，路径包含 50。"""
    stem = os.path.splitext(name_low)[0]
    want_stem = os.path.splitext(want_low)[0]
    if name_low == want_low:
        return 100
    if stem == want_stem:
        return 90
    if want_low in name_low:
        return 70
    if want_low in rel_low:
        return 50
    return 0


def resolve_path(path: str, roots=None, max_depth: int = SEARCH_MAX_DEPTH,
                 timeout: float = SEARCH_MAX_SECONDS) -> dict:
    """把「大概路径」解析成真实存在的文件路径。

    依次尝试：原样 → 相对项目根 → 在常用目录里按文件名/部分路径模糊查找。
    返回 {ok, path, how, decisive, candidates}
      how = exact / relative / search / not_found
    """
    import time
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return {"ok": False, "how": "not_found", "error": "路径是空的"}

    # 1) 原样（绝对路径，或相对当前工作目录）
    p = os.path.abspath(os.path.expanduser(raw))
    if os.path.exists(p):
        return {"ok": True, "path": p, "how": "exact", "candidates": []}

    # 2) 相对项目根
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p2 = os.path.abspath(os.path.join(proj, raw))
    if os.path.exists(p2):
        return {"ok": True, "path": p2, "how": "relative", "candidates": []}

    # 3) 模糊查找
    want_low = raw.replace("\\", "/").strip("/").lower()
    base = os.path.basename(want_low)
    if not base:
        return {"ok": False, "how": "not_found", "error": "看不懂这个路径：%s" % raw}

    search_roots = roots or _default_roots()
    hits = []
    t0 = time.time()
    timed_out = False

    for root in search_roots:
        if timed_out or len(hits) >= SEARCH_MAX_HITS * 3:
            break
        if not os.path.isdir(root):
            continue
        root_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() - t0 > timeout:
                timed_out = True
                break
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            if dirpath.count(os.sep) - root_depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                fn_low = fn.lower()
                # 快筛：文件名或所在路径里至少得有共同子串
                if base not in fn_low and base not in dirpath.lower():
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/").lower()
                sc = _score(fn_low, rel, want_low)
                if sc <= 0:
                    sc = _score(fn_low, rel, base)
                if sc > 0:
                    hits.append((sc, full))

    if not hits:
        return {"ok": False, "how": "not_found", "timed_out": timed_out,
                "error": "没找到跟「%s」匹配的文件%s"
                         % (raw, "（搜索超时，可能只搜了一部分）" if timed_out else "")}

    seen, uniq = set(), []
    for sc, full in sorted(hits, key=lambda x: -x[0]):
        if full in seen:
            continue
        seen.add(full)
        uniq.append({"path": full, "score": sc})
        if len(uniq) >= SEARCH_MAX_HITS:
            break

    top = uniq[0]
    # 唯一命中，或最高分明显领先（>=90 且比第二名高）→ 直接认定
    decisive = (len(uniq) == 1) or (top["score"] >= 90 and
                                    (len(uniq) < 2 or uniq[1]["score"] < top["score"]))
    return {"ok": True, "path": top["path"], "how": "search",
            "decisive": decisive, "candidates": uniq, "timed_out": timed_out}


def read_by_keywords(paths_or_dirs: list[str], keywords: str, max_files: int = 50) -> str:
    """在指定路径集里按关键词检索文件内容，返回匹配摘要。"""
    import glob
    results = []
    for target in paths_or_dirs:
        target = os.path.abspath(target)
        if os.path.isdir(target):
            candidates = []
            for ext in TEXT_EXTS:
                candidates += glob.glob(os.path.join(target, "**", "*" + ext), recursive=True)
            candidates = candidates[:max_files]
        elif os.path.isfile(target):
            candidates = [target]
        else:
            continue
        kws = [k.strip().lower() for k in keywords.split() if k.strip()]
        for c in candidates[:max_files]:
            text = read_text(c)
            if not text:
                continue
            low = text.lower()
            if kws and any(k in low for k in kws):
                # 截取命中上下文
                first = min((low.find(k) for k in kws if k in low), default=0)
                seg = text[max(0, first - 100): first + 200]
                results.append(f"--- {c} ---\n{seg}")
    return "\n\n".join(results[:30])