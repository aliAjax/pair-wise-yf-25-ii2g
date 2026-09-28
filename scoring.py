"""评审评分计算：维度、权重、校验、合成与平均。

只包含纯函数，不依赖数据库或 HTTP，便于独立测试与复用。
"""
from __future__ import annotations

DIMENSIONS = ("innovation", "rigor", "reproducibility")
DIMENSION_LABELS = {
    "innovation": "创新性",
    "rigor": "严谨性",
    "reproducibility": "复现性",
}
WEIGHTS = {
    "innovation": 0.40,  # 创新性 40%
    "rigor": 0.35,       # 严谨性 35%
    "reproducibility": 0.25,  # 复现性 25%
}
assert round(sum(WEIGHTS.values()), 6) == 1.0, "三个维度权重之和必须为 100%"

MIN_SCORE = 1
MAX_SCORE = 5


class ScoreError(ValueError):
    """评分数据不合法。code 供存取层映射为业务错误码。"""

    def __init__(self, message: str, code: str = "invalid_score"):
        super().__init__(message)
        self.code = code


def validate_dimension(name: str, value: object) -> int:
    if name not in DIMENSIONS:
        raise ScoreError(f"未知评分维度: {name}")
    # bool 是 int 的子类型，必须显式排除。
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_SCORE <= value <= MAX_SCORE:
        raise ScoreError(f"{DIMENSION_LABELS[name]}评分必须是 {MIN_SCORE} 到 {MAX_SCORE} 的整数")
    return value


def validate_scores(raw: object, *, partial: bool = False) -> dict[str, int]:
    """校验三维度评分。

    partial=True 时允许只带部分维度（用于保存草稿），但出现的维度必须合法；
    partial=False（提交）时三个维度缺一不可。
    """
    if not isinstance(raw, dict):
        raise ScoreError("评分必须是包含三个维度的对象")
    cleaned: dict[str, int] = {}
    for name, value in raw.items():
        validate_dimension(name, value)
        cleaned[name] = value
    if not partial:
        missing = [d for d in DIMENSIONS if d not in cleaned]
        if missing:
            labels = "、".join(DIMENSION_LABELS[d] for d in missing)
            raise ScoreError(f"缺少维度评分: {labels}", "missing_dimension")
    return cleaned


def composite_score(scores: dict[str, int]) -> float:
    """按 40%/35%/25% 合成 1-5 的总分。"""
    return round(sum(scores[d] * WEIGHTS[d] for d in DIMENSIONS), 2)


def dimension_averages(rows: list[dict[str, int]]) -> dict[str, float] | None:
    """对多条完整三维度评审逐维求平均；没有任何新制评审时返回 None。"""
    if not rows:
        return None
    return {
        d: round(sum(row[d] for row in rows) / len(rows), 2)
        for d in DIMENSIONS
    }
