"""一次性端到端脚本：DS-4b 小时特征矩阵 → 两组实验 × 四类模型 × 三个 horizon → 证据。

输入（本地副本，原始数据不进 Git）：

- ``data/processed/hourly_features_cata12720.csv``：DS-4b 交付的 3090 行 × 31 列特征矩阵，
  SHA-256 在本文件里冻结；指纹不符立即退出，**不重建数据**。

产物（全部落在被 ``.gitignore`` 忽略的目录，不进入提交）：

- ``data/processed/hourly_model_experiments.json``：严格 JSON 指标与口径声明
  （非有限值写 ``null``，不含绝对路径）；
- ``data/processed/hourly_model_predictions/``：首种子逐段预测序列
  （``observed_at`` / ``segment`` / ``observed`` / ``prediction``），供 DS-7 做误差分析；
- ``ml/artifacts/hourly-models/``：首种子模型权重与超参元数据。

用法::

    python scripts/train_hourly_models.py
    python scripts/train_hourly_models.py --features data/processed/other.csv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from river_sentinel_ml.models import (
    FEATURE_SUBSETS,
    HORIZONS,
    MODEL_NAMES,
    SEEDS,
    SEGMENT_ORDER,
    run_hourly_experiments,
    to_json_safe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES = REPO_ROOT / "data" / "processed" / "hourly_features_cata12720.csv"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "processed" / "hourly_model_experiments.json"
DEFAULT_PREDICTIONS = REPO_ROOT / "data" / "processed" / "hourly_model_predictions"
DEFAULT_ARTIFACTS = REPO_ROOT / "ml" / "artifacts" / "hourly-models"

#: DS-4b 冻结的特征矩阵指纹（3090 行 × 31 列）；不符即退出，不允许"顺手重建"。
EXPECTED_FEATURE_SHA256 = "D3E2CE5CAAB739BAE163906045613D553CE05507987DE263DFD97C8FC5801B4A"
EXPECTED_FEATURE_ROWS = 3090

#: 特征矩阵时间戳的时区口径，与 DS-2b / DS-3 / DS-4b 一致。
TIMEZONE = "Asia/Shanghai"

#: 预测序列只落盘首个种子，避免产物数量无谓膨胀。
PRIMARY_SEED = SEEDS[0]

#: DS-5b 实测的持久性基线与训练集均值参考下界（**直接引用，不在此重训**）。
#: 来源：DS-5b 产物 ``hourly_baseline_metrics.json``（``schema_version`` =
#: ``ds5b-hourly-baseline-1``，分支 ``codex/ds2-water-daily``，提交 ``48cc4e3``）。
BASELINE_REFERENCE: dict[str, Any] = {
    "source": "DS-5b hourly_baseline_metrics.json (codex/ds2-water-daily @ 48cc4e3)",
    "note": "持久性基线与训练集均值参考下界在此**不重训**，直接引用；样本口径为 DS-5b 段内口径。",
    "persistence": {
        "train": {
            "h1": {
                "sample_count": 1610,
                "mae": 0.1403395447204969,
                "rmse": 0.4637001989458443,
                "r2": 0.587698551366209,
                "nse": 0.587698551366209,
            },
            "h3": {
                "sample_count": 1518,
                "mae": 0.30704655270092224,
                "rmse": 0.6046939857338316,
                "r2": 0.2350675547351786,
                "nse": 0.2350675547351786,
            },
            "h6": {
                "sample_count": 1401,
                "mae": 0.4700737566024269,
                "rmse": 0.7821817027541644,
                "r2": -0.2422052800255754,
                "nse": -0.2422052800255754,
            },
        },
        "validation": {
            "h1": {
                "sample_count": 198,
                "mae": 0.0854040404040404,
                "rmse": 0.13676456212976174,
                "r2": 0.8946585090900748,
                "nse": 0.8946585090900748,
            },
            "h3": {
                "sample_count": 168,
                "mae": 0.22482142857142856,
                "rmse": 0.33604580895212144,
                "r2": 0.38493006429523224,
                "nse": 0.38493006429523224,
            },
            "h6": {
                "sample_count": 126,
                "mae": 0.34317460317460313,
                "rmse": 0.4930887424556638,
                "r2": -0.5573370988809263,
                "nse": -0.5573370988809263,
            },
        },
        "test": {
            "h1": {
                "sample_count": 144,
                "mae": 0.04055555555555552,
                "rmse": 0.09481707534921001,
                "r2": 0.9322078251061499,
                "nse": 0.9322078251061499,
            },
            "h3": {
                "sample_count": 118,
                "mae": 0.1075423728813559,
                "rmse": 0.23337065883863728,
                "r2": 0.6280461010215935,
                "nse": 0.6280461010215935,
            },
            "h6": {
                "sample_count": 84,
                "mae": 0.21095238095238095,
                "rmse": 0.3924920381358073,
                "r2": 0.20067359578076094,
                "nse": 0.20067359578076094,
            },
        },
    },
    "train_mean_lower_bound": {
        "constant": 6.340845410628019,
        "calibration_rows": 1656,
        "calibration_basis": "train 段 is_imputed=False 且 water_level_m 有限的观测",
        "test": {
            "h1": {"sample_count": 144, "mae": 0.48037238325281795, "rmse": 0.5229643811704784},
            "h3": {"sample_count": 118, "mae": 0.5075468762793743, "rmse": 0.5454655707238188},
            "h6": {"sample_count": 84, "mae": 0.5445939728548423, "rmse": 0.5792940911969596},
        },
    },
}

#: 论文、页面、报告必须一致表述的口径声明。
DECLARATIONS: tuple[str, ...] = (
    "本系统与本文一切结论仅用于教学、科研与辅助分析，不替代水行政主管部门的正式监测、"
    "预警、调度或应急决策，也不冒充官方预警发布。",
    "HS-D-5 有界前瞻：resample_water_level 在每小时桶内取末次观测、时间戳取桶左边界，"
    "因此标签 t 的值可能晚于 t 至多 1 小时，**1 小时 horizon 的评估结果偏乐观**，"
    "3/6 小时 horizon 不受该影响。",
    "test 段限制：test 段 464 小时中缺失 307 小时（66.16%），h1/h3/h6 的 usable_h{h} 仅 "
    "144/118/84，属数据源缺失而非模型或切分缺陷；测试段指标只能作为探索性参考，"
    "不得用于任何超参、特征或模型选择。",
    "HS-D-12 结论保守性：样本量有限，全部结论标注探索性/教学科研，不给出正式洪水预警结论，"
    "不就高水位事件下确定性结论，不使用「超警」表述。",
    "降雨口径：E2 使用的降雨是 Open-Meteo **Best Match 网格化再分析**降雨（5 个网格点等权"
    "区域平均），是「文成县区域降雨代理变量」，**不是地面雨量站实测**、**不是 ERA5-Land**。",
    "协议：只使用 usable_h{h} = True 的行（C-5 剔除目标时刻被插补的样本）；标准化、alpha 选择"
    "与早停只用 train / validation；test 段只在最终评估时读一次；每个 horizon 单独建模，"
    "禁止递归滚动（HS-D-3）。",
)

MODEL_DESCRIPTIONS = {
    "ridge": "sklearn Ridge 线性回归；alpha 在 validation 上按 MAE 从 {0.1,1,10,100} 选取；"
    "含缺失特征的行剔除并计数。",
    "xgboost": "XGBoost 回归（hist / n_jobs=1 / 固定 random_state）；缺失由树原生处理；"
    "n_estimators 上界 2000 + validation 早停（early_stopping_rounds=50）。",
    "lstm": "PyTorch 单层 LSTM（hidden_size=32、每行一个时间步、批 64、Adam lr=0.01、"
    "最多 300 轮、patience=20 早停）；CPU 训练、固定 torch.manual_seed 与确定性算法；"
    "含缺失特征的行剔除并计数。",
}


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    """Streaming SHA-256 of one file, upper-case hex."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _relative(path: Path) -> str:
    """Repository-relative POSIX path; artifacts must never leak an absolute path."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None`` so the document stays valid JSON."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def _read_features(path: Path) -> pd.DataFrame:
    """Read the DS-4b feature matrix, restoring timezone-aware timestamps."""
    frame = pd.read_csv(path)
    stamps = pd.to_datetime(frame["observed_at"], format="ISO8601")
    frame["observed_at"] = stamps.dt.tz_convert(TIMEZONE)
    return frame


def _environment() -> dict[str, str]:
    """Runtime versions and code revision, without any absolute path."""
    import sklearn
    import torch
    import xgboost

    revision = "unknown"
    # 只读探测代码版本；失败时如实写 ``unknown``，绝不编造哈希（D-9）。
    # 训练刚结束时进程仍处于高负载，子进程可能超时，故重试一次。
    for _attempt in range(2):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            revision = result.stdout.strip()
            break

    return {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "scikit_learn": sklearn.__version__,
        "xgboost": xgboost.__version__,
        "torch": torch.__version__,
        "torch_device": "cpu",
        "device_note": "LSTM 固定 CPU 训练；未使用 CUDA，即使本机存在 GPU",
        "code_revision": revision,
    }


def _segment_evidence(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-segment boundaries, missing buckets and usable sample counts."""
    evidence: list[dict[str, Any]] = []
    for name in SEGMENT_ORDER:
        part = frame.loc[frame["segment"] == name]
        entry: dict[str, Any] = {
            "segment": name,
            "start": part["observed_at"].iloc[0].isoformat(),
            "end": part["observed_at"].iloc[-1].isoformat(),
            "hours": int(len(part)),
            "missing_buckets": int(part["water_level_t"].isna().sum()),
            "imputed_rows": int(part["is_imputed_t"].astype(bool).sum()),
        }
        for horizon in HORIZONS:
            entry[f"usable_h{horizon}"] = int(part[f"usable_h{horizon}"].astype(bool).sum())
        evidence.append(entry)
    return evidence


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #


class ArtifactCollector:
    """Persist the primary-seed models and their prediction series."""

    def __init__(self, artifact_root: Path, prediction_root: Path) -> None:
        self.artifact_root = artifact_root
        self.prediction_root = prediction_root
        self.artifacts: list[str] = []
        self.predictions: list[str] = []

    def write_model(
        self, experiment: str, model: str, horizon: int, seed: int, forecaster: object
    ) -> None:
        """Save one fitted model's weights/metadata for the primary seed only."""
        if seed != PRIMARY_SEED:
            return
        directory = self.artifact_root / experiment / model / f"h{horizon}" / f"seed{seed}"
        directory.mkdir(parents=True, exist_ok=True)
        if model == "ridge":
            target = directory / "ridge.json"
            target.write_text(
                json.dumps(forecaster.as_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif model == "xgboost":
            target = directory / "xgboost.json"
            forecaster.model.save_model(target)
        elif model == "lstm":
            import torch

            target = directory / "lstm.pt"
            torch.save(forecaster.state_dict, target)
        else:  # pragma: no cover - 模型名由 MODEL_NAMES 约束
            raise ValueError(f"unknown model {model!r}")
        self.artifacts.append(_relative(target))

    def write_predictions(
        self,
        experiment: str,
        model: str,
        horizon: int,
        seed: int,
        segment: str,
        frame: pd.DataFrame,
    ) -> None:
        """Save one segment's prediction series for the primary seed only."""
        if seed != PRIMARY_SEED:
            return
        directory = self.prediction_root / experiment
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{model}_h{horizon}_{segment}.csv"
        table = frame.copy()
        table["observed_at"] = [stamp.isoformat() for stamp in frame["observed_at"]]
        table.to_csv(
            target,
            index=False,
            columns=["observed_at", "segment", "observed", "prediction"],
            float_format="%.6f",
            lineterminator="\n",
            encoding="utf-8",
        )
        self.predictions.append(_relative(target))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _format_metric(entry: dict[str, Any], name: str) -> str:
    """Render one aggregated metric as ``mean ± std`` or ``n/a``."""
    value = entry.get(name)
    if not isinstance(value, dict) or value.get("mean") is None:
        return "n/a"
    deviation = value.get("std")
    if deviation is None:
        return f"{value['mean']:.6f}"
    return f"{value['mean']:.6f}±{deviation:.6f}"


def _print_table(document: dict[str, Any]) -> None:
    """Print the full three-segment × three-horizon × model × experiment table."""
    for experiment, payload in document["experiments"].items():
        print(f"== {experiment} ({len(payload['feature_columns'])} features) ==")
        for model in document["models"]:
            for horizon in document["horizons"]:
                entry = payload["models"][model][f"h{horizon}"]
                cells = []
                for segment in SEGMENT_ORDER:
                    block = entry[segment]
                    cells.append(
                        f"{segment[:4]}:n={block['sample_count']} "
                        f"mae={_format_metric(block, 'mae')} "
                        f"r2={_format_metric(block, 'r2')} "
                        f"nse={_format_metric(block, 'nse')}"
                    )
                print(f"  {model:8s} h{horizon}  " + " | ".join(cells))


def _persistence_comparison(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare every model's test MAE against the frozen persistence threshold."""
    reference = document["baseline_reference"]["persistence"]["test"]
    rows: list[dict[str, Any]] = []
    for experiment, payload in document["experiments"].items():
        for model in document["models"]:
            for horizon in document["horizons"]:
                block = payload["models"][model][f"h{horizon}"]["test"]
                threshold = reference[f"h{horizon}"]["mae"]
                value = block["mae"]["mean"]
                rows.append(
                    {
                        "experiment": experiment,
                        "model": model,
                        "horizon": horizon,
                        "sample_count": block["sample_count"],
                        "mae_mean": value,
                        "mae_std": block["mae"]["std"],
                        "persistence_mae": threshold,
                        "beats_persistence": None if value is None else bool(value < threshold),
                        "mae_gap": None if value is None else float(value - threshold),
                    }
                )
    return rows


def _post_hoc_common_support(
    frame: pd.DataFrame,
    predictions_root: Path,
    *,
    experiments: Sequence[str],
    models: Sequence[str],
    horizons: Sequence[int],
    segments: Sequence[str],
) -> dict[str, Any]:
    """Post-hoc comparison restricted to the rows where **every** model can predict.

    三个模型的评估样本集并不相同：线性与 LSTM 剔除了含缺失特征的行，树模型原生保留。
    直接比较 MAE 会把样本差异混进模型差异。这里取三者都给出有限预测的行的交集，并在
    同一批行上重算持久性基线（anchor 为 ``water_level_t``）。

    **该分析是事后的、探索性的，不参与任何超参、特征或模型选择。**
    """
    lookup = frame.set_index("observed_at")["water_level_t"]
    sections: dict[str, Any] = {}
    for segment in segments:
        for experiment in experiments:
            for horizon in horizons:
                tables = {
                    model: pd.read_csv(
                        predictions_root / experiment / f"{model}_h{horizon}_{segment}.csv"
                    )
                    for model in models
                }
                reference = tables[models[0]]
                stamps = reference["observed_at"].to_numpy()
                for model in models:
                    if not np.array_equal(tables[model]["observed_at"].to_numpy(), stamps):
                        raise ValueError(
                            f"prediction series are not aligned for {experiment} h{horizon} {segment}"
                        )
                mask = reference["observed"].notna().to_numpy()
                for model in models:
                    mask &= tables[model]["prediction"].notna().to_numpy()
                key = f"{experiment}_h{horizon}_{segment}"
                if not mask.any():
                    sections[key] = {"sample_count": 0, "reason": "no common predictable row"}
                    continue
                observed = reference.loc[mask, "observed"].to_numpy(dtype="float64")
                anchors = pd.DatetimeIndex(
                    pd.to_datetime(reference.loc[mask, "observed_at"], format="ISO8601")
                ).tz_convert(TIMEZONE)
                entry: dict[str, Any] = {"sample_count": int(mask.sum())}
                entry["persistence_mae"] = float(
                    np.mean(np.abs(lookup.loc[anchors].to_numpy(dtype="float64") - observed))
                )
                for model in models:
                    entry[f"{model}_mae"] = float(
                        np.mean(
                            np.abs(
                                tables[model].loc[mask, "prediction"].to_numpy(dtype="float64")
                                - observed
                            )
                        )
                    )
                sections[key] = entry
    return {
        "primary_seed": PRIMARY_SEED,
        "note": "事后分析（探索性），不用于任何超参、特征或模型选择；只取三类模型都给出"
        "有限预测的行的交集，并在同一批行上重算持久性基线（anchor = water_level_t）",
        "sections": sections,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Run the two experiments across the four model families and write every artifact."""
    parser = argparse.ArgumentParser(
        description="Train and evaluate the DS-6 hourly water-level models."
    )
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="feature matrix CSV")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="metrics JSON output")
    parser.add_argument(
        "--predictions", default=str(DEFAULT_PREDICTIONS), help="prediction CSV directory"
    )
    parser.add_argument(
        "--artifacts", default=str(DEFAULT_ARTIFACTS), help="model weight directory"
    )
    args = parser.parse_args(argv)

    features_path = Path(args.features)
    output_path = Path(args.output)
    predictions_root = Path(args.predictions)
    artifact_root = Path(args.artifacts)
    if not features_path.is_file():
        raise FileNotFoundError(f"feature matrix does not exist: {features_path}")

    digest = _sha256(features_path)
    if digest != EXPECTED_FEATURE_SHA256:
        raise SystemExit(
            f"feature matrix SHA-256 mismatch: expected {EXPECTED_FEATURE_SHA256}, got {digest}; "
            "stop instead of rebuilding the data"
        )

    frame = _read_features(features_path)
    if len(frame) != EXPECTED_FEATURE_ROWS:
        raise SystemExit(
            f"feature matrix row count mismatch: expected {EXPECTED_FEATURE_ROWS}, got {len(frame)}"
        )

    collector = ArtifactCollector(artifact_root, predictions_root)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    payload = run_hourly_experiments(
        frame,
        experiments=tuple(FEATURE_SUBSETS),
        horizons=HORIZONS,
        seeds=SEEDS,
        model_names=MODEL_NAMES,
        prediction_writer=collector.write_predictions,
        artifact_writer=collector.write_model,
    )
    duration = time.perf_counter() - started
    finished_at = datetime.now(UTC)

    document = {
        "schema_version": "ds6-hourly-models-1",
        "task": "DS-6",
        "generated_at": finished_at.isoformat(),
        "source": {
            "features": _relative(features_path),
            "sha256": digest,
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "first_at": frame["observed_at"].iloc[0].isoformat(),
            "last_at": frame["observed_at"].iloc[-1].isoformat(),
            "split_ratios": {"train": 0.70, "validation": 0.15, "test": 0.15},
            "segments": _segment_evidence(frame),
            "feature_subsets": {name: list(columns) for name, columns in FEATURE_SUBSETS.items()},
        },
        "seeds": payload["seeds"],
        "primary_seed": PRIMARY_SEED,
        "horizons": payload["horizons"],
        "models": payload["models"],
        "model_descriptions": {name: MODEL_DESCRIPTIONS[name] for name in payload["models"]},
        "experiments": payload["experiments"],
        "baseline_reference": BASELINE_REFERENCE,
        "environment": _environment(),
        "run": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(duration, 3),
            "device": "cpu",
            "note": "duration_seconds 为全部 2×3×3 组（实验×模型×horizon）× 3 个随机种子"
            "的 CPU 训练与评估总时长",
        },
        "artifacts": {
            "model_weights": collector.artifacts,
            "prediction_series": collector.predictions,
            "git_ignored": True,
            "note": "模型权重与预测序列落在被忽略目录，不进入 Git 提交",
        },
        "declarations": list(DECLARATIONS),
    }
    document["persistence_comparison"] = _persistence_comparison(document)
    document["post_hoc_common_support"] = _post_hoc_common_support(
        frame,
        predictions_root,
        experiments=tuple(FEATURE_SUBSETS),
        models=MODEL_NAMES,
        horizons=HORIZONS,
        segments=SEGMENT_ORDER,
    )

    safe = _json_safe(to_json_safe(document))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    print("FEATURES", digest, len(frame), "rows", len(frame.columns), "columns")
    print("SEGMENTS")
    print(pd.DataFrame(document["source"]["segments"]).to_string(index=False))
    print("TABLE")
    _print_table(document)
    print("VS_PERSISTENCE_TEST")
    print(pd.DataFrame(_persistence_comparison(document)).to_string(index=False))
    print("POST_HOC_COMMON_SUPPORT")
    print(
        pd.DataFrame(document["post_hoc_common_support"]["sections"])
        .T[["sample_count", "persistence_mae"] + [f"{name}_mae" for name in MODEL_NAMES]]
        .to_string()
    )
    print("RUN", document["run"])
    print("ARTIFACTS", len(collector.artifacts), "weights,", len(collector.predictions), "series")
    print("OUTPUT", output_path, _sha256(output_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
