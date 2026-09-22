"""XGBoost 梯度提升模型（DS-6 的第 3 类模型）。

口径与边界：

- **原生缺失处理**：``missing=np.nan``（XGBoost 默认），段首的 ``NaN`` 滞后不剔除、不填充，
  由树自己决定分裂方向；因此本模型 ``dropped_rows`` 恒为 0，样本量与线性/LSTM 不同，
  这个差额必须写进报告，不得含糊成"一样多"。
- **早停只用验证段**：``early_stopping_rounds`` 的 ``eval_set`` 只允许传
  :class:`~river_sentinel_ml.models.splitting.SegmentSamples` 的 ``validation``；
  ``test`` 段从不参与早停或调参。
- **CPU 确定性**：``tree_method="hist"``、``n_jobs=1``、固定 ``random_state``。
  单线程是为了让"同种子两次运行一致"可复现，不是性能取舍。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Integral
from typing import Any

import numpy as np
from xgboost import XGBRegressor

from .splitting import SegmentSamples, as_float_matrix

#: ``n_estimators`` 上界；实际轮数由验证段早停决定。
DEFAULT_N_ESTIMATORS = 2000
DEFAULT_EARLY_STOPPING_ROUNDS = 50
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_MAX_DEPTH = 4
DEFAULT_SUBSAMPLE = 0.9
DEFAULT_COLSAMPLE_BYTREE = 0.9
DEFAULT_MIN_CHILD_WEIGHT = 1.0
DEFAULT_REG_LAMBDA = 1.0


def default_params() -> dict[str, Any]:
    """The frozen XGBoost configuration, without ``random_state`` / early stopping."""
    return {
        "n_estimators": DEFAULT_N_ESTIMATORS,
        "learning_rate": DEFAULT_LEARNING_RATE,
        "max_depth": DEFAULT_MAX_DEPTH,
        "subsample": DEFAULT_SUBSAMPLE,
        "colsample_bytree": DEFAULT_COLSAMPLE_BYTREE,
        "min_child_weight": DEFAULT_MIN_CHILD_WEIGHT,
        "reg_lambda": DEFAULT_REG_LAMBDA,
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "tree_method": "hist",
        "device": "cpu",
        "n_jobs": 1,
        "verbosity": 0,
    }


@dataclass(frozen=True)
class GbdtForecaster:
    """A fitted :class:`xgboost.XGBRegressor` behind a narrow, inspectable surface."""

    model: XGBRegressor
    feature_count: int
    params: dict[str, Any]
    best_iteration: int | None
    dropped_rows: int = 0
    handles_missing: bool = True

    def predict(self, features: object) -> np.ndarray:
        """Predict on raw features; missing values are handled natively by the trees."""
        matrix = as_float_matrix(features, columns=self.feature_count)
        if matrix.shape[0] == 0:
            return np.empty(0, dtype="float64")
        return np.asarray(self.model.predict(matrix), dtype="float64")


def fit_xgboost(
    train: SegmentSamples,
    validation: SegmentSamples,
    *,
    seed: int,
    params: Mapping[str, Any] | None = None,
) -> GbdtForecaster:
    """Fit one XGBoost regressor with validation-based early stopping.

    Args:
        train: 训练段样本（允许含缺失）。
        validation: 验证段样本，仅用于早停；无可用样本时退化为固定轮数训练。
        seed: 随机种子，写入 ``random_state``。
        params: 覆盖 :func:`default_params` 的额外/替代参数。

    Returns:
        :class:`GbdtForecaster`；``best_iteration`` 为实际生效的轮数（无早停时为 ``None``）。

    Raises:
        ValueError: 训练段一个可用样本都没有。
    """
    if isinstance(seed, bool) or not isinstance(seed, Integral):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    if train.rows == 0:
        raise ValueError(
            f"no usable training row for horizon {train.horizon}; XGBoost needs at least one"
        )

    settings = default_params()
    settings["random_state"] = int(seed)
    settings["early_stopping_rounds"] = DEFAULT_EARLY_STOPPING_ROUNDS
    if params:
        settings.update(dict(params))
    if validation.rows == 0:
        # 没有验证段就不能早停：明确移除，而不是让 XGBoost 在 fit 时抛错。
        settings.pop("early_stopping_rounds", None)

    matrix = train.as_matrix()
    model = XGBRegressor(**settings)
    if validation.rows > 0:
        model.fit(
            matrix,
            train.target,
            eval_set=[(validation.as_matrix(), validation.target)],
            verbose=False,
        )
    else:
        model.fit(matrix, train.target, verbose=False)

    best_iteration = getattr(model, "best_iteration", None)
    return GbdtForecaster(
        model=model,
        feature_count=int(matrix.shape[1]),
        params=settings,
        best_iteration=None if best_iteration is None else int(best_iteration),
    )
