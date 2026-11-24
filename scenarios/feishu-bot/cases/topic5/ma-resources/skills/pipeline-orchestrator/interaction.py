"""供应商互动量（缺陷清单 Q2）。

总互动量直接采用数说聚合返回的互动量字段，统计定义沿用供应商口径，不根据点赞/评论/转发/收藏
等明细重新计算。缺失或平台不支持时为不可得（None），不补零、不估算。
"""
from __future__ import annotations


def interaction_value(row: dict) -> int | None:
    value = (row or {}).get("interaction_cnt")
    if value in (None, "", "-", "--"):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
