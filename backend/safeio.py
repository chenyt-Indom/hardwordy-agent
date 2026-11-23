# -*- coding: utf-8 -*-
"""安全写盘 —— 全项目统一的「原子写」入口。

★ 2026-11-24 实测踩到的两个真故障，都在这一个函数里解决：

1) **Windows 文件锁**（本轮实测）：
   `test_task_card.py` 在 `os.replace(tmp, pending_cards.jsonl)` 抛
   `PermissionError: [WinError 5] 拒绝访问`。
   原因：Windows 上只要**任何**进程/线程此刻正打开着目标文件（前端轮询读卡片、
   杀毒软件扫描、另一个后台线程读账本），rename 就会被拒 —— 而 POSIX 上
   这种替换是原子的、不会被拒。所以同一份代码在 Linux 上一直绿、在 Windows
   上偶发红，且**只在并发时出现**，最难查。
   做法：指数退避重试若干次；仍失败则退化为「原地覆盖写」（不原子，但保证
   数据不丢 —— 丢数据比不原子严重得多）。

2) **孤立代理字符**（2026-11-24 更早那次）：
   模型输出被截断时会留下半个 emoji（如 '\\udbc2'），`json.dump` 遇到它必然抛
   `UnicodeEncodeError: surrogates not allowed`。写在 try/except 里就变成
   **静默丢写** —— 用户看到的是「这条记录没了」，日志里只有一行 debug。
   做法：写前统一清洗（见 scrub_surrogates），只换非法代理，其余字符一个不动。

⚠️ 这里**不吞异常**：重试和降级都失败时照样往上抛，让调用方如实记日志。
   「写失败但假装成功」是这套机制里最不能接受的事。
"""

import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# 重试次数与退避基数（秒）。5 次累计约 0.31s，足够熬过前端一次轮询/杀毒扫描。
_RETRIES = 5
_BACKOFF = 0.02


def scrub_surrogates(obj):
    """把孤立代理字符（被截断的 emoji 半截）换成 `?`，返回清洗后的新对象。

    用 utf-8 的 replace 编解码清洗：只换非法代理，其余字符一个不动。
    """
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, list):
        return [scrub_surrogates(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_surrogates(x) for x in obj)
    if isinstance(obj, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in obj.items()}
    return obj


def _replace_with_retry(tmp, path):
    """把 tmp 换成 path，Windows 文件锁下重试，最后退化为原地覆盖。

    返回 "replace" / "overwrite"，调用方可用于观测（重试次数异常多说明有竞争）。
    """
    last = None
    for i in range(_RETRIES):
        try:
            os.replace(tmp, path)
            return "replace"
        except (PermissionError, OSError) as e:
            last = e
            # WinError 5/32 = 拒绝访问 / 文件被占用；其余错误也重试一次无妨
            time.sleep(_BACKOFF * (2 ** i))
    # 兜底：原地覆盖。不原子，但内容一定落盘 —— 比丢数据强。
    logger.warning("os.replace 重试 %d 次仍失败（%s），退化为原地覆盖: %s",
                   _RETRIES, last, path)
    with open(tmp, "rb") as src:
        data = src.read()
    with open(path, "wb") as dst:
        dst.write(data)
    try:
        os.remove(tmp)
    except OSError:
        logger.debug("清理 .tmp 失败", exc_info=True)
    return "overwrite"


def atomic_write_text(path, text):
    """原子写文本（UTF-8）。写前清洗孤立代理字符，替换带重试。"""
    path = str(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # ★ 2026-11-24 并发实测：原来固定用 path + ".tmp"，两个线程同时写同一路径时
    #   会互相把对方的 tmp 替换走/删掉，抛
    #   `PermissionError: Permission denied: 'xxx.json.tmp'` 和
    #   `WinError 2 找不到指定的文件`。
    #   临时文件名必须按写者唯一（进程 + 线程 + 随机），各写各的再原子替换，
    #   谁最后 replace 谁赢 —— 这才是并发安全的写法。
    tmp = "%s.%d.%d.%s.tmp" % (path, os.getpid(),
                               threading.get_ident(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(scrub_surrogates(text))
        return _replace_with_retry(tmp, path)
    finally:
        # 只在替换没成功时才残留 tmp；这里兜底清掉，避免干扰下次读取
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                logger.debug("清理 .tmp 失败", exc_info=True)


def atomic_write_json(path, data, indent=None):
    """原子写 JSON（UTF-8，不转义中文）。写前清洗孤立代理字符，替换带重试。"""
    text = json.dumps(scrub_surrogates(data), ensure_ascii=False, indent=indent)
    return atomic_write_text(path, text)
