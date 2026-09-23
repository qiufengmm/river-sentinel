"""DS-7 已发布指标的冻结快照（只读消费，严禁在此重算或改动）。

来源：``docs/experiments/hourly-error-and-events.md`` 的 **§2 test 段逐段逐时域指标** 与
**§3 test 段事件级指标**（由 ``scripts/run_hourly_error_analysis.py`` 确定性生成，禁止手工编辑）。

为什么内嵌：``data/processed/hourly_error_analysis.json`` 属于本地处理产物，被
``.gitignore`` 忽略且不进仓库；``Settings.artifact_dir`` 下若提供
``model_comparison.json``，:class:`ComparisonService` **优先读取产物**，读不到才用本快照，
并在 ``warnings`` 中给出 ``COMPARISON_ARTIFACT_UNAVAILABLE``。

数值一律照抄报告，**不得四舍五入、不得挑选、不得把 ``null`` 写成 0**。
"""

from __future__ import annotations

#: 报告路径与取值小节（机器核对用）。
SOURCE_REPORT = "docs/experiments/hourly-error-and-events.md"
SOURCE_SECTIONS = "§2 逐段逐时域指标（test 段）与 §3 事件级指标（test 段）"
SNAPSHOT_VERSION = "ds7-hourly-error-and-events"

#: 本快照只覆盖 test 段（真正的泛化结论由 test 段承担）。
SEGMENT = "test"

#: 研究性高水位阈值（train 段 p90 拟合，validation / test 段不参与）。
THRESHOLD = 7.18
THRESHOLD_SOURCE = "train p90 research_high_water"

#: 模型键 -> 报告中的显示名。
MODEL_DISPLAY: dict[str, str] = {
    "persistence": "持久性基线",
    "ridge_e1": "Ridge E1",
    "xgboost_e1": "XGBoost E1",
    "lstm_e1": "LSTM E1",
    "ridge_e2": "Ridge E2",
    "xgboost_e2": "XGBoost E2",
    "lstm_e2": "LSTM E2",
}

#: 模型键 -> 产物/实验版本标识。
MODEL_VERSION: dict[str, str] = {
    "persistence": "ds5b-hourly-baseline-1",
    "ridge_e1": "ds6-e1",
    "xgboost_e1": "ds6-e1",
    "lstm_e1": "ds6-e1",
    "ridge_e2": "ds6-e2",
    "xgboost_e2": "ds6-e2",
    "lstm_e2": "ds6-e2",
}

#: (model, horizon, sample_count, mae, rmse, r2, nse)，照抄 §2 test 段。
METRICS: tuple[tuple[str, int, int, float, float, float, float], ...] = (
    ("persistence", 1, 144, 0.040556, 0.094817, 0.932208, 0.932208),
    ("ridge_e1", 1, 144, 0.101970, 0.151822, 0.826190, 0.826190),
    ("xgboost_e1", 1, 144, 0.083304, 0.157271, 0.813490, 0.813490),
    ("lstm_e1", 1, 144, 0.111667, 0.146782, 0.837538, 0.837538),
    ("ridge_e2", 1, 144, 0.095372, 0.146994, 0.837069, 0.837069),
    ("xgboost_e2", 1, 144, 0.079689, 0.151059, 0.827931, 0.827931),
    ("lstm_e2", 1, 144, 0.151730, 0.195925, 0.710541, 0.710541),
    ("persistence", 3, 118, 0.107542, 0.233371, 0.628046, 0.628046),
    ("ridge_e1", 3, 118, 0.212239, 0.308216, 0.351206, 0.351206),
    ("xgboost_e1", 3, 118, 0.207706, 0.303808, 0.369629, 0.369629),
    ("lstm_e1", 3, 118, 0.262026, 0.340767, 0.206928, 0.206928),
    ("ridge_e2", 3, 118, 0.202161, 0.297492, 0.395570, 0.395570),
    ("xgboost_e2", 3, 118, 0.215479, 0.310437, 0.341823, 0.341823),
    ("lstm_e2", 3, 118, 0.218320, 0.295951, 0.401816, 0.401816),
    ("persistence", 6, 84, 0.210952, 0.392492, 0.200674, 0.200674),
    ("ridge_e1", 6, 84, 0.306152, 0.450579, -0.053426, -0.053426),
    ("xgboost_e1", 6, 84, 0.277804, 0.389333, 0.213487, 0.213487),
    ("lstm_e1", 6, 84, 0.269113, 0.368295, 0.296192, 0.296192),
    ("ridge_e2", 6, 84, 0.290213, 0.435436, 0.016191, 0.016191),
    ("xgboost_e2", 6, 84, 0.282732, 0.398999, 0.173949, 0.173949),
    ("lstm_e2", 6, 84, 0.309740, 0.391074, 0.206438, 0.206438),
)

#: (model, horizon, 观测事件点, 观测事件段, 预测事件点, precision, recall, f1, reason)，
#: 照抄 §3 test 段；观测事件点为 0 时三项指标保持 ``None`` 且 reason=no_observed_events。
EVENT_METRICS: tuple[
    tuple[str, int, int, int, int, float | None, float | None, float | None, str | None],
    ...,
] = (
    ("persistence", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e1", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("xgboost_e1", 1, 0, 0, 1, None, None, None, "no_observed_events"),
    ("lstm_e1", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e2", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("xgboost_e2", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("lstm_e2", 1, 0, 0, 0, None, None, None, "no_observed_events"),
    ("persistence", 3, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e1", 3, 0, 0, 2, None, None, None, "no_observed_events"),
    ("xgboost_e1", 3, 0, 0, 0, None, None, None, "no_observed_events"),
    ("lstm_e1", 3, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e2", 3, 0, 0, 1, None, None, None, "no_observed_events"),
    ("xgboost_e2", 3, 0, 0, 0, None, None, None, "no_observed_events"),
    ("lstm_e2", 3, 0, 0, 0, None, None, None, "no_observed_events"),
    ("persistence", 6, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e1", 6, 0, 0, 7, None, None, None, "no_observed_events"),
    ("xgboost_e1", 6, 0, 0, 0, None, None, None, "no_observed_events"),
    ("lstm_e1", 6, 0, 0, 0, None, None, None, "no_observed_events"),
    ("ridge_e2", 6, 0, 0, 2, None, None, None, "no_observed_events"),
    ("xgboost_e2", 6, 0, 0, 0, None, None, None, "no_observed_events"),
    ("lstm_e2", 6, 0, 0, 0, None, None, None, "no_observed_events"),
)

#: test 段主对照：XGBoost 与持久性基线；Ridge / LSTM 不得作为主要泛化结论。
PRIMARY_COMPARISON: tuple[str, ...] = ("xgboost_e1", "persistence")

PRIMARY_NOTE = (
    "test 段主对照为 XGBoost 与持久性基线；Ridge / LSTM 受缺失填充与有效样本限制，"
    "不得作为主要泛化结论。"
)

#: DS-7 §11 的限制声明（摘要，原文口径不得弱化）。
LIMITATIONS: tuple[str, ...] = (
    "研究性高水位阈值 research_high_water 只由 train 段 p90 拟合，"
    "非官方警戒 / 超警标准，仅用于教学科研辅助。",
    "空值口径：观测事件点为 0 的格子，事件级 precision / recall / f1 一律为 null"
    "（reason=no_observed_events），不得填 0；有效配对样本为 0 的回归指标亦为 null。",
    "test 段在 train p90 阈值下零观测事件，因此 test 段事件级指标只返回 null，"
    "不得据此宣称模型具备事件检出能力。",
    "validation 段曾用于早停与超参数选择且只有 3 个事件段，其事件指标仅作机制验证，不作泛化结论。",
    "支持集差异：持久性基线只需要滞后观测，学习模型还依赖特征可用性；"
    "Ridge / LSTM 的输入缺失按 train 段统计量填充，真实在线场景的缺测机制若不同，"
    "本轮指标不能代表那时的表现。",
    "数据完整性局限：train / validation / test 段分别有 23.44% / 54.00% / 66.16% 的"
    "小时缺测或被插补剔除；降雨采用 Best Match 代理变量，不等于上游站点真值。",
    "全部数字都是探索性 / 教学科研结果，数据为历史回放 / 离线数据，不代表实时水情。",
)
