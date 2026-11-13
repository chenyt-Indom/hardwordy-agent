"""文档导入：把 docx / pdf / doc / txt / md 里的文字抽出来给数字人朗读。

只做**取文本**这一件事，不解析排版（表格里的文字会按行拼起来，图片忽略）。

各格式的实际能力（不许对用户夸大）：
  · .txt / .md / .csv  —— 多编码自动识别，最可靠
  · .docx              —— python-docx，段落 + 表格文字
  · .pdf               —— pypdf，**只对有文字层的 PDF 有效**；
                          扫描件（图片型 PDF）抽不出字，会如实报错，不做 OCR
  · .doc（Word 97-2003）—— olefile 解析 OLE 复合文档的 piece table。
                          这是**尽力而为**：能处理绝大多数正常保存的 .doc，
                          但加密/损坏/极老版本（Word 6.0）可能抽不全，
                          失败时返回明确的 warning 而不是假装成功。
"""

import io
import os
import re

MAX_CHARS = 200000          # 单次导入的硬上限，防止把整个大文件塞进内存


# ---------------------------------------------------------------- 编码
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16", "latin-1")


# CJK 部首补充区 (U+2E80-U+2EFF) **整个区都没有 NFKC 映射**（实测确认），
# 只能手工对照。缺了这张表，PDF 抽出来的「⻘年」「⺠航」「⻓期」会原样进 TTS，
# 朗读时直接读错或跳过。
_RADICAL_MAP = {
    0x2E81: "\u5382", 0x2E84: "\u4E59", 0x2E85: "\u4EBB", 0x2E88: "\u5200",
    0x2E8A: "\u535C", 0x2E8C: "\u5C0F", 0x2E8D: "\u5C0F", 0x2E8E: "\u5140",
    0x2E8F: "\u5C22", 0x2E90: "\u5C22", 0x2E91: "\u5C22", 0x2E92: "\u5DF3",
    0x2E93: "\u5E7A", 0x2E94: "\u5F50", 0x2E95: "\u5F50", 0x2E96: "\u5FC4",
    0x2E97: "\u5FC3", 0x2E98: "\u624C", 0x2E99: "\u6535", 0x2E9B: "\u65E1",
    0x2E9C: "\u65E5", 0x2E9D: "\u6708", 0x2E9E: "\u6B79", 0x2E9F: "\u6BCD",
    0x2EA0: "\u6C11", 0x2EA1: "\u6C35", 0x2EA2: "\u6C3A", 0x2EA3: "\u706C",
    0x2EA4: "\u722B", 0x2EA5: "\u722B", 0x2EA6: "\u4E2C", 0x2EA7: "\u725B",
    0x2EA8: "\u72AD", 0x2EA9: "\u738B", 0x2EAA: "\u758B", 0x2EAB: "\u7F52",
    0x2EAC: "\u793B", 0x2EAD: "\u793B", 0x2EAE: "\u7AF9", 0x2EAF: "\u7CF9",
    0x2EB0: "\u7E9F", 0x2EB1: "\u7F52", 0x2EB2: "\u7F52", 0x2EB3: "\u7F52",
    0x2EB4: "\u7F52", 0x2EB5: "\u7F52", 0x2EB6: "\u7F8A", 0x2EB7: "\u7F8A",
    0x2EB8: "\u7F8A", 0x2EB9: "\u8001", 0x2EBA: "\u8080", 0x2EBB: "\u807F",
    0x2EBC: "\u6708", 0x2EBD: "\u81FC", 0x2EBE: "\u8279", 0x2EBF: "\u8279",
    0x2EC0: "\u8279", 0x2EC1: "\u864E", 0x2EC2: "\u8864", 0x2EC3: "\u897F",
    0x2EC4: "\u897F", 0x2EC5: "\u89C1", 0x2EC6: "\u89D2", 0x2EC7: "\u89D2",
    0x2EC8: "\u8BA0", 0x2EC9: "\u8D1D", 0x2ECA: "\u8DB3", 0x2ECB: "\u8F66",
    0x2ECC: "\u8FB6", 0x2ECD: "\u8FB6", 0x2ECE: "\u8FB6", 0x2ECF: "\u961D",
    0x2ED0: "\u9485", 0x2ED1: "\u9577", 0x2ED2: "\u9578", 0x2ED3: "\u957F",
    0x2ED4: "\u95E8", 0x2ED5: "\u961D", 0x2ED6: "\u961D", 0x2ED7: "\u96E8",
    0x2ED8: "\u9752", 0x2ED9: "\u97E6", 0x2EDA: "\u9875", 0x2EDB: "\u98CE",
    0x2EDC: "\u98DE", 0x2EDD: "\u98DF", 0x2EDE: "\u98DF", 0x2EDF: "\u98DF",
    0x2EE0: "\u9963", 0x2EE1: "\u9996", 0x2EE2: "\u9A6C", 0x2EE3: "\u9AA8",
    0x2EE4: "\u9B3C", 0x2EE5: "\u9C7C", 0x2EE6: "\u9E1F", 0x2EE7: "\u5364",
    0x2EE8: "\u9EA6", 0x2EE9: "\u9EC4", 0x2EEA: "\u9EFE", 0x2EEB: "\u6589",
    0x2EEC: "\u9F50", 0x2EED: "\u6B6F", 0x2EEE: "\u9F7F", 0x2EEF: "\u7ADC",
    0x2EF0: "\u9F99", 0x2EF1: "\u9F9C", 0x2EF2: "\u4E80", 0x2EF3: "\u9F9F",
}


def _normalize_cjk(text: str) -> str:
    """把 CJK 部首/兼容字符还原成正常汉字。

    实测（2026-11-14，data/kb/入团申请书.pdf）：pypdf 抽出来的是
    「⼊团申请书」「⻘年团」「⼴⼤」「⾝边」「⽼师」—— 这些是康熙部首
    (U+2F00-2FDF) 和 CJK 部首补充 (U+2E80-2EFF)，字形像但码位不同，
    TTS 会把它们读错或直接跳过。

    只对部首区做 NFKC 单字转换，**不动全角标点**（整体 NFKC 会把
    「，。；」变成半角，中文排版会变丑）。
    """
    import unicodedata

    out = []
    for ch in text:
        cp = ord(ch)
        if cp in _RADICAL_MAP:
            out.append(_RADICAL_MAP[cp])
        elif 0x2F00 <= cp <= 0x2FDF or 0xF900 <= cp <= 0xFAFF:
            out.append(unicodedata.normalize("NFKC", ch))
        else:
            out.append(ch)
    return "".join(out)


_SENT_END = "。！？；：!?;:…\"'）)》」』】"


def _reflow(text: str) -> str:
    """PDF 段落重排：把排版造成的**硬换行**合并回自然句子。

    实测同一份 PDF 抽出「在实践中学习中\\n国特⾊社会主义」——
    词被换行劈成两半，直接喂给 TTS 会断句错误、发音也怪。

    规则：上一行结尾不是句末标点、且下一行不像新段落开头（标题/序号/
    项目符号）时，就拼起来。行尾的连字符也一并处理。
    """
    lines = [l.strip() for l in text.split("\n")]
    out = []
    for ln in lines:
        if not ln:
            out.append("")
            continue
        if not out or not out[-1]:
            out.append(ln)
            continue
        prev = out[-1]
        # 下一行像新段落/标题/列表 -> 不合并
        if re.match(r"^(第[一二三四五六七八九十百]+[章节条]|[0-9]+[.、）)]|"
                    r"[（(][0-9一二三四五六七八九十]+[）)]|[-*•·]|#)", ln):
            out.append(ln)
            continue
        if prev and prev[-1] in _SENT_END:
            out.append(ln)
            continue
        # 合并：中文直接拼，英文单词之间补一个空格
        if re.search(r"[A-Za-z0-9]$", prev) and re.match(r"^[A-Za-z0-9]", ln):
            out[-1] = prev + " " + ln
        elif prev.endswith("-") and re.match(r"^[A-Za-z]", ln):
            out[-1] = prev[:-1] + ln
        else:
            out[-1] = prev + ln
    return "\n".join(out)


def _decode_text(raw: bytes) -> tuple:
    """按常见编码依次尝试，返回 (text, encoding)。"""
    for enc in _ENCODINGS:
        try:
            t = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 用替换字符比例判断这次解码是不是"勉强成功"
        bad = t.count("\ufffd")
        if bad == 0 or bad / max(1, len(t)) < 0.02:
            return t, enc
    return raw.decode("utf-8", "ignore"), "utf-8(ignore)"


# ---------------------------------------------------------------- docx
def _read_docx(raw: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(raw))
    parts = []
    for p in doc.paragraphs:
        s = (p.text or "").strip()
        if s:
            parts.append(s)
    # 表格：按行拼，单元格之间用空格
    for tb in doc.tables:
        for row in tb.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            cells = [c for c in cells if c]
            if cells:
                parts.append(" ".join(cells))
    return "\n".join(parts)


# ---------------------------------------------------------------- pdf
def _read_pdf(raw: bytes) -> tuple:
    from pypdf import PdfReader

    rd = PdfReader(io.BytesIO(raw))
    if getattr(rd, "is_encrypted", False):
        try:
            rd.decrypt("")
        except Exception:                               # noqa: BLE001
            return "", "PDF 已加密，解不开"
    parts = []
    empty_pages = 0
    for i, page in enumerate(rd.pages):
        try:
            t = page.extract_text() or ""
        except Exception:                               # noqa: BLE001
            t = ""
        t = t.strip()
        if t:
            parts.append(t)
        else:
            empty_pages += 1
    warn = ""
    if not parts:
        warn = ("这份 PDF 抽不出文字（很可能是扫描件/图片型 PDF）。"
                "本机没有 OCR，做不到识别图片里的字。")
    elif empty_pages:
        warn = "有 %d 页没有文字层（可能是扫描页），已跳过" % empty_pages
    return "\n\n".join(parts), warn


# ---------------------------------------------------------------- doc（老格式）
def _read_doc(raw: bytes) -> tuple:
    """Word 97-2003 .doc：解析 OLE 复合文档里的 piece table。

    ⚠️ 这是**尽力而为**的解析。核心步骤：
      1. olefile 打开，取 WordDocument 流
      2. 读 FIB：fcClx / lcbClx 定位 Clx（piece table 的容器）
      3. Clx 里 0x02 标记之后是 PlcPcd：CP 数组 + PCD 数组
      4. 每个 PCD 的 fc 决定这段文字是 UTF-16LE 还是单字节(CP1252)
    失败一律返回空串 + 明确原因，绝不返回乱码当成功。
    """
    import olefile
    import struct

    if not olefile.isOleFile(io.BytesIO(raw)):
        raise ValueError("不是 OLE 复合文档（可能不是真正的 .doc，或已损坏）")

    ole = olefile.OleFileIO(io.BytesIO(raw))
    try:
        if not ole.exists("WordDocument"):
            raise ValueError("缺少 WordDocument 流")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x200:
            raise ValueError("WordDocument 流过短")

        # FIB 起始处的 wIdent 应为 0xA5EC
        wIdent = struct.unpack_from("<H", wd, 0)[0]
        if wIdent != 0xA5EC:
            raise ValueError("FIB 标识不对（wIdent=0x%04X）" % wIdent)

        flags = struct.unpack_from("<H", wd, 0x000A)[0]
        which_tbl = "1Table" if (flags & 0x0200) else "0Table"

        fcClx = struct.unpack_from("<i", wd, 0x01A2)[0]
        lcbClx = struct.unpack_from("<I", wd, 0x01A6)[0]
        if lcbClx <= 0:
            raise ValueError("没有 Clx（可能是 Word 6.0/95 老格式）")

        tbl = which_tbl if ole.exists(which_tbl) else (
            "0Table" if ole.exists("0Table") else "1Table")
        if not ole.exists(tbl):
            raise ValueError("缺少 Table 流")
        table = ole.openstream(tbl).read()
        if fcClx < 0 or fcClx + lcbClx > len(table):
            raise ValueError("Clx 位置越界")

        clx = table[fcClx:fcClx + lcbClx]

        # 跳过 Prc（0x01）块，找到 0x02 的 Pcdt
        i = 0
        while i < len(clx) and clx[i] == 0x01:
            if i + 3 > len(clx):
                break
            cb = struct.unpack_from("<h", clx, i + 1)[0]
            i += 3 + max(0, cb)
        if i >= len(clx) or clx[i] != 0x02:
            raise ValueError("Clx 里找不到 piece table")
        lcb = struct.unpack_from("<I", clx, i + 1)[0]
        plc = clx[i + 5:i + 5 + lcb]
        n = (len(plc) - 4) // 12
        if n <= 0:
            raise ValueError("piece table 为空")

        cps = [struct.unpack_from("<I", plc, k * 4)[0] for k in range(n + 1)]
        out = []
        for k in range(n):
            pcd_off = 4 * (n + 1) + k * 8
            if pcd_off + 8 > len(plc):
                break
            fc = struct.unpack_from("<I", plc, pcd_off + 2)[0]
            nchars = cps[k + 1] - cps[k]
            if nchars <= 0:
                continue
            compressed = bool(fc & 0x40000000)
            fc = fc & 0x3FFFFFFF
            if compressed:
                # 单字节（CP1252 系）：fc 是字节偏移的一半
                start = fc // 2
                chunk = wd[start:start + nchars]
                try:
                    out.append(chunk.decode("cp1252", "ignore"))
                except Exception:                       # noqa: BLE001
                    out.append(chunk.decode("latin-1", "ignore"))
            else:
                start = fc
                chunk = wd[start:start + nchars * 2]
                out.append(chunk.decode("utf-16-le", "ignore"))
        text = "".join(out)
        # Word 用 \r 分段、\x07 表单元格、\x0c 分页
        text = text.replace("\r", "\n").replace("\x07", "\t").replace("\x0c", "\n")
        text = re.sub(r"[\x00-\x08\x0b\x0e-\x1f]", "", text)
        return text, ""
    finally:
        try:
            ole.close()
        except Exception:                               # noqa: BLE001
            pass


# ---------------------------------------------------------------- 统一入口
_EXT_KIND = {
    ".txt": "text", ".md": "text", ".markdown": "text", ".csv": "text",
    ".json": "text", ".log": "text", ".srt": "text",
    ".docx": "docx", ".pdf": "pdf", ".doc": "doc",
}


def read_bytes(raw: bytes, filename: str = "") -> dict:
    """把一份文档的字节流解析成纯文本。

    返回 {ok, text, kind, chars, encoding?, pages?, warning?}
    """
    if not raw:
        return {"ok": False, "error": "文件是空的"}
    ext = os.path.splitext(filename or "")[1].lower()
    kind = _EXT_KIND.get(ext, "text")
    warn = ""
    enc = ""

    try:
        if kind == "docx":
            text = _read_docx(raw)
            if not text.strip():
                return {"ok": False,
                        "error": "这份 Word 里没有可读的文字（可能全是图片或文本框）"}
        elif kind == "pdf":
            text, warn = _read_pdf(raw)
            if not text.strip():
                return {"ok": False, "error": warn or "PDF 里没有可读的文字"}
            # PDF 特有的两个坑：康熙部首、排版硬换行断词
            text = _reflow(_normalize_cjk(text))
        elif kind == "doc":
            try:
                text, warn = _read_doc(raw)
            except Exception as e:                      # noqa: BLE001
                # .doc 解析失败时退一步：如果内容其实是 RTF/纯文本就按文本读
                head = raw[:5]
                if head.startswith(b"{\\rtf"):
                    text, enc = _decode_text(raw)
                    warn = "这份文件其实是 RTF 格式，按纯文本读的（会带控制符）"
                else:
                    return {"ok": False,
                            "error": "解析老 .doc 失败：%s。"
                                     "可以在 Word/WPS 里另存为 .docx 再导入。" % e}
            if not text.strip():
                return {"ok": False, "error": "没能从这份 .doc 里抽出文字"}
            text = _normalize_cjk(text)
        else:
            text, enc = _decode_text(raw)

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return {"ok": False, "error": "文档里没有可读的文字"}

        truncated = False
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
            truncated = True

        res = {"ok": True, "text": text, "kind": kind, "chars": len(text),
               "truncated": truncated}
        if enc:
            res["encoding"] = enc
        if warn:
            res["warning"] = warn
        if truncated:
            res["warning"] = ((res.get("warning", "") + "；").lstrip("；")
                              + "文档超过 %d 字，只取了前面部分" % MAX_CHARS)
        return res
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def read_path(path: str) -> dict:
    if not os.path.isfile(path):
        return {"ok": False, "error": "找不到文件"}
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CHARS * 8)
    except OSError as e:
        return {"ok": False, "error": "读文件失败：%s" % e}
    r = read_bytes(raw, os.path.basename(path))
    if r.get("ok"):
        r["name"] = os.path.basename(path)
    return r


def split_paragraphs(text: str) -> list:
    """给前端预览用：切成段落，带字数。"""
    out = []
    for i, p in enumerate((text or "").split("\n")):
        s = p.strip()
        if s:
            out.append({"i": i, "text": s, "chars": len(s)})
    return out
