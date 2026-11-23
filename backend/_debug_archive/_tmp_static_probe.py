# -*- coding: utf-8 -*-
"""临时探针（验证完立即删除）：故意引入静默吞异常，用来验证
iter_runner 的「评分前静态预检 → 新增扣分点先修掉再评分」链路。"""
import logging

logger = logging.getLogger(__name__)


def probe_a(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        logger.debug("silent-except", exc_info=True)
    return 0


def probe_b(d):
    try:
        return d["k"]
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return None
