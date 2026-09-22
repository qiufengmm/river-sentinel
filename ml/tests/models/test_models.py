"""DS-6 模型模块契约测试（测试先行）。

本文件只使用 ``np.arange`` 造出的**合成**小时水雨序列，再经
:func:`river_sentinel_ml.features.build_hourly_features` 构造成真实列契约的特征矩阵；
不读取 ``data/processed/`` 下的真实数据、不访问网络、不写仓库文件、不依赖环境变量。

覆盖范围：

1. **特征子集**：E1 / E2 的列名、列数与顺序固定，非法实验名与缺列抛 ``ValueError``；
2. **段内协议**：``usable_h{h}`` 计数与段样本数一致，缺失段与缺标签列抛 ``ValueError``；
3. **只由 train 拟合**：改写 test 段取值后，标准化统计量、训练权重与 train 段预测逐元素不变
   （未来信息探针）；
4. **缺失处理**：Ridge / LSTM 剔除含 ``NaN`` 的样本并计数，XGBoost 原生保留 ``NaN``；
5. **零样本兜底**：有效样本为 0 时写 ``sample_count=0`` 与四项指标 ``null`` 加 ``reason``，
   不填 0、不崩溃；
6. **指标口径**：与既有 ``metrics.evaluate_forecasts`` 逐位一致；
7. **确定性**：Ridge / XGBoost / LSTM 同种子两次运行在 1e-9 内一致，LSTM 在 CPU 上训练。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.features import build_hourly_features, feature_columns
from river_sentinel_ml.metrics import evaluate_forecasts
from river_sentinel_ml.models import (
    FEATURE_SUBSETS,
    HORIZONS,
    MODEL_NAMES,
    SEEDS,
    WATER_ONLY_FEATURES,
    aggregate_metric_summaries,
    build_all_segments,
    build_segment_samples,
    evaluate_predictions,
    fit_lstm,
    fit_ridge,
    fit_xgboost,
    run_hourly_experiments,
    select_alpha,
    select_features,
    to_json_safe,
)
from river_sentinel_ml.models.sequence import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EPOCHS,
    DEFAULT_HIDDEN_SIZE,
    DEFAULT_PATIENCE,
)

TIMEZONE = "Asia/Shanghai"
START = "2022-06-01T00:00:00"
RATIOS = (0.70, 0.15, 0.15)
ROWS = 400
TRAIN_ROWS = 280
VALIDATION_ROWS = 60
TEST_ROWS = 60

#: 测试里把 LSTM 的 epoch 压到很小，只为验证契约与确定性，不代表正式运行配置。
FAST_LSTM = {"epochs": 40, "patience": 10}


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #


def _stamps(rows: int) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(START, tz=TIMEZONE), periods=rows, freq="1h")


def _synthetic_matrix(
    rows: int = ROWS,
    *,
    missing_at: tuple[int, ...] = (),
    imputed_at: tuple[int, ...] = (),
) -> pd.DataFrame:
    """Build a real 31-column feature matrix from a synthetic hourly water/rain pair."""
    index = np.arange(rows, dtype="float64")
    levels = np.round(6.5 + 0.4 * np.sin(index / 7.0) + 0.001 * index, 3)
    for position in missing_at:
        levels[position] = np.nan
    imputed = np.zeros(rows, dtype=bool)
    for position in imputed_at:
        imputed[position] = True
    water = pd.DataFrame(
        {
            "observed_at": _stamps(rows),
            "water_level_m": levels,
            "is_imputed": imputed,
            "source_record_id": [str(900000 + position) for position in range(rows)],
        }
    )
    rain = pd.DataFrame(
        {
            "observed_at": _stamps(rows),
            "rainfall_mm": np.round(np.abs(np.sin(index / 3.0)) * 1.5, 3),
            "point_count": pd.array(np.full(rows, 5, dtype="int64"), dtype="Int64"),
        }
    )
    return build_hourly_features(water, rain, split_ratios=RATIOS)


@pytest.fixture(scope="module")
def matrix() -> pd.DataFrame:
    """One shared synthetic feature matrix; every test copies before mutating."""
    return _synthetic_matrix()


def _water_columns() -> tuple[str, ...]:
    return WATER_ONLY_FEATURES


def _train_samples(frame: pd.DataFrame, columns: tuple[str, ...], horizon: int = 1):
    return build_segment_samples(frame, horizon=horizon, feature_columns=columns, segment="train")


def _scaled_test_segment(frame: pd.DataFrame, factor: float = 100.0) -> pd.DataFrame:
    """Return a copy whose test-segment feature values are rewritten wholesale."""
    altered = frame.copy()
    mask = altered["segment"] == "test"
    for column in feature_columns(altered):
        altered.loc[mask, column] = altered.loc[mask, column] * factor
    return altered


# --------------------------------------------------------------------------- #
# 1. Feature subsets
# --------------------------------------------------------------------------- #


def test_experiment_names_and_water_only_subset_are_frozen() -> None:
    assert tuple(FEATURE_SUBSETS) == ("E1", "E2")
    assert WATER_ONLY_FEATURES == (
        "water_level_t",
        "water_level_lag_1",
        "water_level_lag_2",
        "water_level_lag_3",
        "water_level_lag_6",
        "water_level_lag_12",
        "water_level_lag_24",
        "hour_of_day",
        "month",
    )
    assert len(WATER_ONLY_FEATURES) == 9
    assert len(FEATURE_SUBSETS["E2"]) == 20


def test_e2_subset_matches_feature_columns_order(matrix: pd.DataFrame) -> None:
    assert select_features(matrix, "E2") == feature_columns(matrix)
    assert select_features(matrix, "E1") == WATER_ONLY_FEATURES
    assert set(select_features(matrix, "E2")) - set(select_features(matrix, "E1")) == {
        "rain_lag_0",
        "rain_lag_1",
        "rain_lag_2",
        "rain_lag_3",
        "rain_lag_6",
        "rain_lag_12",
        "rain_cum_1",
        "rain_cum_3",
        "rain_cum_6",
        "rain_cum_12",
        "rain_cum_24",
    }


def test_select_features_rejects_unknown_experiment(matrix: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="E9"):
        select_features(matrix, "E9")


def test_select_features_rejects_a_missing_column(matrix: pd.DataFrame) -> None:
    broken = matrix.drop(columns=["rain_lag_0"])
    with pytest.raises(ValueError, match="rain_lag_0"):
        select_features(broken, "E2")


# --------------------------------------------------------------------------- #
# 2. Segment protocol
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("horizon", HORIZONS)
def test_segment_sample_counts_match_usable_flags(matrix: pd.DataFrame, horizon: int) -> None:
    columns = select_features(matrix, "E1")
    samples = build_all_segments(matrix, horizon=horizon, feature_columns=columns)
    assert tuple(samples) == ("train", "validation", "test")
    for name, segment in samples.items():
        expected = int(
            ((matrix["segment"] == name) & matrix[f"usable_h{horizon}"].astype(bool)).sum()
        )
        assert segment.rows == expected
        assert segment.usable_rows == expected
        assert len(segment.features) == expected
        assert segment.target.shape == (expected,)
    # 每段末尾 ``horizon`` 行的标签跨段，故退出可用集。
    assert samples["train"].rows == TRAIN_ROWS - horizon
    assert samples["validation"].rows == VALIDATION_ROWS - horizon
    assert samples["test"].rows == TEST_ROWS - horizon


def test_build_segment_samples_rejects_a_missing_segment(matrix: pd.DataFrame) -> None:
    train_only = matrix.loc[matrix["segment"] == "train"]
    with pytest.raises(ValueError, match="test"):
        build_segment_samples(
            train_only, horizon=1, feature_columns=WATER_ONLY_FEATURES, segment="test"
        )


def test_build_segment_samples_rejects_a_missing_label_column(matrix: pd.DataFrame) -> None:
    broken = matrix.drop(columns=["target_h3"])
    with pytest.raises(ValueError, match="target_h3"):
        build_segment_samples(
            broken, horizon=3, feature_columns=WATER_ONLY_FEATURES, segment="train"
        )


def test_build_segment_samples_rejects_an_unknown_feature_column(matrix: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="not_a_feature"):
        build_segment_samples(
            matrix,
            horizon=1,
            feature_columns=("water_level_t", "not_a_feature"),
            segment="train",
        )


def test_build_segment_samples_rejects_a_non_positive_horizon(matrix: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        build_segment_samples(
            matrix, horizon=0, feature_columns=WATER_ONLY_FEATURES, segment="train"
        )


def test_usable_flags_written_as_boolean_text_are_accepted(matrix: pd.DataFrame) -> None:
    text_flags = matrix.copy()
    for horizon in HORIZONS:
        text_flags[f"usable_h{horizon}"] = text_flags[f"usable_h{horizon}"].map(
            {True: "True", False: "False"}
        )
    samples = build_segment_samples(
        text_flags, horizon=1, feature_columns=WATER_ONLY_FEATURES, segment="train"
    )
    assert samples.rows == int(matrix.loc[matrix["segment"] == "train", "usable_h1"].sum())


# --------------------------------------------------------------------------- #
# 3. Train-only fitting and future-information probes
# --------------------------------------------------------------------------- #


def test_standardizer_and_coefficients_are_untouched_by_the_test_segment(
    matrix: pd.DataFrame,
) -> None:
    columns = select_features(matrix, "E1")
    baseline = fit_ridge(_train_samples(matrix, columns), alpha=10.0)
    altered = fit_ridge(_train_samples(_scaled_test_segment(matrix), columns), alpha=10.0)

    np.testing.assert_array_equal(baseline.standardizer.mean, altered.standardizer.mean)
    np.testing.assert_array_equal(baseline.standardizer.scale, altered.standardizer.scale)
    np.testing.assert_array_equal(baseline.coefficients, altered.coefficients)
    assert baseline.intercept == altered.intercept
    assert baseline.dropped_rows == altered.dropped_rows


def test_train_predictions_survive_a_rewritten_test_segment(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    altered_train = _train_samples(_scaled_test_segment(matrix), columns)

    baseline = fit_ridge(train, alpha=1.0)
    altered = fit_ridge(altered_train, alpha=1.0)

    np.testing.assert_array_equal(
        train.features.to_numpy(dtype="float64"), altered_train.features.to_numpy(dtype="float64")
    )
    np.testing.assert_allclose(
        baseline.predict(train.features.to_numpy(dtype="float64")),
        altered.predict(altered_train.features.to_numpy(dtype="float64")),
        rtol=0.0,
        atol=0.0,
    )


def test_xgboost_train_predictions_survive_a_rewritten_test_segment(
    matrix: pd.DataFrame,
) -> None:
    columns = select_features(matrix, "E1")
    baseline = fit_xgboost(
        _train_samples(matrix, columns),
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=SEEDS[0],
    )
    altered = fit_xgboost(
        _train_samples(_scaled_test_segment(matrix), columns),
        build_segment_samples(
            _scaled_test_segment(matrix), horizon=1, feature_columns=columns, segment="validation"
        ),
        seed=SEEDS[0],
    )
    features = _train_samples(matrix, columns).features.to_numpy(dtype="float64")
    np.testing.assert_allclose(
        baseline.predict(features), altered.predict(features), rtol=0.0, atol=0.0
    )


# --------------------------------------------------------------------------- #
# 4. Missing-value handling
# --------------------------------------------------------------------------- #


def test_ridge_drops_nan_feature_rows_and_counts_them(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    any_nan = int(train.features.isna().any(axis=1).sum())
    longest_lag_nan = int(train.features["water_level_lag_24"].isna().sum())
    # 段首 24 行的 ``water_level_lag_24`` 为 NaN，是本合成序列里唯一的缺失来源。
    assert any_nan == longest_lag_nan == 24

    forecaster = fit_ridge(train, alpha=1.0)

    assert forecaster.dropped_rows == any_nan


def test_xgboost_keeps_rows_with_missing_features(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    forecaster = fit_xgboost(
        _train_samples(matrix, columns),
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=SEEDS[0],
    )
    assert forecaster.dropped_rows == 0
    predictions = forecaster.predict(_train_samples(matrix, columns).features.to_numpy("float64"))
    assert np.isfinite(predictions).all()


def test_lstm_drops_nan_feature_rows_and_counts_them(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    expected = int(train.features.isna().any(axis=1).sum())

    forecaster = fit_lstm(
        train,
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=SEEDS[0],
        **FAST_LSTM,
    )

    assert forecaster.dropped_rows == expected


# --------------------------------------------------------------------------- #
# 5. Prediction shape / dtype / determinism
# --------------------------------------------------------------------------- #


def test_ridge_prediction_shape_and_dtype(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    forecaster = fit_ridge(train, alpha=1.0)

    predictions = forecaster.predict(train.features.to_numpy(dtype="float64"))

    assert predictions.shape == (train.rows,)
    assert predictions.dtype == np.float64


def test_xgboost_prediction_shape_and_dtype(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    forecaster = fit_xgboost(
        train,
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=SEEDS[0],
    )

    predictions = forecaster.predict(train.features.to_numpy(dtype="float64"))

    assert predictions.shape == (train.rows,)
    assert predictions.dtype == np.float64


def test_lstm_prediction_shape_and_dtype(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    forecaster = fit_lstm(
        train,
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=SEEDS[0],
        **FAST_LSTM,
    )

    predictions = forecaster.predict(train.features.to_numpy(dtype="float64"))

    assert predictions.shape == (train.rows,)
    assert predictions.dtype == np.float64
    assert forecaster.device == "cpu"


def test_ridge_is_reproducible_for_one_seed(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    features = train.features.to_numpy(dtype="float64")

    first = fit_ridge(train, alpha=1.0).predict(features)
    second = fit_ridge(train, alpha=1.0).predict(features)

    np.testing.assert_allclose(first, second, rtol=0.0, atol=1e-12)


def test_xgboost_is_reproducible_for_one_seed(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    validation = build_segment_samples(
        matrix, horizon=1, feature_columns=columns, segment="validation"
    )
    features = train.features.to_numpy(dtype="float64")

    first = fit_xgboost(train, validation, seed=1).predict(features)
    second = fit_xgboost(train, validation, seed=1).predict(features)

    np.testing.assert_allclose(first, second, rtol=0.0, atol=1e-12)


def test_lstm_is_reproducible_on_cpu_for_one_seed(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    validation = build_segment_samples(
        matrix, horizon=1, feature_columns=columns, segment="validation"
    )
    features = train.features.to_numpy(dtype="float64")

    first = fit_lstm(train, validation, seed=2, **FAST_LSTM)
    second = fit_lstm(train, validation, seed=2, **FAST_LSTM)

    assert first.device == "cpu"
    np.testing.assert_allclose(
        first.predict(features), second.predict(features), rtol=0.0, atol=1e-9
    )


def test_lstm_frozen_hyperparameters() -> None:
    assert DEFAULT_HIDDEN_SIZE == 32
    assert DEFAULT_EPOCHS <= 300
    assert DEFAULT_PATIENCE == 20
    assert DEFAULT_BATCH_SIZE == 64


def test_default_seeds_and_horizons_are_frozen() -> None:
    assert SEEDS == (20260921, 1, 2)
    assert HORIZONS == (1, 3, 6)
    assert MODEL_NAMES == ("ridge", "xgboost", "lstm")


# --------------------------------------------------------------------------- #
# 6. Metrics
# --------------------------------------------------------------------------- #


def test_evaluate_predictions_matches_the_existing_metric_implementation() -> None:
    observed = np.array([1.0, 2.0, 3.0, 4.0, np.nan])
    predicted = np.array([1.5, 2.5, 2.5, 4.5, 9.9])

    summary = evaluate_predictions(observed, predicted, horizon=1)
    reference = evaluate_forecasts(
        pd.DataFrame({"observed": observed, "prediction_h1": predicted}), horizons=(1,)
    )[1]

    assert summary.sample_count == reference.sample_count
    assert summary.mae == reference.mae
    assert summary.rmse == reference.rmse
    assert summary.r2 == reference.r2
    assert summary.nse == reference.nse
    assert summary.peak_absolute_error == reference.peak_absolute_error
    assert summary.reason is None


def test_evaluate_predictions_nulls_skill_metrics_for_a_constant_target() -> None:
    observed = np.full(5, 3.0)
    predicted = np.array([2.0, 3.0, 4.0, 3.0, 3.0])

    summary = evaluate_predictions(observed, predicted, horizon=2)

    assert summary.sample_count == 5
    assert summary.mae == pytest.approx(0.4)
    assert summary.r2 is None
    assert summary.nse is None
    assert to_json_safe(summary.as_dict())["r2"] is None


def test_evaluate_predictions_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        evaluate_predictions(np.zeros(3), np.zeros(4), horizon=1)


def test_evaluate_predictions_rejects_a_non_positive_horizon() -> None:
    with pytest.raises(ValueError):
        evaluate_predictions(np.zeros(3), np.zeros(3), horizon=0)


def test_zero_sample_fallback_is_null_with_a_reason() -> None:
    summary = evaluate_predictions(np.empty(0), np.empty(0), horizon=6)

    assert summary.sample_count == 0
    assert summary.mae is None
    assert summary.rmse is None
    assert summary.r2 is None
    assert summary.nse is None
    assert summary.reason
    payload = to_json_safe(summary.as_dict())
    assert payload["sample_count"] == 0
    assert payload["mae"] is None
    assert payload["reason"] == summary.reason


def test_aggregate_reports_mean_and_standard_deviation_across_seeds() -> None:
    identical = [evaluate_predictions(np.array([1.0, 2.0]), np.array([1.0, 2.0]), horizon=1)] * 3
    aggregate = aggregate_metric_summaries(identical)

    assert aggregate["sample_count"] == 2
    assert aggregate["mae"]["mean"] == pytest.approx(0.0)
    assert aggregate["mae"]["std"] == pytest.approx(0.0)
    assert aggregate["reason"] is None

    mixed = [
        evaluate_predictions(np.array([1.0, 2.0]), np.array([1.0, 2.0]), horizon=1),
        evaluate_predictions(np.array([1.0, 2.0]), np.array([2.0, 3.0]), horizon=1),
    ]
    spread = aggregate_metric_summaries(mixed)
    assert spread["mae"]["mean"] == pytest.approx(0.5)
    assert spread["mae"]["std"] == pytest.approx(0.5)


def test_aggregate_of_zero_samples_keeps_the_reason_and_nulls() -> None:
    empty = [evaluate_predictions(np.empty(0), np.empty(0), horizon=1)] * 3

    aggregate = aggregate_metric_summaries(empty)

    assert aggregate["sample_count"] == 0
    assert aggregate["reason"]
    assert aggregate["mae"] == {"mean": None, "std": None}
    assert aggregate["nse"] == {"mean": None, "std": None}


def test_to_json_safe_replaces_non_finite_values_and_numpy_scalars() -> None:
    payload = to_json_safe(
        {
            "nan": float("nan"),
            "pos": float("inf"),
            "neg": float("-inf"),
            "numpy_int": np.int64(7),
            "numpy_float": np.float64(1.5),
            "numpy_bool": np.bool_(True),
            "nested": [np.float64("nan"), {"deep": np.int32(3)}],
        }
    )

    assert payload["nan"] is None
    assert payload["pos"] is None
    assert payload["neg"] is None
    assert payload["numpy_int"] == 7
    assert isinstance(payload["numpy_int"], int)
    assert payload["numpy_float"] == 1.5
    assert payload["numpy_bool"] is True
    assert payload["nested"][0] is None
    assert payload["nested"][1]["deep"] == 3
    assert json.dumps(payload)  # 必须能被严格 JSON 序列化


# --------------------------------------------------------------------------- #
# 7. Ridge alpha selection
# --------------------------------------------------------------------------- #


def test_alpha_selection_returns_a_candidate_scored_on_validation(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    validation = build_segment_samples(
        matrix, horizon=1, feature_columns=columns, segment="validation"
    )

    alpha, scores = select_alpha(train, validation, alphas=(0.1, 1.0, 10.0, 100.0))

    assert alpha in (0.1, 1.0, 10.0, 100.0)
    assert set(scores) == {"0.1", "1.0", "10.0", "100.0"}
    assert all(value is None or math.isfinite(value) for value in scores.values())
    best = min(
        (value for value in scores.values() if value is not None),
        default=None,
    )
    assert best is None or scores[str(alpha)] == pytest.approx(best)


# --------------------------------------------------------------------------- #
# 8. Experiment runner
# --------------------------------------------------------------------------- #


def test_run_hourly_experiments_is_deterministic_with_one_seed(matrix: pd.DataFrame) -> None:
    first = run_hourly_experiments(
        matrix, experiments=("E1",), horizons=(1,), seeds=(20260921,), model_names=("ridge",)
    )
    second = run_hourly_experiments(
        matrix, experiments=("E1",), horizons=(1,), seeds=(20260921,), model_names=("ridge",)
    )

    assert first == second
    test_samples = build_segment_samples(
        matrix, horizon=1, feature_columns=WATER_ONLY_FEATURES, segment="test"
    )
    assert test_samples.usable_rows == TEST_ROWS - 1
    # Ridge 剔除段首 24 行 ``water_level_lag_24`` 为 NaN 的样本，评估样本因此少于可用样本。
    complete = int((~test_samples.features.isna().any(axis=1)).sum())
    assert complete == 35
    assert first["experiments"]["E1"]["models"]["ridge"]["h1"]["test"]["sample_count"] == complete


def test_run_hourly_experiments_reports_a_zero_sample_segment() -> None:
    blanked = _synthetic_matrix(missing_at=tuple(range(TRAIN_ROWS + VALIDATION_ROWS, ROWS)))

    payload = run_hourly_experiments(
        blanked, experiments=("E1",), horizons=(1,), seeds=(1,), model_names=("ridge",)
    )

    test_entry = payload["experiments"]["E1"]["models"]["ridge"]["h1"]["test"]
    assert test_entry["sample_count"] == 0
    assert test_entry["mae"] == {"mean": None, "std": None}
    assert test_entry["reason"]
    serialized = json.loads(json.dumps(to_json_safe(payload)))
    assert serialized["experiments"]["E1"]["models"]["ridge"]["h1"]["test"]["mae"] == {
        "mean": None,
        "std": None,
    }
    # 严格 JSON：产物里不得出现 NaN / Infinity 字面量。
    assert "NaN" not in json.dumps(to_json_safe(payload))
    assert "Infinity" not in json.dumps(to_json_safe(payload))


def test_run_hourly_experiments_rejects_an_unknown_model(matrix: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="randomforest"):
        run_hourly_experiments(matrix, model_names=("randomforest",))


def test_prediction_sink_receives_every_segment(matrix: pd.DataFrame) -> None:
    seen: list[tuple[str, str, int, int, str, int]] = []

    def sink(experiment, model, horizon, seed, segment, frame) -> None:
        seen.append((experiment, model, horizon, seed, segment, len(frame)))

    run_hourly_experiments(
        matrix,
        experiments=("E1",),
        horizons=(1,),
        seeds=(1,),
        model_names=("ridge",),
        prediction_writer=sink,
    )

    assert [entry[4] for entry in seen] == ["train", "validation", "test"]
    assert all(entry[5] > 0 for entry in seen)


def test_empty_features_are_never_handed_to_a_model(matrix: pd.DataFrame) -> None:
    columns = select_features(matrix, "E1")
    train = _train_samples(matrix, columns)
    empty = pd.DataFrame(columns=list(columns), dtype="float64")

    ridge = fit_ridge(train, alpha=1.0).predict(empty.to_numpy(dtype="float64"))
    xgboost = fit_xgboost(
        train,
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=1,
    ).predict(empty.to_numpy(dtype="float64"))
    lstm = fit_lstm(
        train,
        build_segment_samples(matrix, horizon=1, feature_columns=columns, segment="validation"),
        seed=1,
        **FAST_LSTM,
    ).predict(empty.to_numpy(dtype="float64"))

    for predictions in (ridge, xgboost, lstm):
        assert predictions.shape == (0,)
        assert predictions.dtype == np.float64
