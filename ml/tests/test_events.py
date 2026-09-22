"""Contract tests for research high-water events, event metrics and peak errors.

全部上证阈值为 **研究性阈值（research_high_water）**，由 train 段拟合得出，
非官方警戒 / 超警标准，仅用于教学科研辅助。
"""

import copy
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.events import (
    EventMetrics,
    EventSegment,
    PeakError,
    earliest_warning_lead,
    evaluate_event_metrics,
    event_segments,
    fit_research_high_water_threshold,
    label_research_high_water,
    peak_lag_hours,
    peak_underestimation,
)

TIMEZONE = "UTC"


def _times(count: int, start: str = "2024-06-01T00:00:00") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=count, freq="h", tz=TIMEZONE)


def _frame(levels, *, segments=("train",), imputed=None) -> pd.DataFrame:
    count = len(levels)
    payload = {
        "observed_at": _times(count),
        "segment": list(segments[:1]) * count,
        "water_level_t": pd.Series(levels, dtype="float64"),
        "is_imputed_t": pd.Series(imputed or [False] * count, dtype=bool),
    }
    return pd.DataFrame(payload)


def _mixed_frame() -> pd.DataFrame:
    """10 行的一段帧：train 前 5 行、validation 后 5 行，便于做分位与泄漏检查。"""
    return pd.DataFrame(
        {
            "observed_at": _times(10),
            "segment": ["train"] * 5 + ["validation"] * 5,
            "water_level_t": pd.Series(
                [1.0, 2.0, 3.0, 4.0, 5.0, 60.0, 70.0, 80.0, 90.0, 100.0],
                dtype="float64",
            ),
            "is_imputed_t": pd.Series([False] * 10, dtype=bool),
        }
    )


# --------------------------------------------------------------------------- #
# 阈值拟合
# --------------------------------------------------------------------------- #


def test_threshold_is_fitted_on_the_train_segment_only():
    frame = _mixed_frame()

    trained = fit_research_high_water_threshold(frame, quantile=0.90)

    expected = float(np.quantile(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 0.90))
    assert trained == pytest.approx(expected)


def test_threshold_does_not_change_when_other_segments_are_rewritten():
    frame = _mixed_frame()
    baseline = fit_research_high_water_threshold(frame, quantile=0.90)

    mutated = frame.copy()
    mutated.loc[mutated["segment"] != "train", "water_level_t"] = [9.0, 9.0, 9.0, 9.0, 9.0]
    mutated_train_prefix = 5.0
    assert mutated.loc[4, "water_level_t"] == pytest.approx(mutated_train_prefix)

    assert fit_research_high_water_threshold(mutated, quantile=0.90) == pytest.approx(baseline)


def test_threshold_excludes_missing_and_imputed_observations():
    # train 段有效观测为 1..8（9 被插补、10 缺失）→ p90 位置 = 0.9 * 7 = 6.3 → 7 + 0.3 = 7.3
    frame = _frame(
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, np.nan], imputed=[False] * 8 + [True, False]
    )

    threshold = fit_research_high_water_threshold(frame, quantile=0.90)

    assert threshold == pytest.approx(7.3)
    assert threshold != pytest.approx(float(np.quantile(np.arange(1.0, 10.0), 0.90)))


def test_threshold_raises_when_train_has_no_usable_observation():
    frame = _frame([np.nan, 1.0], imputed=[False, True])

    with pytest.raises(ValueError, match="train"):
        fit_research_high_water_threshold(frame)


def test_threshold_raises_on_missing_columns_or_bad_quantile():
    frame = _frame([1.0, 2.0])

    with pytest.raises(ValueError, match="columns"):
        fit_research_high_water_threshold(frame.drop(columns=["water_level_t"]))
    with pytest.raises(ValueError, match="quantile"):
        fit_research_high_water_threshold(frame, quantile=1.5)


# --------------------------------------------------------------------------- #
# 事件标注与事件段
# --------------------------------------------------------------------------- #


def test_label_treats_missing_as_false_and_keeps_boundary_inclusive():
    levels = np.array([7.0, 7.18, 7.19, np.nan, 6.0])

    mask = label_research_high_water(levels, 7.18)

    assert mask.tolist() == [False, True, True, False, False]
    assert mask.dtype == np.bool_


def test_label_rejects_negative_or_non_finite_threshold():
    levels = np.array([1.0, 2.0])

    with pytest.raises(ValueError, match="threshold"):
        label_research_high_water(levels, -1.0)
    with pytest.raises(ValueError, match="threshold"):
        label_research_high_water(levels, float("nan"))


def test_event_segments_merges_consecutive_points_and_single_points():
    mask = np.array([False, True, True, False, True, False, True, True, True])

    segments = event_segments(mask, _times(len(mask)))

    assert [(item.start_index, item.end_index) for item in segments] == [(1, 2), (4, 4), (6, 8)]
    assert [item.length for item in segments] == [2, 1, 3]
    assert segments[0].start_time == pd.Timestamp("2024-06-01T01:00:00", tz=TIMEZONE)
    assert segments[0].end_time == pd.Timestamp("2024-06-01T02:00:00", tz=TIMEZONE)


def test_event_segments_handles_empty_and_all_true_masks():
    assert event_segments(np.array([], dtype=bool)) == []
    assert event_segments(np.array([False, False, False])) == []

    single = event_segments(np.array([True, True]))
    assert len(single) == 1
    assert single[0].length == 2
    assert single[0].start_time is None


def test_event_segments_rejects_non_boolean_input():
    with pytest.raises(ValueError, match="boolean"):
        event_segments(np.array([1, 0, 1]))


# --------------------------------------------------------------------------- #
# 事件级指标
# --------------------------------------------------------------------------- #


def test_event_metrics_match_hand_computed_precision_recall_f1():
    # 观测 = [T, T, F, F, T]（3 点、2 段）；预测 = [T, F, T, F, F]（2 点）
    # tp = 1（位置 0）、fp = 1（位置 2）、fn = 2（位置 1、4）
    # precision = 1/2 = 0.5、recall = 1/3、f1 = 2*0.5*(1/3)/(0.5+1/3) = 0.4
    observed = np.array([True, True, False, False, True])
    predicted = np.array([True, False, True, False, False])

    metrics = evaluate_event_metrics(observed, predicted)

    assert isinstance(metrics, EventMetrics)
    assert (metrics.tp, metrics.fp, metrics.fn) == (1, 1, 2)
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(1 / 3)
    assert metrics.f1 == pytest.approx(0.4)
    assert metrics.observed_event_points == 3
    assert metrics.observed_event_segments == 2
    assert metrics.predicted_event_points == 2
    assert metrics.predicted_event_segments == 2
    assert metrics.reason is None


def test_event_metrics_are_none_when_no_observed_event_exists():
    observed = np.array([False, False, False])
    predicted = np.array([True, False, True])

    metrics = evaluate_event_metrics(observed, predicted)

    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1 is None
    assert metrics.reason == "no_observed_events"
    assert metrics.observed_event_points == 0
    assert metrics.tp == 0
    # 禁止用 0.0 冒充无法计算的指标
    assert 0.0 not in (metrics.precision, metrics.recall, metrics.f1)


def test_event_metrics_are_all_none_without_predicted_event():
    # 有观测事件但一条预测事件都没有：precision 分母为 0、F1 失去定义，
    # 因此三项都写 None（用 fn 表达"全部漏报"），不得用 0.0 代替。
    observed = np.array([True, True, False])
    predicted = np.array([False, False, False])

    metrics = evaluate_event_metrics(observed, predicted)

    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1 is None
    assert metrics.reason == "no_predicted_events"
    assert metrics.fn == 2
    assert metrics.predicted_event_points == 0


def test_event_metrics_rejects_mismatched_lengths_or_non_boolean():
    with pytest.raises(ValueError, match="length"):
        evaluate_event_metrics(np.array([True, False]), np.array([True]))
    with pytest.raises(ValueError, match="boolean"):
        evaluate_event_metrics(np.array([1, 0]), np.array([1, 0]))


# --------------------------------------------------------------------------- #
# 峰值误差与滞后
# --------------------------------------------------------------------------- #


def test_peak_underestimation_is_measured_at_the_observed_peak():
    # 观测 = [1, 5, 3, NaN]，预测 = [1.5, 4.0, 3.2, 2.0]（第 4 行观测缺失，成对剔除）
    # 观测峰值 = 5.0（01:00），该时刻预测 = 4.0 → 低估量 = 1.0
    # 最大绝对误差 = max(0.5, 1.0, 0.2) = 1.0，同样落在 01:00，方向为低估
    times = _times(4)
    observed = np.array([1.0, 5.0, 3.0, np.nan])
    prediction = np.array([1.5, 4.0, 3.2, 2.0])

    peak = peak_underestimation(observed, prediction, times)

    assert isinstance(peak, PeakError)
    assert peak.observed_peak == pytest.approx(5.0)
    assert peak.observed_peak_time == pd.Timestamp("2024-06-01T01:00:00", tz=TIMEZONE)
    assert peak.prediction_at_peak == pytest.approx(4.0)
    assert peak.underestimation == pytest.approx(1.0)
    assert peak.max_absolute_error == pytest.approx(1.0)
    assert peak.max_absolute_error_time == pd.Timestamp("2024-06-01T01:00:00", tz=TIMEZONE)
    assert peak.max_absolute_error_direction == "under"


def test_peak_underestimation_reports_overestimation_direction_and_empty_input():
    # 观测峰值 2.0 位于 01:00、该时刻预测 2.1 → 低估量为 -0.1（偏高）
    # 最大绝对误差出现在 00:00：|1.0 - 3.0| = 2.0，方向为高估
    times = _times(2)
    peak = peak_underestimation(np.array([1.0, 2.0]), np.array([3.0, 2.1]), times)

    assert peak.max_absolute_error == pytest.approx(2.0)
    assert peak.max_absolute_error_direction == "over"
    assert peak.underestimation == pytest.approx(-0.1)

    empty = peak_underestimation(np.array([]), np.array([]), pd.DatetimeIndex([], tz=TIMEZONE))
    assert empty.observed_peak is None
    assert empty.max_absolute_error is None
    assert empty.max_absolute_error_direction is None


def test_peak_lag_hours_is_positive_negative_or_none():
    times = _times(5)
    # 观测峰值在 01:00、预测峰值在 03:00 → 滞后 +2 小时
    assert (
        peak_lag_hours(
            np.array([1.0, 9.0, 2.0, 2.0, 2.0]), np.array([1.0, 2.0, 2.0, 9.5, 2.0]), times
        )
        == 2
    )
    # 预测峰值早于观测峰值 1 小时 → 负值（提前，不是滞后）
    assert peak_lag_hours(np.array([1.0, 2.0, 9.0]), np.array([1.0, 9.5, 2.0]), _times(3)) == -1
    # 任一序列全缺失 → None
    assert peak_lag_hours(np.array([np.nan, np.nan]), np.array([1.0, 2.0]), _times(2)) is None
    assert peak_lag_hours(np.array([1.0, 2.0]), np.array([np.nan, np.nan]), _times(2)) is None


def test_peak_lag_hours_accepts_series_with_datetime_index():
    index = _times(4)
    observed = pd.Series([1.0, 2.0, 7.0, 2.0], index=index)
    prediction = pd.Series([1.0, 7.5, 2.0, 2.0], index=index)

    assert peak_lag_hours(observed, prediction) == -1


# --------------------------------------------------------------------------- #
# 提前量
# --------------------------------------------------------------------------- #


def test_earliest_warning_lead_counts_hours_before_the_event_start():
    # 发布时间 23:00 / 00:00 / 01:00 / 02:00 / 03:00，对应目标时刻 +1 小时
    # 事件窗口为目标时刻 02:00 ~ 04:00；其中预测达阈值的行最早发布于 01:00
    # → 提前量 = 02:00 - 01:00 = 1 小时（23:00 那一行预测的是 00:00，不属本事件窗口）
    times = pd.date_range("2024-05-31T23:00:00", periods=5, freq="h", tz=TIMEZONE)
    issue_times = times
    target_times = times + pd.Timedelta(hours=1)
    predictions = np.array([7.5, 7.4, 7.3, 7.2, 6.0])
    event = EventSegment(
        start_index=0,
        end_index=2,
        start_time=pd.Timestamp("2024-06-01T02:00:00", tz=TIMEZONE),
        end_time=pd.Timestamp("2024-06-01T04:00:00", tz=TIMEZONE),
    )

    lead = earliest_warning_lead(
        event,
        issue_times=issue_times,
        target_times=target_times,
        predictions=predictions,
        threshold=7.0,
    )

    assert lead.lead_hours == 1
    assert lead.reason is None
    assert lead.issue_time == pd.Timestamp("2024-06-01T01:00:00", tz=TIMEZONE)


def test_earliest_warning_lead_is_none_without_early_crossing():
    times = pd.date_range("2024-05-31T23:00:00", periods=4, freq="h", tz=TIMEZONE)
    event = EventSegment(
        start_index=0,
        end_index=1,
        start_time=pd.Timestamp("2024-06-01T02:00:00", tz=TIMEZONE),
        end_time=pd.Timestamp("2024-06-01T03:00:00", tz=TIMEZONE),
    )

    # 预测始终低于阈值
    none = earliest_warning_lead(
        event,
        issue_times=times,
        target_times=times + pd.Timedelta(hours=1),
        predictions=np.array([6.0, 6.1, 6.2, 6.3]),
        threshold=7.0,
    )
    assert none.lead_hours is None
    assert none.reason == "no_prediction_above_threshold"

    # 仅在事件发生之后（发布时间不早于事件开始时刻）才越过阈值 → 没有提前量
    late = earliest_warning_lead(
        event,
        issue_times=times,
        target_times=times + pd.Timedelta(hours=1),
        predictions=np.array([6.0, 6.1, 6.2, 7.4]),
        threshold=7.0,
    )
    assert late.lead_hours is None
    assert late.reason == "crossing_after_event_start"
    assert late.issue_time == pd.Timestamp("2024-06-01T02:00:00", tz=TIMEZONE)


# --------------------------------------------------------------------------- #
# DS-7 证据脚本回归：填充口径 / 重点窗口 / 输入完整性
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
DS7_SCRIPT = REPO_ROOT / "scripts" / "run_hourly_error_analysis.py"
DS7_FOCUS_WINDOW = ("2022-09-30T17:00:00+08:00", "2022-09-30T23:00:00+08:00")
DS7_SEED = "1"


def _load_ds7():
    """按路径加载 DS-7 脚本（它不是包模块）。"""
    spec = importlib.util.spec_from_file_location("ds7_hourly_error_analysis", DS7_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules，脚本内的 dataclass 才能解析自己的模块名。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ds7():
    if not DS7_SCRIPT.exists():
        pytest.skip("当前工作树没有 DS-7 脚本")
    return _load_ds7()


def _ds7_grid(periods: int = 3) -> pd.DataFrame:
    """合成 DS-4b 风格网格：E1 用 a/b 两列，E2 多用一列 c。"""
    rows: list[dict[str, object]] = []
    for day, segment in enumerate(("train", "validation", "test")):
        base = pd.Timestamp("2024-01-01T00:00:00", tz=TIMEZONE) + pd.Timedelta(days=day)
        for step in range(periods):
            rows.append(
                {
                    "observed_at": base + pd.Timedelta(hours=step),
                    "segment": segment,
                    "water_level_t": 6.0 + 0.01 * step,
                    "is_imputed_t": False,
                    "a": 1.0 if step != 1 else np.nan,
                    "b": 1.0 if step != 2 else np.nan,
                    "c": 1.0 if step != 2 else np.nan,
                }
            )
    return pd.DataFrame(rows)


def _ds7_frames(ds7) -> dict:
    """全部 6 个学习模型 x 3 horizon x 3 段的目标行，取值恒定。"""
    grid = _ds7_grid()
    frames = {}
    for experiment in ds7.EXPERIMENTS:
        for stem in ds7.MODEL_STEMS:
            for horizon in ds7.HORIZONS:
                for segment in ds7.SEGMENTS:
                    part = grid[grid["segment"] == segment]
                    anchors = pd.DatetimeIndex(part["observed_at"])
                    count = len(anchors)
                    frames[ds7.SeriesKey(f"{stem}_{experiment}", horizon, segment)] = pd.DataFrame(
                        {
                            "target": anchors + pd.Timedelta(hours=horizon),
                            "anchor": anchors,
                            "observed": np.full(count, 6.0),
                            "prediction": np.full(count, 6.05),
                        }
                    )
    return frames


def _ds7_experiments(
    *,
    e2_ridge_cells: int | None = None,
    xgboost_imputed: int = 0,
    drop_feature_columns: bool = False,
) -> dict:
    """合成 DS-6 风格产物：E1 两列、E2 三列，缺失模式见 ``_ds7_grid``。"""
    experiments: dict = {"seeds": [1], "experiments": {}}
    for name, columns, cells in (("E1", ["a", "b"], 2), ("E2", ["a", "b", "c"], 3)):
        block: dict = {"models": {}}
        if not drop_feature_columns:
            block["feature_columns"] = columns
        for stem in ("ridge", "xgboost", "lstm"):
            horizons = {}
            for horizon in (1, 3, 6):
                segments = {}
                for segment in ("train", "validation", "test"):
                    if stem == "xgboost":
                        imputed_rows = imputed_cells = xgboost_imputed
                    else:
                        imputed_rows, imputed_cells = 2, cells
                        if name == "E2" and stem == "ridge" and e2_ridge_cells is not None:
                            imputed_cells = e2_ridge_cells
                    segments[segment] = {
                        "usable_rows": 3,
                        "rows_with_missing_features": 2,
                        "imputed_rows": imputed_rows,
                        "imputed_cells": imputed_cells,
                    }
                horizons[f"h{horizon}"] = segments
            block["models"][stem] = horizons
        experiments["experiments"][name] = block
    return experiments


def test_ds7_focus_window_is_the_seven_hour_window(ds7):
    assert ds7.FOCUS_WINDOW == DS7_FOCUS_WINDOW
    assert ds7.FOCUS_WINDOW[0] < ds7.FOCUS_WINDOW[1]


def test_ds7_imputation_is_reported_per_experiment_and_model(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    profile = ds7.imputation_profile(frames, grid, _ds7_experiments())

    e1 = profile["experiments"]["E1"]
    e2 = profile["experiments"]["E2"]
    assert e1["feature_count"] == 2
    assert e2["feature_count"] == 3
    assert e1["feature_columns"] == ["a", "b"]
    assert e2["feature_columns"] == ["a", "b", "c"]
    assert e1["models"] == ["ridge_E1", "xgboost_E1", "lstm_E1"]

    ridge_e1 = e1["cells"]["test"]["h1"]["ridge_E1"]
    ridge_e2 = e2["cells"]["test"]["h1"]["ridge_E2"]
    xgb_e1 = e1["cells"]["test"]["h1"]["xgboost_E1"]
    lstm_e2 = e2["cells"]["test"]["h1"]["lstm_E2"]

    # E1 只按 2 列统计、E2 按 3 列统计，两者不得合并成一条记录
    assert ridge_e1["feature_count"] == 2
    assert ridge_e1["imputed_cells"] == 2
    assert ridge_e1["rows_with_missing_features"] == 2
    assert ridge_e1["input_cells"] == 6
    assert ridge_e2["feature_count"] == 3
    assert ridge_e2["imputed_cells"] == 3
    assert ridge_e2["input_cells"] == 9
    assert lstm_e2["imputed_cells"] == 3

    # XGBoost 原生缺失处理：填充计数恒为 0，缺失只以 missing_* 记录
    assert xgb_e1["imputed_rows"] == 0
    assert xgb_e1["imputed_cells"] == 0
    assert xgb_e1["missing_feature_cells"] == 2
    assert xgb_e1["native_missing_handling"] is True
    assert xgb_e1["missing_handling"] == "native tree handling (missing=NaN)"

    assert profile["cross_check"]["cells"] == ds7.EXPECTED_PREDICTION_FILES
    assert profile["cross_check"]["max_abs_difference"] == 0

    table = ds7._imputation_table(profile, "E1")
    assert "Ridge / XGBoost / LSTM" not in table
    assert "XGBoost E1" in table
    # 报告显示中文标签，JSON 仍保留英文原值，便于与 DS-6 产物逐字核对
    assert "树原生缺失处理" in table
    assert "native tree handling (missing=NaN)" not in table
    assert xgb_e1["missing_handling"] == "native tree handling (missing=NaN)"
    assert xgb_e1["missing_handling_label"] == "树原生缺失处理"


def test_ds7_imputation_mismatch_fails_fast(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)

    with pytest.raises(ValueError, match="填充统计与原始特征矩阵不一致"):
        ds7.imputation_profile(frames, grid, _ds7_experiments(e2_ridge_cells=5))
    with pytest.raises(ValueError, match="原生缺失处理"):
        ds7.imputation_profile(frames, grid, _ds7_experiments(xgboost_imputed=1))
    with pytest.raises(ValueError, match="feature_columns"):
        ds7.imputation_profile(frames, grid, _ds7_experiments(drop_feature_columns=True))

    incomplete = _ds7_frames(ds7)
    del incomplete[ds7.SeriesKey("lstm_E2", 6, "test")]
    with pytest.raises(ValueError, match="lstm_E2"):
        ds7.imputation_profile(incomplete, grid, _ds7_experiments())


def test_ds7_load_model_frames_requires_all_54(ds7, tmp_path: Path):
    for experiment in ds7.EXPERIMENTS:
        for stem in ds7.MODEL_STEMS:
            for horizon in ds7.HORIZONS:
                for segment in ds7.SEGMENTS:
                    target = tmp_path / experiment / f"{stem}_h{horizon}_{segment}.csv"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        "observed_at,observed,prediction\n2024-01-01T00:00:00+00:00,6.0,6.1\n",
                        encoding="utf-8",
                    )

    frames = ds7.load_model_frames(tmp_path)
    assert ds7.EXPECTED_PREDICTION_FILES == 54
    assert len(frames) == 54

    removed = tmp_path / "E2" / "lstm_h6_test.csv"
    removed.unlink()
    with pytest.raises(ValueError, match="lstm_h6_test.csv") as error:
        ds7.load_model_frames(tmp_path)
    assert "1 / 54" in str(error.value)


def _ds7_phase_frames(ds7, observed, prediction, start: str = "2024-01-01T00:00:00") -> dict:
    anchors = pd.date_range(start, periods=len(observed), freq="h", tz=TIMEZONE)
    frames = {}
    for model in ds7.ALL_MODELS:
        for horizon in ds7.HORIZONS:
            frames[ds7.SeriesKey(model, horizon, "test")] = pd.DataFrame(
                {
                    "target": anchors,
                    "anchor": anchors - pd.Timedelta(hours=horizon),
                    "observed": np.asarray(observed, dtype="float64"),
                    "prediction": np.asarray(prediction, dtype="float64"),
                }
            )
    return frames


def test_ds7_phase_summary_uses_focus_window_only(ds7):
    window = ("2024-01-01T02:00:00+00:00", "2024-01-01T04:00:00+00:00")
    observed = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    prediction = [1.1, 2.1, 3.1, 4.1, 5.1, 6.1, 7.1]

    windowed = ds7.phase_summary(_ds7_phase_frames(ds7, observed, prediction), window)

    # 窗口外的数据被大幅改写后，窗口统计必须完全不变
    tampered = _ds7_phase_frames(ds7, [999.0, 998.0, 3.0, 4.0, 5.0, -100.0, -200.0], prediction)
    assert ds7.phase_summary(tampered, window) == windowed

    # 与整段统计不同，证明真的按窗口过滤，而不是用整段样本
    full = ds7.phase_summary(
        _ds7_phase_frames(ds7, observed, prediction),
        ("2024-01-01T00:00:00+00:00", "2024-01-01T06:00:00+00:00"),
    )
    assert full != windowed

    assert windowed["window"] == {"start": window[0], "end": window[1], "segment": "test"}
    cell = windowed["models"]["persistence"][str(ds7.HORIZONS[0])]
    assert cell["rising"]["sample_count"] == 2
    assert cell["falling"]["sample_count"] == 0


def test_ds7_event_cell_is_null_without_observed_events(ds7):
    times = pd.date_range("2024-01-01T00:00:00", periods=3, freq="h", tz=TIMEZONE)
    frame = pd.DataFrame(
        {
            "target": times,
            "anchor": times,
            "observed": np.array([6.0, 6.1, 6.2]),
            "prediction": np.array([7.5, 7.6, 7.7]),
        }
    )
    cell = ds7.event_cell(frame, 7.0)
    assert cell["precision"] is None
    assert cell["recall"] is None
    assert cell["f1"] is None
    assert cell["reason"] == "no_observed_events"


def test_ds7_common_support_requires_all_seven_models(ds7):
    frames, _, _ = _ds7_metric_case(ds7)
    ds7._require_complete_support(frames, "test", ds7.HORIZONS[0])

    without_lstm = dict(frames)
    del without_lstm[ds7.SeriesKey("lstm_E2", ds7.HORIZONS[0], "test")]
    with pytest.raises(ValueError, match="lstm_E2"):
        ds7._require_complete_support(without_lstm, "test", ds7.HORIZONS[0])


def _ds7_metric_case(ds7, observed_values: list[float] | None = None) -> tuple[dict, dict, dict]:
    """合成齐全的 54 + 9 个序列，报告值先与脚本复算一致，供篡改测试使用。"""
    observed = np.asarray(
        [1.0, 2.0, 3.0, 4.0] if observed_values is None else observed_values, dtype="float64"
    )
    prediction = observed + 0.1
    frames = {}
    experiments: dict = {"seeds": [1], "experiments": {}}
    for experiment in ds7.EXPERIMENTS:
        models: dict = {}
        for stem in ds7.MODEL_STEMS:
            horizons: dict = {}
            for horizon in ds7.HORIZONS:
                segments: dict = {}
                for segment in ds7.SEGMENTS:
                    anchors = pd.date_range("2024-01-01T00:00:00", periods=len(observed), freq="h")
                    frames[ds7.SeriesKey(f"{stem}_{experiment}", horizon, segment)] = pd.DataFrame(
                        {
                            "target": anchors,
                            "anchor": anchors,
                            "observed": observed,
                            "prediction": prediction,
                        }
                    )
                    cell = ds7.regression_cell(
                        frames[ds7.SeriesKey(f"{stem}_{experiment}", horizon, segment)], horizon
                    )
                    segments[segment] = {
                        "per_seed": {
                            DS7_SEED: {
                                "sample_count": cell["sample_count"],
                                "mae": cell["mae"],
                                "rmse": cell["rmse"],
                                "r2": cell["r2"],
                                "nse": cell["nse"],
                            }
                        }
                    }
                horizons[f"h{horizon}"] = segments
            models[stem] = horizons
        experiments["experiments"][experiment] = {
            "feature_columns": ["a"],
            "models": models,
        }

    baseline: dict = {"metrics": {}}
    for segment in ds7.SEGMENTS:
        anchors = pd.date_range("2024-01-01T00:00:00", periods=len(observed), freq="h")
        horizons = {}
        for horizon in ds7.HORIZONS:
            frames[ds7.SeriesKey(ds7.PERSISTENCE, horizon, segment)] = pd.DataFrame(
                {
                    "target": anchors,
                    "anchor": anchors,
                    "observed": observed,
                    "prediction": prediction,
                }
            )
            cell = ds7.regression_cell(
                frames[ds7.SeriesKey(ds7.PERSISTENCE, horizon, segment)], horizon
            )
            horizons[str(horizon)] = {
                "persistence": {
                    "sample_count": cell["sample_count"],
                    "mae": cell["mae"],
                    "rmse": cell["rmse"],
                    "r2": cell["r2"],
                    "nse": cell["nse"],
                }
            }
        baseline["metrics"][segment] = {"horizons": horizons}
    return frames, experiments, baseline


def test_ds7_cross_check_passes_and_fails_fast(ds7):
    frames, experiments, baseline = _ds7_metric_case(ds7)
    result = ds7._cross_check(frames, experiments, baseline)
    assert result["model_cells"]["count"] == ds7.EXPECTED_PREDICTION_FILES
    assert result["persistence_cells"]["count"] == len(ds7.HORIZONS) * len(ds7.SEGMENTS)
    assert result["model_cells"]["max_abs_difference"] == 0
    assert result["persistence_cells"]["max_abs_difference"] == 0

    bad_count = copy.deepcopy(experiments)
    bad_count["experiments"]["E1"]["models"]["ridge"]["h1"]["test"]["per_seed"][DS7_SEED][
        "sample_count"
    ] = 3
    with pytest.raises(ValueError, match="样本数不一致"):
        ds7._cross_check(frames, bad_count, baseline)

    bad_metric = copy.deepcopy(experiments)
    bad_metric["experiments"]["E2"]["models"]["lstm"]["h6"]["train"]["per_seed"][DS7_SEED][
        "mae"
    ] = 9.0
    with pytest.raises(ValueError, match="指标差异"):
        ds7._cross_check(frames, bad_metric, baseline)

    missing_cell = copy.deepcopy(experiments)
    del missing_cell["experiments"]["E1"]["models"]["xgboost"]["h3"]["validation"]
    with pytest.raises(ValueError, match="E1/xgboost/h3/validation"):
        ds7._cross_check(frames, missing_cell, baseline)

    missing_series = dict(frames)
    del missing_series[ds7.SeriesKey("lstm_E2", 6, "test")]
    with pytest.raises(ValueError, match="缺少预测序列"):
        ds7._cross_check(missing_series, experiments, baseline)


def test_ds7_imputation_usable_rows_mismatch_fails(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    experiments = _ds7_experiments()
    experiments["experiments"]["E1"]["models"]["ridge"]["h1"]["test"]["usable_rows"] = 2
    with pytest.raises(ValueError, match="样本数与 DS-6 产物不一致"):
        ds7.imputation_profile(frames, grid, experiments)


def test_ds7_imputation_missing_rows_mismatch_fails(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    experiments = _ds7_experiments()
    experiments["experiments"]["E2"]["models"]["xgboost"]["h6"]["train"][
        "rows_with_missing_features"
    ] = 1
    with pytest.raises(ValueError, match="缺失行数与 DS-6 产物不一致"):
        ds7.imputation_profile(frames, grid, experiments)


def test_ds7_imputation_cell_missing_fails(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    experiments = _ds7_experiments()
    del experiments["experiments"]["E2"]["models"]["lstm"]["h3"]["validation"]
    with pytest.raises(ValueError, match="E2/lstm/h3/validation"):
        ds7.imputation_profile(frames, grid, experiments)


def test_ds7_imputation_field_missing_fails(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    experiments = _ds7_experiments()
    del experiments["experiments"]["E1"]["models"]["ridge"]["h1"]["train"]["imputed_cells"]
    with pytest.raises(ValueError, match="imputed_cells"):
        ds7.imputation_profile(frames, grid, experiments)


def test_ds7_imputation_unknown_feature_column_fails(ds7):
    grid = _ds7_grid()
    frames = _ds7_frames(ds7)
    experiments = _ds7_experiments()
    experiments["experiments"]["E2"]["feature_columns"] = ["a", "b", "c", "not_in_grid"]
    with pytest.raises(ValueError, match="不在特征矩阵中"):
        ds7.imputation_profile(frames, grid, experiments)


def test_ds7_cross_check_persistence_failures(ds7):
    frames, experiments, baseline = _ds7_metric_case(ds7)

    missing_cell = copy.deepcopy(baseline)
    del missing_cell["metrics"]["test"]["horizons"]["1"]["persistence"]
    with pytest.raises(ValueError, match="DS-5b 产物缺少"):
        ds7._cross_check(frames, experiments, missing_cell)

    bad_count = copy.deepcopy(baseline)
    bad_count["metrics"]["validation"]["horizons"]["3"]["persistence"]["sample_count"] = 2
    with pytest.raises(ValueError, match="样本数不一致"):
        ds7._cross_check(frames, experiments, bad_count)

    bad_metric = copy.deepcopy(baseline)
    bad_metric["metrics"]["train"]["horizons"]["6"]["persistence"]["rmse"] = 9.0
    with pytest.raises(ValueError, match="指标差异"):
        ds7._cross_check(frames, experiments, bad_metric)

    missing_series = dict(frames)
    del missing_series[ds7.SeriesKey(ds7.PERSISTENCE, 3, "train")]
    with pytest.raises(ValueError, match="缺少持久性序列"):
        ds7._cross_check(missing_series, experiments, baseline)


def test_ds7_cross_check_tolerates_null_r2_when_target_variance_is_zero(ds7):
    # 观测恒定 → SS_tot = 0 → R² / NSE 合法为 None（见 ml/tests/test_metrics.py）
    frames, experiments, baseline = _ds7_metric_case(ds7, [2.0, 2.0, 2.0, 2.0])
    constant = ds7.SeriesKey("ridge_E1", 1, "test")
    assert ds7.regression_cell(frames[constant], 1)["r2"] is None

    result = ds7._cross_check(frames, experiments, baseline)

    assert result["persistence_cells"]["count"] == len(ds7.HORIZONS) * len(ds7.SEGMENTS)
    assert result["model_cells"]["max_abs_difference"] == 0
    assert result["persistence_cells"]["max_abs_difference"] == 0


def test_ds7_cross_check_prefers_primary_seed(ds7):
    frames, experiments, baseline = _ds7_metric_case(ds7)
    experiments["seeds"] = [99, 1]
    experiments["primary_seed"] = 1

    result = ds7._cross_check(frames, experiments, baseline)

    assert result["first_seed"] == DS7_SEED
    assert result["seed_source"] == "primary_seed"
    assert result["model_cells"]["count"] == ds7.EXPECTED_PREDICTION_FILES
