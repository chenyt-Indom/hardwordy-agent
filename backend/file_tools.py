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
import logging
logger = logging.getLogger(__name__)

TEXT_EXTS = {
    ".txt", ".md", ".py", ".json", ".csv", ".log", ".ini", ".cfg",
    ".html", ".htm", ".xml", ".js", ".ts", ".css", ".sh", ".bat",
    ".yaml", ".yml", ".toml", ".conf", ".sql", ".r", ".go", ".java",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".rb", ".php",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MAX_READ = 200_000  # 单文件最多读取字符，防止超大文件卡死
MAX_IMAGE_BYTES = 80 * 1024 * 1024  # 图片上限 80MB：再大也会被压到 1024px，读进内存只会吃爆


def read_text(path: str, max_chars: int = MAX_READ):
    encodings = ["utf-8", "gbk", "latin-1"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read(max_chars)
            return text
        except UnicodeDecodeError:
            continue          # 编码不对，换下一个编码再试
        except OSError:
            # ⚠️ 2026-11-19 实测：原来只捕 PermissionError，漏了 OSError 本体。
            # 扫 C:\Users 时有一个文件读时报 `OSError: [Errno 22] Invalid argument`
            # （被独占锁 / 特殊设备文件 / 网络盘都会这样），异常直接冒到端点，
            # 整个目录扫描崩掉、返回 500，一个能搜的文件都拿不到。
            # 这类错误换编码没用，直接放弃这个文件（返回 None，调用方会跳过）。
            return None
    return None


def read_image_b64(path: str, max_side: int = 1024):
    """读取图片并压缩为 base64，供 Qwen-VL 理解。"""
    # ⚠️ 原来这里有一句 raw = open(path, "rb").read()，读完从未被使用：
    # 等于为每张图多占一份完整文件大小的内存，大图直接吃爆。已删。
    try:
        _size = os.path.getsize(path)
    except OSError:
        _size = 0
    if _size > MAX_IMAGE_BYTES:
        raise ValueError("图片过大（%.0f MB），上限 %d MB" % (_size / 1048576, MAX_IMAGE_BYTES // 1048576))
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
        # ⚠️ 2026-11-19 实测：走 read_text 直读时，指向虚拟文件系统 / 网络盘的文件
        # 会卡 **12 秒**才抛 Errno 22，而 read_text 把它当成"编码不对"返回 None，
        # 用户看到的是「无法解码文件编码」—— 提示完全误导。这里用带超时的读取，
        # 并把「读超时」和「编码不对」分开报。
        import time as _t
        _t0 = _t.time()
        text = _read_with_timeout(path)
        if text is None:
            if _t.time() - _t0 >= READ_ONE_TIMEOUT * 0.9:
                return {"ok": False, "error":
                        "读取超时（%.0f 秒）：该文件可能在网络盘或虚拟文件系统上，"
                        "建议先复制到本地再读。路径: %s" % (READ_ONE_TIMEOUT, path)}
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
    # 2026-11-14 实测（本机真实数字）：项目根一共 264530 个文件，
    # 其中 data/ 103951 个（2.6s）、dist/ 63111 个（1.1s），
    # 全遍历要 7 秒 —— 模糊查找在 4 秒超时内根本走不到 backend/。
    # 这些是运行期数据 / 构建产物，不该参与「找文件」。
    "data", "sessions", "logs", "hive_data", "image_library",
    # ⚠️ 2026-11-19 实测：这些是「虚拟文件系统 / 云同步」目录，读一个 89 字节的
    # 文件要 **12.17 秒**（os.stat 却是 0.00s，所以查大小根本预判不出来），
    # 最后还抛 `OSError: [Errno 22]`。它们不是真实磁盘，不该参与任何批量扫描：
    # 扫 C:\Users 实测被一个 CrossDevice 里的文件拖到 11.87 秒（deadline 是 4 秒）。
    "CrossDevice", "MicrosoftEdgeBackups", "Application Data",
    "百度网盘", "BaiduNetdisk", "坚果云", "Nutstore", "WPS云盘",
    "GoogleDrive", "Google Drive", "iCloudDrive", "iCloud Drive",
    "Dropbox", "OneDriveTemp", "CloudDrive",
    "build", "build-lite", "vendor", "releases", "_backup_out",
    "_bak_avatarrm_20261112_235603", ".webtest-data",
}

# 这些目录名前缀一律跳过（dist_v3 / dist_v24 … 本机有二十多个）
SKIP_PREFIXES = ("dist", ".", "_bak_", "_backup_")
SEARCH_MAX_DEPTH = 5        # 最多往下钻 5 层
SEARCH_MAX_SECONDS = 4.0    # 总耗时上限，防止在大盘上卡死
SEARCH_MAX_HITS = 20
# 候选文件硬上限（只为内存保护，不是搜索范围）。
# ⚠️ 2026-11-19 实测：这个值原本是 50，导致 backend 175 个文本文件里**只搜前 50 个**，
# 漏掉 125 个 —— tools.py / self_iterate.py / video.py / model_manager.py 全在漏掉那批里，
# 搜这些文件独有的关键词会直接「未找到」（实测搜 model_keep_alive 漏掉 model_manager.py）。
# 而实测把 175 个文件全读一遍只要 **0.037 秒**，50 这个上限纯属自伤。
# 真正的时间控制交给 SEARCH_MAX_SECONDS（deadline 兜底）。
SEARCH_MAX_CANDIDATES = 20000
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


def _iter_files(root, max_depth: int = SEARCH_MAX_DEPTH, deadline: float = 1e18):
    r"""逐条产出 (所在目录, 文件名)。**唯一**的目录遍历实现。

    ⚠️ 2026-11-19 实测三次后定稿，不用 os.walk 的两个硬原因：

    1) os.walk 必须把**一个目录里的条目全部读完**才 yield 一次，所以
       deadline 只能在「目录与目录之间」检查。扫 C:\Windows 实测 5.54 秒、
       C:\Users 实测 14.75 秒，都远超 SEARCH_MAX_SECONDS=4 的预算。
    2) junction：`C:\Users\Default User` 是 junction，但
       `entry.is_symlink()` 对它返回 False，于是 `os.walk(followlinks=False)`
       **拦不住它**，会跟进去重复扫。

    改成 os.scandir 逐条遍历：每 512 条查一次 deadline（能真正中断），
    junction / 符号链接目录一律不跟。
    resolve_path 和 _walk_candidates 共用这一份，避免两套逻辑分叉。
    """
    import time as _t
    from collections import deque
    queue = deque([(root, 0)])
    n_seen = 0
    while queue:
        cur, depth = queue.popleft()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for entry in it:
                n_seen += 1
                if (n_seen & 511) == 0 and _t.time() > deadline:
                    return
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth + 1 >= max_depth:
                            continue
                        name = entry.name
                        if name in SKIP_DIRS or name.startswith(SKIP_PREFIXES):
                            continue
                        if entry.is_symlink():
                            continue
                        _isj = getattr(entry, "is_junction", None)
                        if _isj is not None and _isj():
                            continue
                        queue.append((entry.path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        yield cur, entry.name
                except OSError:
                    continue


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
        if timed_out or len(hits) >= SEARCH_COLLECT_CAP:
            break
        # 上一个 root 里已经找到「文件名完全相等」的（100 分），不用再翻别处
        if any(sc >= 100 for sc, _ in hits):
            break
        if not os.path.isdir(root):
            continue
        # 2026-11-19：原来这里自己写了一套 os.walk + 剪枝，和 _walk_candidates
        # 是两份重复实现，且同样有「单目录读完才检查超时」和「跟 junction」的毛病
        # （实测 C:\Windows 5.54 秒超预算）。统一改用 _iter_files。
        for dirpath, fn in _iter_files(root, max_depth, t0 + timeout):
            if len(hits) >= SEARCH_COLLECT_CAP:
                break
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
        # 生成器是因为 deadline 到点才停的 → 如实标记超时
        if time.time() - t0 > timeout:
            timed_out = True

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


def _walk_candidates(target: str, max_files: int = SEARCH_MAX_CANDIDATES,
                     deadline: float = 1e18) -> list:
    r"""在目录里收集候选文本文件（只看 TEXT_EXTS）。

    ⚠️ 2026-11-19 实测三次才定下来：
    1) 最初是「对 31 个扩展名各做一次 glob 全递归」：项目根 26 万文件实测
       7.29s/扩展名、外推 **224 秒**，接口直接卡死。
    2) 改成 os.walk 后仍不行：单目录读完才 yield，deadline 拦不住
       （C:\Users 实测 14.75 秒）。
    3) 还跟 junction（os.walk(followlinks=False) 不认 junction）。
    现在统一走 _iter_files，与 resolve_path 共用同一份遍历实现。
    """
    out = []
    for dirpath, fn in _iter_files(target, SEARCH_MAX_DEPTH, deadline):
        if os.path.splitext(fn)[1].lower() in TEXT_EXTS:
            out.append(os.path.join(dirpath, fn))
            if len(out) >= max_files:
                break
    return out


# 单文件读取的时间上限。deadline（SEARCH_MAX_SECONDS）只在两次读取**之间**检查，
# 对「单次 open 就卡 12 秒」的虚拟文件系统完全无效（实测 CrossDevice 目录）。
# 这里用可放弃的 daemon 线程兜住单次 I/O：
#   - daemon=True → 卡住的线程不会阻止进程退出
#   - join(timeout) → 超时立刻放弃，不阻塞整次扫描
#   - 读文件是纯 I/O、无共享状态，线程安全
READ_ONE_TIMEOUT = 3.0      # 单个文件最多等 3 秒
READ_SLOW_BUDGET = 3        # 累计超时 3 次就判定「这个目录不适合扫」，提前收工


def _read_with_timeout(path: str, timeout: float = READ_ONE_TIMEOUT):
    """带超时的 read_text。超时/异常都返回 None（调用方跳过该文件）。"""
    import threading
    box = {}

    def _work():
        try:
            box["text"] = read_text(path)
        except Exception as e:                          # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_work, daemon=True, name="ft-read")
    th.start()
    th.join(timeout)
    if th.is_alive():
        # 放弃这个文件：线程是 daemon，会自己结束（或随进程退出被清理）
        logger.debug("读取超时（%.0fs），跳过：%s", timeout, path)
        return None
    return box.get("text")


def find_hits(paths_or_dirs: list[str], keywords: str,
              max_files: int = SEARCH_MAX_CANDIDATES) -> list:
    """按关键词检索，返回**结构化**命中列表。

    每项：{"path": 路径, "text": 全文, "idx": 首个命中位置, "kws": 命中的词}

    ⚠️ 2026-11-19：`tools._do_search_files` 原来自己另写了一套
    「31 个扩展名各 glob 一次 + 全部扫完才截断」—— 和本文件修之前是**同一个 bug**
    （项目根 26 万文件实测 221.97 秒，且扫完还因为截断在前 200 个而返回"未找到"）。
    现在两边共用这一份实现，以后只有一处需要维护。
    """
    import time as _t
    # ⚠️ 2026-11-19：传字符串时 `for target in "D:\\xxx"` 会**逐字符遍历**，
    # 每个字符都不是有效路径 → 全部 continue → 静默返回空、且不报错。
    # 实测 /api/file/scan 传 {"paths": "D:\\xxx"} 得到 0 字，看起来像「没搜到」，
    # 调用方会据此得出错误结论。这里统一归一成 list。
    if isinstance(paths_or_dirs, str):
        paths_or_dirs = [paths_or_dirs]
    elif not paths_or_dirs:
        return []
    kws = [k.strip().lower() for k in str(keywords or "").split() if k.strip()]
    if not kws:
        return []
    out = []
    slow_n = 0                      # 累计读取超时次数，超预算就放弃整个目录
    deadline = _t.time() + SEARCH_MAX_SECONDS
    for target in paths_or_dirs:
        if _t.time() > deadline:
            break
        # 空路径必须跳过：os.path.abspath("") 会解析成**当前工作目录**，
        # 于是「没传路径」变成「递归扫描整个项目目录」（实测接口卡死数分钟）。
        if not target or not str(target).strip():
            continue
        target = os.path.abspath(str(target).strip())
        if os.path.isdir(target):
            candidates = _walk_candidates(target, max_files, deadline)
        elif os.path.isfile(target):
            candidates = [target]
        else:
            continue
        for c in candidates[:max_files]:
            if _t.time() > deadline:
                break
            # ⚠️ 单个文件读不出来（独占锁 / 虚拟文件系统 / 编码怪异）不能拖垮整次扫描。
            # read_text 兜了 OSError，_read_with_timeout 再兜住「单次 open 卡十几秒」。
            _t1 = _t.time()
            text = _read_with_timeout(c)
            if text is None and (_t.time() - _t1) >= READ_ONE_TIMEOUT * 0.9:
                slow_n += 1
                if slow_n >= READ_SLOW_BUDGET:
                    # 累计多个文件读超时 → 这个目录大概挂在网络盘/虚拟 FS 上，
                    # 继续扫下去只会一个个等 3 秒，不如提前收工返回已找到的。
                    logger.info("累计 %d 个文件读取超时，提前结束扫描：%s", slow_n, target)
                    break
            if not text:
                continue
            low = text.lower()
            if any(k in low for k in kws):
                first = min((low.find(k) for k in kws if k in low), default=0)
                out.append({"path": c, "text": text, "idx": first,
                            "kws": [k for k in kws if k in low]})
    return out


def read_by_keywords(paths_or_dirs: list[str], keywords: str,
                     max_files: int = SEARCH_MAX_CANDIDATES) -> str:
    """在指定路径集里按关键词检索文件内容，返回匹配摘要。"""
    results = []
    for h in find_hits(paths_or_dirs, keywords, max_files):
        # 截取命中上下文
        seg = h["text"][max(0, h["idx"] - 100): h["idx"] + 200]
        results.append(f"--- {h['path']} ---\n{seg}")
    return "\n\n".join(results[:30])