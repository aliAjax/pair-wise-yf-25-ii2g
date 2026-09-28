"""评审评分计算（纯逻辑）：维度权重、输入校验、总分合成与维度平均。

本模块不依赖数据库或 HTTP：数据存取见 ``app.ReviewStore``，页面见
``web/index.html``，三处分开维护。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional

# (接口字段, 中文名, 权重)：权重之和为 1.0。
DIMENSIONS: tuple[tuple[str, str, float], ...] = (
    ("innovation", "创新性", 0.40),
    ("rigor", "严谨性", 0.35),
    ("reproducibility", "复现性", 0.25),
)
DIMENSION_KEYS: tuple[str, ...] = tuple(key for key, _, _ in DIMENSIONS)
LABELS: dict[str, str] = {key: label for key, label, _ in DIMENSIONS}
WEIGHTS: dict[str, float] = {key: weight for key, _, weight in DIMENSIONS}

SCORE_MIN = 1
SCORE_MAX = 5


class ScoreError(ValueError):
    """评分数据不合法（缺维度、越界或类型错误）。"""


def _coerce(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScoreError(f"{LABELS[key]}评分必须是 {SCORE_MIN} 到 {SCORE_MAX} 的整数")
    if not SCORE_MIN <= value <= SCORE_MAX:
        raise ScoreError(f"{LABELS[key]}评分必须在 {SCORE_MIN} 到 {SCORE_MAX} 之间")
    return value


def validate_scores(payload: object, *, partial: bool = False) -> dict[str, int]:
    """校验三维度评分。

    正式提交（``partial=False``）时三个维度缺一不可；草稿暂存（``partial=True``）
    允许只提交已填写的维度，缺省或为 null 的维度直接跳过。
    """
    if not isinstance(payload, Mapping):
        raise ScoreError("评分必须是包含创新性、严谨性、复现性三个维度的对象")
    result: dict[str, int] = {}
    for key in DIMENSION_KEYS:
        value = payload.get(key)
        if value is None:
            if not partial:
                raise ScoreError(f"缺少{LABELS[key]}评分；三个维度全部填写后才能提交")
            continue
        result[key] = _coerce(key, value)
    return result


def composite_score(scores: Mapping[str, object]) -> float:
    """按 40% / 35% / 25% 合成总分，保留两位小数。三维度必须齐全。"""
    clean = validate_scores(scores)
    return round(sum(clean[key] * WEIGHTS[key] for key in DIMENSION_KEYS), 2)


def dimension_averages(rows: Iterable[Mapping[str, Optional[int]]]) -> dict[str, Optional[float]]:
    """逐维度计算平均分；某维度没有任何有效分时为 ``None``。

    旧评审（维度分为 NULL）在对应维度上被跳过；输出永远是三个键。
    """
    totals = {key: 0 for key in DIMENSION_KEYS}
    counts = {key: 0 for key in DIMENSION_KEYS}
    for row in rows:
        for key in DIMENSION_KEYS:
            value = row.get(key)
            if value is None:
                continue
            totals[key] += int(value)
            counts[key] += 1
    return {
        key: (round(totals[key] / counts[key], 2) if counts[key] else None)
        for key in DIMENSION_KEYS
    }
