"""DS-4b 小时特征与水雨时间对齐的契约测试。

本文件只使用 ``tmp_path`` 下**合成**的小时序列，不读取真实数据、不访问网络、
不依赖环境变量。测试对象是三个冻结契约：

1. :func:`river_sentinel_ml.features.load_hourly_rainfall_tables`
2. :func:`river_sentinel_ml.features.build_hourly_features`
3. :func:`river_sentinel_ml.features.feature_columns`

除功能正确性外，本文件还把三条泄漏探针固化成可重复执行的用例（未来降雨、
未来水位、测试段不影响训练段），以及跨段窗口不借用上一段末尾值的探针。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.dataset import chronological_split
from river_sentinel_ml.features import (
    build_hourly_features,
    feature_columns,
    load_hourly_rainfall_tables,
)

START = "2022-06-01T00:00:00"
RATIOS = (0.60, 0.20, 0.20)


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #


def _stamps(count: int, start: str = START) -> pd.DatetimeIndex:
    """``count`` consecutive hourly timestamps in ``Asia/Shanghai``."""
    return pd.date_range(pd.Timestamp(start, tz="Asia/Shanghai"), periods=count, freq="1h")


def _water(
    levels: object,
    *,
    imputed: object = None,
    start: str = START,
    station: bool = False,
) -> pd.DataFrame:
    """Synthetic hourly water table mirroring the persisted cata_12720 5-column shape."""
    values = np.asarray(levels, dtype="float64")
    flags = (
        np.zeros(values.size, dtype=bool) if imputed is None else np.asarray(imputed, dtype=bool)
    )
    frame = pd.DataFrame(
        {
            "observed_at": _stamps(values.size, start),
            "water_level_m": values,
            "is_imputed": flags,
            "quality_flag": np.where(flags, "imputed", "normal"),
            "source_record_id": [str(900000 + index) for index in range(values.size)],
        }
    )
    if station:
        frame["station_code"] = "cata_12720"
        frame["station_name"] = "飞云江"
    return frame


def _rainfall(
    values: object,
    *,
    counts: object = None,
    start: str = START,
) -> pd.DataFrame:
    """Synthetic rainfall table shaped exactly like ``load_hourly_rainfall_tables``."""
    series = np.asarray(values, dtype="float64")
    point_counts = (
        np.full(series.size, 5, dtype="int64")
        if counts is None
        else np.full(series.size, int(counts), dtype="int64")
    )
    return pd.DataFrame(
        {
            "observed_at": _stamps(series.size, start),
            "rainfall_mm": series,
            "point_count": pd.array(point_counts, dtype="Int64"),
        }
    )


def _write_rainfall_csv(
    path,
    rows: list[dict[str, object]],
    *,
    columns: tuple[str, ...] = (
        "observed_at",
        "precipitation_mm",
        "rain_mm",
        "point_id",
        "longitude",
        "latitude",
    ),
) -> None:
    """Write one Open-Meteo shaped CSV chunk, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False, columns=list(columns), lineterminator="\n")


def _point_rows(point: str, hours: list[str], rain: list[float]) -> list[dict[str, object]]:
    """Build ``WRITE_RAIN`` rows for one point with ``rain_mm == precipitation_mm``."""
    return [
        {
            "observed_at": f"2022-01-01T{hour}:00:00+08:00",
            "precipitation_mm": value,
            "rain_mm": value,
            "point_id": point,
            "longitude": 120.0,
            "latitude": 27.7,
        }
        for hour, value in zip(hours, rain, strict=True)
    ]


# --------------------------------------------------------------------------- #
# load_hourly_rainfall_tables
# --------------------------------------------------------------------------- #


def test_load_rainfall_equal_weight_average_over_points(tmp_path) -> None:
    """Two points are averaged with equal weight; a point absent from an hour drops out."""
    root = tmp_path / "hourly"
    _write_rainfall_csv(
        root / "WC-G1" / "chunk.csv",
        _point_rows("WC-G1", ["00", "01", "02"], [1.0, 2.0, 0.0]),
    )
    _write_rainfall_csv(
        root / "WC-G2" / "chunk.csv",
        _point_rows("WC-G2", ["00", "02"], [3.0, 4.0]),
    )

    table = load_hourly_rainfall_tables(root)

    assert list(table.columns) == ["observed_at", "rainfall_mm", "point_count"]
    assert len(table) == 3
    assert table["rainfall_mm"].tolist() == pytest.approx([2.0, 2.0, 2.0])
    assert table["point_count"].tolist() == [2, 1, 2]
    assert str(table["observed_at"].dt.tz) == "Asia/Shanghai"


def test_load_rainfall_recurses_into_subdirectories(tmp_path) -> None:
    """Every ``*.csv`` below the directory participates, not just the top level."""
    root = tmp_path / "hourly"
    _write_rainfall_csv(root / "WC-G1" / "2015" / "chunk.csv", _point_rows("WC-G1", ["00"], [1.0]))
    _write_rainfall_csv(root / "WC-G2" / "2020" / "chunk.csv", _point_rows("WC-G2", ["00"], [3.0]))

    table = load_hourly_rainfall_tables(root)

    assert len(table) == 1
    assert table["rainfall_mm"].iloc[0] == pytest.approx(2.0)
    assert table["point_count"].iloc[0] == 2


def test_load_rainfall_missing_column_names_file(tmp_path) -> None:
    """A missing column is reported together with the offending file path."""
    root = tmp_path / "hourly"
    path = root / "WC-G1" / "chunk.csv"
    _write_rainfall_csv(
        path,
        _point_rows("WC-G1", ["00"], [1.0]),
        columns=("observed_at", "precipitation_mm", "rain_mm", "longitude", "latitude"),
    )

    with pytest.raises(ValueError) as error:
        load_hourly_rainfall_tables(root)

    message = str(error.value)
    assert "point_id" in message
    assert "chunk.csv" in message


def test_load_rainfall_rejects_naive_timestamps(tmp_path) -> None:
    """Naive timestamps leave the bucket grid ambiguous and must be rejected."""
    root = tmp_path / "hourly"
    rows = _point_rows("WC-G1", ["00"], [1.0])
    rows[0]["observed_at"] = "2022-01-01T00:00:00"
    _write_rainfall_csv(root / "WC-G1" / "chunk.csv", rows)

    with pytest.raises(ValueError, match="[Tt]imezone"):
        load_hourly_rainfall_tables(root)


def test_load_rainfall_rejects_mixed_offsets(tmp_path) -> None:
    """Two different UTC offsets cannot describe one hourly grid."""
    root = tmp_path / "hourly"
    rows = _point_rows("WC-G1", ["00", "01"], [1.0, 2.0])
    rows[1]["observed_at"] = "2022-01-01T01:00:00+09:00"
    _write_rainfall_csv(root / "WC-G1" / "chunk.csv", rows)

    with pytest.raises(ValueError, match="[Tt]imezone|offset"):
        load_hourly_rainfall_tables(root)


def test_load_rainfall_point_selection(tmp_path) -> None:
    """``points`` restricts the regional average and rejects unknown identifiers."""
    root = tmp_path / "hourly"
    _write_rainfall_csv(
        root / "WC-G1" / "chunk.csv", _point_rows("WC-G1", ["00", "01"], [1.0, 2.0])
    )
    _write_rainfall_csv(
        root / "WC-G2" / "chunk.csv", _point_rows("WC-G2", ["00", "01"], [3.0, 4.0])
    )

    selected = load_hourly_rainfall_tables(root, points=["WC-G1"])

    assert selected["rainfall_mm"].tolist() == pytest.approx([1.0, 2.0])
    assert selected["point_count"].tolist() == [1, 1]

    with pytest.raises(ValueError, match="WC-G9"):
        load_hourly_rainfall_tables(root, points=["WC-G9"])


def test_load_rainfall_fills_inner_missing_hours_with_nan(tmp_path) -> None:
    """A hole in the hourly grid becomes an explicit NaN row, never an interpolation."""
    root = tmp_path / "hourly"
    _write_rainfall_csv(
        root / "WC-G1" / "chunk.csv", _point_rows("WC-G1", ["00", "03"], [1.0, 4.0])
    )

    table = load_hourly_rainfall_tables(root)

    assert len(table) == 4
    assert table["rainfall_mm"].isna().tolist() == [False, True, True, False]
    assert table["point_count"].tolist() == [1, 0, 0, 1]


def test_load_rainfall_value_column_selects_the_field(tmp_path) -> None:
    """``value_column`` picks the averaged field, letting the two columns be cross-checked."""
    root = tmp_path / "hourly"
    first = _point_rows("WC-G1", ["00", "01"], [1.0, 2.0])
    first[1]["precipitation_mm"] = 9.0
    second = _point_rows("WC-G2", ["00", "01"], [5.0, 5.0])
    second[0]["precipitation_mm"] = 7.0
    _write_rainfall_csv(root / "WC-G1" / "chunk.csv", first)
    _write_rainfall_csv(root / "WC-G2" / "chunk.csv", second)

    primary = load_hourly_rainfall_tables(root)
    cross = load_hourly_rainfall_tables(root, value_column="precipitation_mm")

    assert primary["rainfall_mm"].tolist() == pytest.approx([3.0, 3.5])
    assert cross["rainfall_mm"].tolist() == pytest.approx([4.0, 7.0])
    differing = int((primary["rainfall_mm"] != cross["rainfall_mm"]).sum())
    assert differing == 2


def test_load_rainfall_rejects_double_counted_observations(tmp_path) -> None:
    """The same ``(observed_at, point_id)`` pair twice would silently re-weight a point."""
    root = tmp_path / "hourly"
    rows = _point_rows("WC-G1", ["00"], [1.0])
    _write_rainfall_csv(root / "WC-G1" / "a.csv", rows)
    _write_rainfall_csv(root / "WC-G1" / "b.csv", rows)

    with pytest.raises(ValueError, match="duplicate"):
        load_hourly_rainfall_tables(root)


# --------------------------------------------------------------------------- #
# build_hourly_features — shape, columns and lag/window arithmetic
# --------------------------------------------------------------------------- #


def test_build_output_column_order_is_frozen() -> None:
    """The column contract is positional: DS-5b and DS-6 rely on this exact order."""
    levels = np.arange(10.0, 20.0)
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.arange(1.0, 11.0)),
        horizons=(1, 6),
        water_lags=(1, 2),
        rain_lags=(0, 3),
        rain_windows=(3,),
        split_ratios=RATIOS,
    )

    assert list(frame.columns) == [
        "observed_at",
        "segment",
        "water_level_t",
        "water_level_lag_1",
        "water_level_lag_2",
        "rain_lag_0",
        "rain_lag_3",
        "rain_cum_3",
        "hour_of_day",
        "month",
        "target_h1",
        "target_h6",
        "usable_h1",
        "usable_h6",
        "is_imputed_t",
        "source_record_id_t",
        "rain_point_count",
    ]
    assert len(frame) == 10


def test_build_water_lag_is_the_previous_hour_within_the_segment() -> None:
    """``water_level_lag_k`` is ``t - k`` and restarts as NaN at every segment head."""
    levels = np.arange(10.0, 20.0)
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.zeros(10)),
        horizons=(1,),
        water_lags=(1, 2),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    lag1 = frame["water_level_lag_1"]
    assert pd.isna(lag1.iloc[0])  # train head: no earlier row exists
    assert lag1.iloc[1] == pytest.approx(levels[0])
    assert lag1.iloc[5] == pytest.approx(levels[4])
    assert pd.isna(lag1.iloc[6])  # validation head must not borrow train's tail
    assert lag1.iloc[7] == pytest.approx(levels[6])
    assert pd.isna(lag1.iloc[8])  # test head must not borrow validation's tail
    assert lag1.iloc[9] == pytest.approx(levels[8])

    lag2 = frame["water_level_lag_2"]
    assert pd.isna(lag2.iloc[1])
    assert lag2.iloc[2] == pytest.approx(levels[0])
    assert pd.isna(lag2.iloc[6])


def test_build_long_lag_stays_inside_its_segment() -> None:
    """A 24-step lag needs 24 earlier rows *of the same segment*, never the previous one."""
    levels = np.arange(1.0, 51.0)
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.zeros(50)),
        horizons=(1,),
        water_lags=(24,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=(0.20, 0.60, 0.20),
    )

    assert frame["segment"].tolist() == (["train"] * 10 + ["validation"] * 30 + ["test"] * 10)
    lag24 = frame["water_level_lag_24"]
    assert lag24.iloc[:34].isna().all()  # train is too short and validation restarts
    assert lag24.iloc[34] == pytest.approx(levels[10])
    assert lag24.iloc[39] == pytest.approx(levels[15])
    assert lag24.iloc[40:].isna().all()  # test restarts again


def test_build_rain_cum_is_the_inclusive_running_sum() -> None:
    """``rain_cum_3`` is exactly ``rain[t-2] + rain[t-1] + rain[t]``, resetting per segment."""
    rain = np.arange(1.0, 11.0)
    frame = build_hourly_features(
        _water(np.arange(10.0, 20.0)),
        _rainfall(rain),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(3,),
        split_ratios=(0.40, 0.40, 0.20),
    )

    assert frame["segment"].tolist() == (["train"] * 4 + ["validation"] * 4 + ["test"] * 2)
    assert frame["rain_cum_3"].tolist() == pytest.approx(
        [
            np.nan,
            np.nan,
            rain[0] + rain[1] + rain[2],
            rain[1] + rain[2] + rain[3],
            np.nan,  # validation head cannot borrow the train tail
            np.nan,
            rain[4] + rain[5] + rain[6],
            rain[5] + rain[6] + rain[7],
            np.nan,  # test head
            np.nan,
        ],
        nan_ok=True,
    )


def test_build_rain_lag_zero_is_the_current_hour_rainfall() -> None:
    """``rain_lag_0`` is the rain of the row itself, not a lag at all."""
    rain = np.arange(1.0, 11.0)
    frame = build_hourly_features(
        _water(np.arange(10.0, 20.0)),
        _rainfall(rain),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0, 1),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert frame["rain_lag_0"].tolist() == pytest.approx(rain)
    assert frame["rain_cum_1"].tolist() == pytest.approx(rain)
    lag1 = frame["rain_lag_1"]
    assert pd.isna(lag1.iloc[0])
    assert lag1.iloc[1] == pytest.approx(rain[0])
    assert pd.isna(lag1.iloc[6])  # segment head


def test_build_target_is_the_value_at_t_plus_h() -> None:
    """``target_h{h}`` copies the level of ``t + h`` inside its own segment."""
    levels = np.arange(10.0, 20.0)
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.zeros(10)),
        horizons=(1, 3),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert frame["target_h1"].iloc[0] == pytest.approx(levels[1])
    assert frame["target_h3"].iloc[0] == pytest.approx(levels[3])
    assert frame["target_h3"].iloc[2] == pytest.approx(levels[5])
    assert frame["target_h1"].iloc[4] == pytest.approx(levels[5])
    # the last h rows of a segment cannot see past the boundary
    assert pd.isna(frame["target_h1"].iloc[5])
    assert frame["target_h3"].iloc[3:6].isna().all()
    assert frame["target_h1"].iloc[6] == pytest.approx(levels[7])
    assert pd.isna(frame["target_h1"].iloc[7])
    assert frame["target_h3"].iloc[6:].isna().all()


def test_build_clock_columns_come_from_the_local_timestamp() -> None:
    """``hour_of_day`` and ``month`` are derived from ``t`` and add no seasonal flag."""
    stamps = pd.date_range(
        pd.Timestamp("2022-12-31T22:00:00", tz="Asia/Shanghai"), periods=4, freq="1h"
    )
    water = _water(np.arange(4.0))
    water["observed_at"] = stamps
    frame = build_hourly_features(
        water,
        _rainfall(np.zeros(4)),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=(0.5, 0.25, 0.25),
    )

    assert frame["hour_of_day"].tolist() == [22, 23, 0, 1]
    assert frame["month"].tolist() == [12, 12, 1, 1]
    assert set(frame.columns).isdisjoint({"flood_season", "is_flood_season"})


def test_build_traceability_columns_carry_the_source_row() -> None:
    """``is_imputed_t`` and ``source_record_id_t`` identify the row the features came from."""
    water = _water(np.arange(1.0, 11.0), imputed=[False] * 4 + [True] + [False] * 5)
    frame = build_hourly_features(
        water,
        _rainfall(np.zeros(10), counts=5),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert frame["is_imputed_t"].tolist() == [False] * 4 + [True] + [False] * 5
    assert frame["source_record_id_t"].tolist() == water["source_record_id"].tolist()
    assert frame["rain_point_count"].tolist() == [5] * 10


# --------------------------------------------------------------------------- #
# build_hourly_features — imputation (C-5) and usability
# --------------------------------------------------------------------------- #


def test_build_keeps_imputed_values_but_flags_them_unusable_as_targets() -> None:
    """Imputed levels stay visible in ``water_level_t`` yet never label a training sample."""
    levels = np.arange(10.0, 20.0)
    imputed = [False] * 10
    imputed[3] = True
    frame = build_hourly_features(
        _water(levels, imputed=imputed),
        _rainfall(np.zeros(10)),
        horizons=(1, 3),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    # the interpolated value is preserved and traceable, not hidden
    assert frame["water_level_t"].iloc[3] == pytest.approx(levels[3])
    assert frame["water_level_lag_1"].iloc[4] == pytest.approx(levels[3])
    assert bool(frame["is_imputed_t"].iloc[3]) is True

    # any sample whose *target* hour is imputed is not usable
    assert bool(frame["usable_h1"].iloc[2]) is False
    assert bool(frame["usable_h3"].iloc[0]) is False
    assert bool(frame["usable_h1"].iloc[4]) is True
    assert bool(frame["usable_h1"].iloc[1]) is True


def test_build_usable_requires_a_present_target_and_a_present_current_value() -> None:
    """A row is only usable when both its own hour and its target hour carry a level."""
    levels = np.arange(10.0, 20.0)
    levels[4] = np.nan
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.zeros(10)),
        horizons=(1, 3),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    # the target hour itself is missing
    assert pd.isna(frame["target_h1"].iloc[3])
    assert bool(frame["usable_h1"].iloc[3]) is False
    # the row's own hour is missing, so the feature vector has no anchor
    assert pd.isna(frame["water_level_t"].iloc[4])
    assert frame["target_h1"].iloc[4] == pytest.approx(levels[5])
    assert bool(frame["usable_h1"].iloc[4]) is False
    # a fully observed row is usable
    assert bool(frame["usable_h1"].iloc[0]) is True
    assert bool(frame["usable_h3"].iloc[0]) is True
    assert int(frame["usable_h1"].sum()) == 5  # rows 0, 1, 2, 6, 8


# --------------------------------------------------------------------------- #
# build_hourly_features — segment contract
# --------------------------------------------------------------------------- #


def test_build_segments_match_chronological_split() -> None:
    """Boundaries are the integer formula of ``dataset.chronological_split``, row for row."""
    levels = np.arange(100.0)
    water = _water(levels)
    frame = build_hourly_features(
        water,
        _rainfall(np.zeros(100)),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
    )
    split = chronological_split(water)

    assert frame["segment"].tolist() == (
        ["train"] * len(split.train)
        + ["validation"] * len(split.validation)
        + ["test"] * len(split.test)
    )
    assert [len(split.train), len(split.validation), len(split.test)] == [70, 15, 15]
    assert len(frame) == 100  # no row is dropped, reordered or de-duplicated
    assert frame["observed_at"].is_monotonic_increasing


def test_build_segment_heads_never_borrow_the_previous_segment() -> None:
    """Every lag and window restarts as NaN at a segment head."""
    levels = np.arange(60.0, 160.0)
    frame = build_hourly_features(
        _water(levels),
        _rainfall(np.ones(100)),
        horizons=(1,),
        water_lags=(1, 6, 24),
        rain_lags=(0, 1, 6),
        rain_windows=(1, 3, 24),
        split_ratios=(0.70, 0.15, 0.15),
    )

    for head in (70, 85):
        for column in (
            "water_level_lag_1",
            "water_level_lag_6",
            "water_level_lag_24",
            "rain_lag_1",
            "rain_lag_6",
            "rain_cum_3",
            "rain_cum_24",
        ):
            assert pd.isna(frame[column].iloc[head]), column
        assert frame["rain_lag_0"].iloc[head] == pytest.approx(1.0)
        assert frame["rain_cum_1"].iloc[head] == pytest.approx(1.0)

    # labels stop at the other end: a segment tail cannot reach into the next segment
    assert not pd.isna(frame["target_h1"].iloc[68])
    assert pd.isna(frame["target_h1"].iloc[69])
    assert pd.isna(frame["target_h1"].iloc[84])

    # the row before a head does expose its own history, so the NaN above is a boundary
    assert not pd.isna(frame["water_level_lag_1"].iloc[69])
    assert frame["water_level_lag_1"].iloc[69] == pytest.approx(levels[68])


# --------------------------------------------------------------------------- #
# build_hourly_features — rainfall alignment
# --------------------------------------------------------------------------- #


def test_build_missing_rainfall_hours_become_nan() -> None:
    """A water hour without a rainfall row yields NaN rain columns and a null point count."""
    frame = build_hourly_features(
        _water(np.arange(10.0, 20.0)),
        _rainfall(np.ones(5), start=START),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert frame["rain_lag_0"].iloc[:5].tolist() == pytest.approx([1.0] * 5)
    assert frame["rain_lag_0"].iloc[5:].isna().all()
    assert frame["rain_point_count"].iloc[:5].tolist() == [5] * 5
    assert frame["rain_point_count"].iloc[5:].isna().all()


def test_build_ignores_rainfall_hours_outside_the_water_grid() -> None:
    """Rainfall covering a longer period is aligned by timestamp, not by position."""
    rainfall = _rainfall(np.arange(1.0, 41.0), start="2022-05-31T00:00:00")
    water = _water(np.arange(10.0, 20.0))  # 2022-06-01T00:00 .. 09:00

    frame = build_hourly_features(
        water,
        rainfall,
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert frame["rain_lag_0"].tolist() == pytest.approx(list(np.arange(25.0, 35.0)))


def test_build_rejects_rainfall_with_duplicate_timestamps() -> None:
    """Two rows for one hour would make the join ambiguous."""
    rainfall = _rainfall(np.ones(10))
    rainfall.loc[9, "observed_at"] = rainfall["observed_at"].iloc[8]

    with pytest.raises(ValueError, match="duplicate"):
        build_hourly_features(
            _water(np.arange(10.0, 20.0)),
            rainfall,
            horizons=(1,),
            water_lags=(1,),
            rain_lags=(0,),
            rain_windows=(1,),
            split_ratios=RATIOS,
        )


def test_build_accepts_a_six_column_water_table() -> None:
    """The in-memory resampled frame (with station columns) is recognized by name."""
    frame = build_hourly_features(
        _water(np.arange(10.0, 20.0), station=True),
        _rainfall(np.ones(10)),
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(1,),
        split_ratios=RATIOS,
    )

    assert len(frame) == 10
    assert frame["water_level_t"].iloc[0] == pytest.approx(10.0)


def test_build_missing_water_column_is_named_in_the_error() -> None:
    """Recognizing the input by column name must fail loudly, naming the missing column."""
    water = _water(np.arange(10.0, 20.0)).drop(columns=["is_imputed"])

    with pytest.raises(ValueError, match="is_imputed"):
        build_hourly_features(
            water,
            _rainfall(np.ones(10)),
            horizons=(1,),
            water_lags=(1,),
            rain_lags=(0,),
            rain_windows=(1,),
            split_ratios=RATIOS,
        )


# --------------------------------------------------------------------------- #
# build_hourly_features — validation, purity and determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"horizons": (0,)}, "horizons"),
        ({"horizons": (-1,)}, "horizons"),
        ({"horizons": (1.5,)}, "horizons"),
        ({"water_lags": (0,)}, "water_lags"),
        ({"water_lags": (-3,)}, "water_lags"),
        ({"rain_lags": (-1,)}, "rain_lags"),
        ({"rain_windows": (0,)}, "rain_windows"),
        ({"split_ratios": (0.7, 0.2, 0.2)}, "split_ratios"),
        ({"split_ratios": (0.7, 0.3)}, "split_ratios"),
        ({"split_ratios": (0.0, 0.5, 0.5)}, "split_ratios"),
        ({"split_ratios": (1.0, 0.0, 0.0)}, "split_ratios"),
        ({"split_ratios": (0.99, 0.005, 0.005)}, "split_ratios"),
    ],
)
def test_build_rejects_invalid_parameters(kwargs, expected) -> None:
    """Illegal window and split parameters raise ``ValueError`` naming the parameter."""
    defaults = {
        "horizons": (1,),
        "water_lags": (1,),
        "rain_lags": (0,),
        "rain_windows": (1,),
        "split_ratios": RATIOS,
    }
    defaults.update(kwargs)

    with pytest.raises(ValueError, match=expected):
        build_hourly_features(
            _water(np.arange(10.0, 20.0)),
            _rainfall(np.ones(10)),
            **defaults,
        )


def test_build_does_not_modify_its_inputs() -> None:
    """The callers keep their frames untouched, so reruns cannot drift."""
    water = _water(np.arange(10.0, 20.0), imputed=[False] * 9 + [True])
    rainfall = _rainfall(np.arange(1.0, 11.0))
    water_before = water.copy(deep=True)
    rainfall_before = rainfall.copy(deep=True)

    build_hourly_features(
        water,
        rainfall,
        horizons=(1,),
        water_lags=(1,),
        rain_lags=(0,),
        rain_windows=(3,),
        split_ratios=RATIOS,
    )

    pd.testing.assert_frame_equal(water, water_before)
    pd.testing.assert_frame_equal(rainfall, rainfall_before)


def test_build_is_deterministic() -> None:
    """Two calls on equal inputs produce byte-equal frames."""
    water = _water(np.arange(1.0, 61.0), imputed=[False] * 59 + [True])
    rainfall = _rainfall(np.arange(1.0, 61.0))

    first = build_hourly_features(water, rainfall)
    second = build_hourly_features(water, rainfall)

    pd.testing.assert_frame_equal(first, second)
    assert first.equals(second)


def test_build_returns_a_typed_empty_frame_for_empty_water() -> None:
    """An empty but valid input returns an empty frame with the full column contract."""
    empty_water = _water(np.zeros(0))
    frame = build_hourly_features(empty_water, _rainfall(np.ones(5)))

    assert len(frame) == 0
    assert list(frame.columns) == list(
        build_hourly_features(_water(np.arange(10.0, 20.0)), _rainfall(np.ones(10))).columns
    )
    assert str(frame["observed_at"].dtype.tz) == "Asia/Shanghai"
    assert frame["segment"].dtype == object
    assert frame["usable_h1"].dtype == bool


def test_build_raises_type_error_for_non_frames() -> None:
    """Passing something that is not a DataFrame is a programming error."""
    with pytest.raises(TypeError):
        build_hourly_features([1, 2, 3], _rainfall(np.ones(3)))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Leakage probes (must be tests, not just report prose)
# --------------------------------------------------------------------------- #


def test_leak_probe_future_rainfall_cannot_change_past_features() -> None:
    """Probe 1: replacing all rainfall after ``t`` leaves every rain feature at ``<= t`` equal."""
    rain = np.where(np.arange(300.0) < 150, 1.0, 2.0)
    water = _water(np.arange(1.0, 301.0))
    rainfall = _rainfall(rain)

    base = build_hourly_features(water, rainfall)
    mutated_input = rainfall.copy(deep=True)
    future = mutated_input["observed_at"] > rainfall["observed_at"].iloc[149]
    mutated_input.loc[future, "rainfall_mm"] = np.nan
    mutated = build_hourly_features(water, mutated_input)

    rain_columns = [name for name in base.columns if name.startswith(("rain_lag_", "rain_cum_"))]
    assert rain_columns
    np.testing.assert_array_equal(
        base.loc[:149, rain_columns].to_numpy(dtype="float64"),
        mutated.loc[:149, rain_columns].to_numpy(dtype="float64"),
    )
    # the mutation really happened after the cut, so the probe is not vacuous
    assert not base.loc[200:, rain_columns].equals(mutated.loc[200:, rain_columns])


def test_leak_probe_future_water_rows_cannot_change_past_features() -> None:
    """Probe 2: deleting every row after the cut leaves the earlier window identical."""
    water = _water(np.arange(1.0, 2001.0))
    rainfall = _rainfall(np.ones(2000))
    cut = 1200

    base = build_hourly_features(water, rainfall)
    truncated = build_hourly_features(water.iloc[:cut], rainfall.iloc[:cut])

    limits = (int(len(water) * 0.70), int(cut * 0.70))
    safe = min(limits) - 6  # keep every dependency and every label inside ``train``
    pd.testing.assert_frame_equal(
        base.iloc[:safe].reset_index(drop=True),
        truncated.iloc[:safe].reset_index(drop=True),
    )


def test_leak_probe_test_segment_cannot_change_earlier_segments() -> None:
    """Probe 3: rewriting the test segment leaves train and validation byte-identical."""
    water = _water(np.arange(1.0, 101.0))
    rainfall = _rainfall(np.ones(100))

    base = build_hourly_features(water, rainfall)
    mutated_input = water.copy(deep=True)
    mutated_input.loc[85:, "water_level_m"] = 999.0
    mutated = build_hourly_features(mutated_input, rainfall)

    pd.testing.assert_frame_equal(
        base.iloc[:85].reset_index(drop=True),
        mutated.iloc[:85].reset_index(drop=True),
    )
    assert not base.loc[90:, "water_level_t"].equals(mutated.loc[90:, "water_level_t"])


# --------------------------------------------------------------------------- #
# feature_columns
# --------------------------------------------------------------------------- #


def test_feature_columns_excludes_keys_labels_and_traceability() -> None:
    """DS-6 receives the model inputs only: no key, no segment, no label, no provenance."""
    frame = build_hourly_features(
        _water(np.arange(1.0, 61.0)),
        _rainfall(np.arange(1.0, 61.0)),
        horizons=(1, 3, 6),
        water_lags=(1, 2, 3, 6, 12, 24),
        rain_lags=(0, 1, 2, 3, 6, 12),
        rain_windows=(1, 3, 6, 12, 24),
    )

    columns = feature_columns(frame)

    assert columns == (
        "water_level_t",
        "water_level_lag_1",
        "water_level_lag_2",
        "water_level_lag_3",
        "water_level_lag_6",
        "water_level_lag_12",
        "water_level_lag_24",
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
        "hour_of_day",
        "month",
    )
    assert "observed_at" not in columns
    assert "segment" not in columns
    assert "target_h1" not in columns
    assert "usable_h1" not in columns
    assert "is_imputed_t" not in columns
    assert "source_record_id_t" not in columns
    assert "rain_point_count" not in columns


def test_feature_columns_is_stable_and_empty_frame_safe() -> None:
    """The helper is deterministic and tolerates a frame without any column."""
    frame = build_hourly_features(_water(np.arange(1.0, 21.0)), _rainfall(np.ones(20)))
    assert feature_columns(frame) == feature_columns(frame)
    assert feature_columns(pd.DataFrame()) == ()

    with pytest.raises(TypeError):
        feature_columns(["observed_at"])  # type: ignore[arg-type]
