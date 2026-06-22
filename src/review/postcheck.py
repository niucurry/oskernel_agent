"""后置校验：回源验证 LLM evidence 行号是否落在函数行号范围内，并做降级。"""

from __future__ import annotations

import re

from loguru import logger

from .schema import AGG_DISPUTED

_INT = re.compile(r"\d+")


def extract_line_numbers(value) -> list[int]:
    """从 new_lines / old_lines（str / list / int）抽取所有整数行号。"""
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for v in value:
            out.extend(extract_line_numbers(v))
        return out
    return [int(x) for x in _INT.findall(str(value))]


def _in_range(value, rng: tuple[int, int]) -> bool:
    nums = extract_line_numbers(value)
    if not nums:  # 无法回源的证据视为不可信
        return False
    lo, hi = rng
    return all(lo <= n <= hi for n in nums)


def postcheck(review: dict, q_range: tuple[int, int], c_range: tuple[int, int]) -> list[str]:
    """过滤越界 evidence；likely_clone 但证据清空则降级为 disputed。返回警告列表。"""
    warnings: list[str] = []
    kept = []
    for item in review.get("evidence", []) or []:
        new_ok = _in_range(item.get("new_lines"), q_range)
        old_ok = _in_range(item.get("old_lines"), c_range)
        if new_ok and old_ok:
            kept.append(item)
        else:
            msg = (
                f"丢弃越界 evidence：new_lines={item.get('new_lines')}(应∈{q_range}) "
                f"old_lines={item.get('old_lines')}(应∈{c_range})"
            )
            warnings.append(msg)
            logger.warning(msg)
    review["evidence"] = kept

    if review.get("verdict") == "likely_clone" and not kept:
        review["verdict"] = AGG_DISPUTED
        review["downgraded_reason"] = "likely_clone 但 evidence 全部越界/为空，降级为 disputed"
        warnings.append(review["downgraded_reason"])
        logger.warning(review["downgraded_reason"])

    return warnings
