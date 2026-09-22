"""Ridge 线性模型（DS-6 的第 2 类模型）。

口径与边界：

- 输入先按**训练段**拟合的 :class:`~river_sentinel_ml.models.splitting.Standardizer`
  做 z-score 标准化，再用 ``sklearn.linear_model.Ridge`` 拟合；``validation`` 只用于
  选 ``alpha``，``test`` 从不参与拟合或选择。
- 段首滞后/累计窗口为 ``NaN``：这类行由 :meth:`SegmentSamples.without_missing_features`
  剔除并**计数上报**，绝不用 0 或均值填充。
- 预测用 ``coefficients @ x + intercept`` 自实现（而不是调用 ``Ridge.predict``）：
  这样空矩阵、含 ``NaN`` 的行都有确定行为，且模型可以原样序列化进产物。

本模块不读写文件、无随机性；同一输入两次拟合得到逐位一致的系数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from numbers import Real

import numpy as np
from sklearn.linear_model import Ridge

from .splitting import SegmentSamples, Standardizer, as_float_matrix, validated_horizon

#: ``alpha`` 候选集；在 ``validation`` 上按 MAE 选优。
DEFAULT_ALPHAS: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)


@dataclass(frozen=True)
class RidgeForecaster:
    """A fitted ridge regression plus the train-only standardiser it was fitted with."""

    alpha: float
    standardizer: Standardizer
    coefficients: np.ndarray
    intercept: float
    dropped_rows: int
    handles_missing: bool = False

    @property
    def feature_count(self) -> int:
        """Number of model inputs, i.e. the design-matrix width."""
        return int(self.coefficients.size)

    def predict(self, features: object) -> np.ndarray:
        """Predict with the fitted coefficients; rows holding ``NaN`` yield ``NaN``."""
        matrix = as_float_matrix(features, columns=self.feature_count)
        if matrix.shape[0] == 0:
            return np.empty(0, dtype="float64")
        standardised = self.standardizer.transform(matrix)
        return np.asarray(standardised @ self.coefficients + self.intercept, dtype="float64")

    def as_dict(self) -> dict[str, object]:
        """JSON-safe description of the fitted model (no pickle, no absolute path)."""
        return {
            "alpha": float(self.alpha),
            "feature_count": self.feature_count,
            "coefficients": [float(value) for value in self.coefficients],
            "intercept": float(self.intercept),
            "standardizer_mean": [float(value) for value in self.standardizer.mean],
            "standardizer_scale": [float(value) for value in self.standardizer.scale],
            "dropped_rows": int(self.dropped_rows),
        }


def fit_ridge(samples: SegmentSamples, *, alpha: float) -> RidgeForecaster:
    """Fit one ridge regression on the complete rows of ``samples``.

    Args:
        samples: 训练段样本（只允许传 ``train``；函数不做段来源校验，因为
            :class:`SegmentSamples` 的段名由调用方在 :func:`run_hourly_experiments`
            里固定为 ``train``）。
        alpha: L2 惩罚强度；必须为正的有限数。

    Returns:
        :class:`RidgeForecaster`，``dropped_rows`` 为因缺失特征被剔除的行数。

    Raises:
        ValueError: ``alpha`` 非法；或剔除缺失行后一个样本都不剩。
    """
    if isinstance(alpha, bool) or not isinstance(alpha, Real):
        raise ValueError(f"alpha must be a positive finite number, got {alpha!r}")
    strength = float(alpha)
    if not np.isfinite(strength) or strength <= 0.0:
        raise ValueError(f"alpha must be a positive finite number, got {alpha!r}")

    matrix, target, dropped = samples.without_missing_features()
    if matrix.shape[0] == 0:
        raise ValueError(
            f"no complete feature row left in the '{samples.segment}' segment for "
            f"horizon {samples.horizon}; every row carries at least one missing feature"
        )

    standardizer = Standardizer.fit(matrix)
    estimator = Ridge(alpha=strength, solver="cholesky")
    estimator.fit(standardizer.transform(matrix), target)
    return RidgeForecaster(
        alpha=strength,
        standardizer=standardizer,
        coefficients=np.asarray(estimator.coef_, dtype="float64"),
        intercept=float(estimator.intercept_),
        dropped_rows=int(dropped),
    )


def select_alpha(
    train: SegmentSamples,
    validation: SegmentSamples,
    *,
    alphas: Iterable[float] = DEFAULT_ALPHAS,
) -> tuple[float, dict[str, float | None]]:
    """Pick ``alpha`` by validation MAE, returning the full score table.

    Args:
        train: 训练段样本，唯一允许用于拟合的段。
        validation: 验证段样本，唯一允许用于选择的段。
        alphas: 候选集；必须非空且全为正有限数。

    Returns:
        ``(alpha, scores)``：``scores`` 以 ``str(alpha)`` 为键、验证段 MAE 为值；
        验证段无有效样本时对应值为 ``None``。全部候选都拿不到分数时返回最小的
        ``alpha``，并把所有分数留空，绝不虚构数值。

    Raises:
        ValueError: 候选集为空或含非法取值。
    """
    candidates: list[float] = []
    for value in alphas:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"alphas must be positive finite numbers, got {value!r}")
        number = float(value)
        if not np.isfinite(number) or number <= 0.0:
            raise ValueError(f"alphas must be positive finite numbers, got {value!r}")
        candidates.append(number)
    if not candidates:
        raise ValueError("alphas must not be empty")

    steps = validated_horizon(validation.horizon)
    scores: dict[str, float | None] = {}
    for candidate in candidates:
        forecaster = fit_ridge(train, alpha=candidate)
        if validation.rows == 0:
            scores[str(candidate)] = None
            continue
        predictions = forecaster.predict(validation.as_matrix())
        scores[str(candidate)] = _mean_absolute_error(validation.target, predictions, horizon=steps)

    scored = [(value, key) for key, value in scores.items() if value is not None]
    if not scored:
        return min(candidates), scores
    best = min(scored, key=lambda item: (item[0], float(item[1])))
    return float(best[1]), scores


def _mean_absolute_error(
    target: np.ndarray, prediction: np.ndarray, *, horizon: int
) -> float | None:
    """Pairwise-valid MAE, reusing the frozen metric implementation and nulling empties."""
    from .training import evaluate_predictions

    summary = evaluate_predictions(target, prediction, horizon=horizon)
    return summary.mae
