"""DS-5b 小时基线脚本的契约测试。

全部使用 ``tmp_path`` 上的**合成**序列，不读取 ``data/`` 下的真实数据，
也不把任何绝对路径写进测试逻辑（仓库根目录由 ``__file__`` 推导）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_hourly_baseline import (  # noqa: E402
    HORIZONS,
    MODEL_PERSISTENCE,
    MODEL_TRAIN_MEAN,
    NO_VALID_PAIRS_REASON,
    SEGMENT_NAMES,
    build_forecast_frame,
    constant_prediction_frame,
    evaluate_horizon,
    load_hourly_frame,
    main,
    render_metrics_json,
    render_report,
    run_hourly_baseline,
    sha256_of_file,
    split_hourly_frame,
    train_mean_value,
    validate_hourly_frame,
)

START = "2024-06-01T00:00:00+08:00"
SCORED_KEYS = ("mae", "rmse", "r2", "nse", "peak_absolute_error")
EXPECTED_TOP_LEVEL_KEYS = (
    "schema_version",
    "horizons",
    "source",
    "split",
    "metrics",
    "baselines",
    "cross_check",
    "declarations",
)


def _frame(
    values: Sequence[float | None],
    imputed: Iterable[int] = (),
    *,
    start: str = START,
) -> pd.DataFrame:
    """用合成小时值构造符合小时表契约的帧（``None`` 表示空缺桶）。"""
    imputed_positions = set(imputed)
    stamps = pd.date_range(start, periods=len(values), freq="h")
    levels = pd.Series(
        [np.nan if value is None else float(value) for value in values], dtype="float64"
    )
    return pd.DataFrame(
        {
            "observed_at": stamps,
            "water_level_m": levels,
            "is_imputed": pd.Series([index in imputed_positions for index in range(len(values))]),
            "quality_flag": pd.Series(
                [
                    "imputed" if index in imputed_positions else "normal"
                    for index in range(len(values))
                ],
                dtype="object",
            ),
            "source_record_id": pd.Series(["r"] * len(values), dtype="object"),
        }
    )


def _write_hourly_csv(path: Path, frame: pd.DataFrame) -> Path:
    """按 DS-2b 小时表的列与时间戳格式写出合成 CSV。"""
    table = pd.DataFrame(
        {
            "observed_at": [stamp.isoformat() for stamp in frame["observed_at"]],
            "water_level_m": frame["water_level_m"],
            "is_imputed": frame["is_imputed"],
            "quality_flag": frame["quality_flag"],
            "source_record_id": frame["source_record_id"],
        }
    )
    table.to_csv(path, index=False)
    return path


def _reject_constant(value: str) -> float:
    """让 ``NaN`` / ``Infinity`` 字面量在解析阶段直接失败。"""
    raise AssertionError(f"严格 JSON 不允许 {value} 字面量")


def _strings(payload: Any) -> list[str]:
    """递归收集 JSON 结构里的全部字符串。"""
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        collected: list[str] = []
        for key, value in payload.items():
            collected.extend(_strings(key))
            collected.extend(_strings(value))
        return collected
    if isinstance(payload, list):
        collected = []
        for item in payload:
            collected.extend(_strings(item))
        return collected
    return []


def test_persistence_prediction_is_the_anchor_value_of_each_row():
    forecast = build_forecast_frame(_frame([1.0, 2.0, 3.0, 4.0, 5.0]), horizons=(1, 3))

    assert np.isnan(forecast["prediction_h1"].iloc[0])
    np.testing.assert_allclose(
        forecast["prediction_h1"].to_numpy(dtype="float64")[1:], [1.0, 2.0, 3.0, 4.0]
    )
    assert forecast["prediction_h3"].iloc[:3].isna().all()
    np.testing.assert_allclose(forecast["prediction_h3"].to_numpy(dtype="float64")[3:], [1.0, 2.0])


def test_split_segments_are_contiguous_ordered_and_complete():
    frame = _frame([float(index) for index in range(20)])
    segments = split_hourly_frame(frame)

    assert list(segments) == list(SEGMENT_NAMES)
    assert [len(segments[name]) for name in SEGMENT_NAMES] == [14, 3, 3]
    combined = pd.concat([segments[name]["observed_at"] for name in SEGMENT_NAMES])
    pd.testing.assert_index_equal(
        pd.DatetimeIndex(combined), pd.DatetimeIndex(frame["observed_at"])
    )


def test_imputed_target_rows_are_excluded_and_counted():
    forecast = build_forecast_frame(_frame([float(index) for index in range(24)], imputed=(10, 11)))

    evaluation = evaluate_horizon(forecast, 1)

    assert evaluation.sample_count == 21
    assert evaluation.excluded_imputed == 2
    assert evaluation.metrics is not None
    assert evaluation.reason is None


def test_zero_valid_pairs_yields_null_metrics_and_reason():
    evaluation = evaluate_horizon(build_forecast_frame(_frame([None] * 8)), 1)

    assert evaluation.sample_count == 0
    assert evaluation.excluded_imputed == 0
    assert evaluation.metrics is None
    assert evaluation.reason == NO_VALID_PAIRS_REASON


def test_zero_valid_pairs_are_reported_as_null_in_the_payload():
    payload = run_hourly_baseline(
        _frame([float(index) for index in range(17)] + [None, None, None]),
        source_sha256="0" * 64,
    )

    cell = payload["metrics"]["test"]["horizons"]["1"][MODEL_PERSISTENCE]
    assert cell["sample_count"] == 0
    assert cell["reason"] == NO_VALID_PAIRS_REASON
    for key in SCORED_KEYS:
        assert cell[key] is None
    reloaded = json.loads(render_metrics_json(payload), parse_constant=_reject_constant)
    assert reloaded["metrics"]["test"]["horizons"]["1"][MODEL_PERSISTENCE]["mae"] is None


def test_train_mean_is_fitted_on_the_train_segment_only():
    baseline = run_hourly_baseline(
        _frame([float(index + 1) for index in range(20)]), source_sha256="0" * 64
    )
    changed_tail = [float(index + 1) for index in range(17)] + [999.0, 999.0, 999.0]
    changed = run_hourly_baseline(_frame(changed_tail), source_sha256="0" * 64)

    assert baseline["baselines"][MODEL_TRAIN_MEAN]["value"] == pytest.approx(7.5)
    assert changed["baselines"][MODEL_TRAIN_MEAN]["value"] == pytest.approx(7.5)


def test_train_mean_ignores_imputed_rows():
    payload = run_hourly_baseline(
        _frame([10.0, 10.0] + [1.0] * 18, imputed=(0, 1)), source_sha256="0" * 64
    )

    assert payload["baselines"][MODEL_TRAIN_MEAN]["calibration_rows"] == 12
    assert payload["baselines"][MODEL_TRAIN_MEAN]["value"] == pytest.approx(1.0)


def test_train_mean_value_rejects_a_train_segment_without_observations():
    with pytest.raises(ValueError):
        train_mean_value(build_forecast_frame(_frame([None] * 8)))


def test_segment_heads_keep_predictions_generated_on_the_full_sequence():
    payload = run_hourly_baseline(
        _frame([float(index) for index in range(20)]), source_sha256="0" * 64
    )

    counts = {
        name: payload["metrics"][name]["horizons"]["1"][MODEL_PERSISTENCE]["sample_count"]
        for name in SEGMENT_NAMES
    }
    assert counts == {"train": 13, "validation": 3, "test": 3}


def test_persistence_and_train_mean_share_the_same_sample_set():
    payload = run_hourly_baseline(_frame([1.0, 2.0] * 12), source_sha256="0" * 64)

    for name in SEGMENT_NAMES:
        for horizon in HORIZONS:
            cell = payload["metrics"][name]["horizons"][str(horizon)]
            assert cell[MODEL_PERSISTENCE]["sample_count"] == cell[MODEL_TRAIN_MEAN]["sample_count"]
            assert (
                cell[MODEL_PERSISTENCE]["excluded_imputed"]
                == cell[MODEL_TRAIN_MEAN]["excluded_imputed"]
            )


def test_json_is_strict_and_contains_no_absolute_path(tmp_path):
    source = _write_hourly_csv(
        tmp_path / "synthetic_hourly.csv",
        _frame([float(index) for index in range(24)], imputed=(3,)),
    )
    metrics_path = tmp_path / "metrics.json"
    report_path = tmp_path / "report.md"

    exit_code = main(
        [
            "--input",
            str(source),
            "--metrics-json",
            str(metrics_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    text = metrics_path.read_text(encoding="utf-8")
    payload = json.loads(text, parse_constant=_reject_constant)
    assert tuple(payload) == EXPECTED_TOP_LEVEL_KEYS
    assert payload["horizons"] == list(HORIZONS)
    assert str(tmp_path) not in text
    assert str(source) not in text
    for value in _strings(payload):
        assert not Path(value).is_absolute(), value


def test_two_runs_produce_identical_artifacts(tmp_path):
    source = _write_hourly_csv(
        tmp_path / "synthetic_hourly.csv",
        _frame([float(index) for index in range(24)], imputed=(5, 6)),
    )
    produced: list[tuple[bytes, bytes]] = []

    for name in ("first", "second"):
        metrics_path = tmp_path / name / "metrics.json"
        report_path = tmp_path / name / "report.md"
        assert (
            main(
                [
                    "--input",
                    str(source),
                    "--metrics-json",
                    str(metrics_path),
                    "--report",
                    str(report_path),
                ]
            )
            == 0
        )
        produced.append((metrics_path.read_bytes(), report_path.read_bytes()))

    assert produced[0] == produced[1]


def test_input_frame_and_dtypes_are_left_untouched():
    frame = _frame([float(index) for index in range(20)], imputed=(2,))
    snapshot = frame.copy(deep=True)
    dtypes = frame.dtypes.copy(deep=True)

    run_hourly_baseline(frame, source_sha256="0" * 64)

    pd.testing.assert_frame_equal(frame, snapshot)
    assert frame.dtypes.equals(dtypes)


@pytest.mark.parametrize("mutation", ["missing_column", "naive_timestamp", "unsorted"])
def test_invalid_input_frames_are_rejected(mutation):
    frame = _frame([float(index) for index in range(8)])
    if mutation == "missing_column":
        frame = frame.drop(columns=["is_imputed"])
    elif mutation == "naive_timestamp":
        frame["observed_at"] = pd.date_range("2024-06-01", periods=len(frame), freq="h")
    else:
        frame = frame.iloc[::-1].reset_index(drop=True)

    with pytest.raises(ValueError):
        validate_hourly_frame(frame)


def test_constant_prediction_frame_nulls_out_the_rows_penalised_by_missing_history():
    forecast = build_forecast_frame(_frame([1.0, 2.0, 3.0, 4.0]), horizons=(1, 3))

    filled = constant_prediction_frame(forecast, (1, 3), 9.0)

    assert np.isnan(filled["prediction_h1"].iloc[0])
    assert (filled["prediction_h1"].iloc[1:] == 9.0).all()
    assert filled["prediction_h3"].iloc[:3].isna().all()
    assert filled["prediction_h3"].iloc[3] == 9.0
    assert forecast["prediction_h1"].iloc[1] == 1.0


def test_load_hourly_frame_keeps_timezone_aware_hourly_grid(tmp_path):
    source = _write_hourly_csv(
        tmp_path / "synthetic_hourly.csv", _frame([1.0, 2.0, 3.0, 4.0], imputed=(2,))
    )

    loaded = load_hourly_frame(source)

    assert list(loaded.columns) == [
        "observed_at",
        "water_level_m",
        "is_imputed",
        "quality_flag",
        "source_record_id",
    ]
    assert isinstance(loaded["observed_at"].dtype, pd.DatetimeTZDtype)
    assert loaded["observed_at"].dt.hour.tolist() == [0, 1, 2, 3]
    assert loaded["is_imputed"].tolist() == [False, False, True, False]
    assert loaded["water_level_m"].tolist() == [1.0, 2.0, 3.0, 4.0]


def test_sha256_of_file_matches_hashlib(tmp_path):
    source = _write_hourly_csv(tmp_path / "synthetic_hourly.csv", _frame([1.0, 2.0, 3.0]))

    assert sha256_of_file(source) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_report_embeds_source_hash_and_every_declaration():
    payload = run_hourly_baseline(
        _frame([float(index) for index in range(24)], imputed=(4,)), source_sha256="a" * 64
    )

    report = render_report(payload)

    assert payload["source"]["sha256"] in report
    for name, text in payload["declarations"].items():
        assert text in report, name
    assert "1 小时 horizon 的指标偏乐观" in report
    assert "test 段限制" in report
    assert "探索性" in report
    assert "不替代官方防汛决策" in report
