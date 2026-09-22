"""DS-6 模型模块：四类模型、两组特征集与统一训练/评估入口。

本包位于 :mod:`river_sentinel_ml.features`（DS-4b 特征矩阵）与 DS-7（误差分析、高水位
事件与风险规则）之间，只消费冻结产物，不重新构造特征、不读原始数据。

模块划分：

- :mod:`~river_sentinel_ml.models.splitting`：段内样本协议与"只由 train 拟合"的标准化器；
- :mod:`~river_sentinel_ml.models.linear`：Ridge 线性模型与 ``alpha`` 选择；
- :mod:`~river_sentinel_ml.models.gbdt`：XGBoost（原生缺失处理、验证段早停）；
- :mod:`~river_sentinel_ml.models.sequence`：单层 LSTM（CPU、固定种子、确定性算法）；
- :mod:`~river_sentinel_ml.models.training`：特征子集、多随机种子、指标聚合与零样本兜底。

四条不可让步的边界：

1. 只用 ``usable_h{h} = True`` 的行（C-5，剔除目标时刻被插补的样本）；
2. 标准化、超参选择与早停只用 ``train`` / ``validation``，``test`` 只评一次；
3. 直接多时域，禁止递归滚动（HS-D-3）；
4. 结论一律为探索性/教学科研，不构成正式洪水预警（HS-D-12）。
"""

from .gbdt import GbdtForecaster, default_params, fit_xgboost
from .linear import DEFAULT_ALPHAS, RidgeForecaster, fit_ridge, select_alpha
from .sequence import LstmForecaster, fit_lstm
from .splitting import (
    SEGMENT_ORDER,
    SegmentSamples,
    Standardizer,
    as_float_matrix,
    boolean_values,
    build_all_segments,
    build_segment_samples,
)
from .training import (
    FEATURE_SUBSETS,
    FULL_FEATURES,
    HORIZONS,
    METRIC_NAMES,
    MODEL_NAMES,
    RAIN_FEATURES,
    SEEDS,
    WATER_ONLY_FEATURES,
    MetricSummary,
    aggregate_metric_summaries,
    evaluate_predictions,
    run_hourly_experiments,
    select_features,
    to_json_safe,
)

__all__ = [
    "DEFAULT_ALPHAS",
    "FEATURE_SUBSETS",
    "FULL_FEATURES",
    "GbdtForecaster",
    "HORIZONS",
    "LstmForecaster",
    "METRIC_NAMES",
    "MODEL_NAMES",
    "MetricSummary",
    "RAIN_FEATURES",
    "RidgeForecaster",
    "SEEDS",
    "SEGMENT_ORDER",
    "SegmentSamples",
    "Standardizer",
    "WATER_ONLY_FEATURES",
    "aggregate_metric_summaries",
    "as_float_matrix",
    "boolean_values",
    "build_all_segments",
    "build_segment_samples",
    "default_params",
    "evaluate_predictions",
    "fit_lstm",
    "fit_ridge",
    "fit_xgboost",
    "run_hourly_experiments",
    "select_alpha",
    "select_features",
    "to_json_safe",
]
