"""``river-sentinel-data`` 命令行入口。

只有两个子命令：``audit-file``（本地文件）与 ``audit-api``（官方接口）。
成功返回 0；参数错误返回 2；缺凭据、读取失败或管线失败返回 1，
错误信息为中文并打印到 stderr，绝不回显凭据取值。

模块导入时不读取环境变量、不发起网络请求：配置只在命令真正执行时加载。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from river_sentinel_ml.ingestion.wenzhou_api import MAX_PAGE_SIZE, SourceAccessError
from river_sentinel_ml.pipeline import (
    DEFAULT_FREQUENCY,
    DEFAULT_HORIZONS,
    DEFAULT_STEPS,
    DEFAULT_TIMEZONE,
    DEFAULT_TRAIN_RATIO,
    DEFAULT_VALIDATION_RATIO,
    BaselineResult,
    run_api_baseline,
    run_file_baseline,
)

SUCCESS = 0
RUNTIME_ERROR = 1
USAGE_ERROR = 2

_ARTIFACT_NAMES = (
    "processed.parquet",
    "quality-report.json",
    "baseline-metrics.json",
    "experiment-manifest.json",
    "baseline-report.md",
)


def main(argv: list[str] | None = None) -> int:
    """命令入口，返回进程退出码。"""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as request:
        # argparse 用 SystemExit 表达用法错误（2）与 --help（0）。
        code = request.code
        return code if isinstance(code, int) else USAGE_ERROR
    handler = args.handler
    return handler(args)


def _audit_file(args: argparse.Namespace) -> int:
    try:
        result = run_file_baseline(
            input_path=Path(args.input),
            output_dir=Path(args.output_dir),
            timezone=args.timezone,
            frequency=args.frequency,
            max_interpolation_steps=args.max_interpolation_steps,
            horizons=tuple(args.horizons),
            train_ratio=args.train_ratio,
            validation_ratio=args.validation_ratio,
            station_code=args.station_code,
        )
    except (ValueError, OSError) as error:
        return _fail(f"文件审计失败：{error}")
    _report_success(result, "文件审计完成")
    return SUCCESS


def _audit_api(args: argparse.Namespace) -> int:
    appsecret = _resolve_appsecret()
    if not appsecret:
        print(
            "缺少访问凭据：请设置环境变量 WENZHOU_DATA_APPSECRET 后重试"
            "（凭据不会写入任何产物、日志或异常消息）",
            file=sys.stderr,
        )
        return RUNTIME_ERROR
    try:
        result = run_api_baseline(
            output_dir=Path(args.output_dir),
            appsecret=appsecret,
            timezone=args.timezone,
            frequency=args.frequency,
            max_interpolation_steps=DEFAULT_STEPS,
            horizons=tuple(args.horizons),
            train_ratio=args.train_ratio,
            validation_ratio=args.validation_ratio,
            station_code=args.station_code,
            page_size=args.page_size,
        )
    except (ValueError, OSError, SourceAccessError) as error:
        return _fail(f"接口审计失败：{error}")
    _report_success(result, "接口审计完成")
    return SUCCESS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="river-sentinel-data",
        description="飞云江水位数据审计与基线报告命令行工具（历史回放 / 离线数据）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_parser = subparsers.add_parser("audit-file", help="审计本地水位数据文件")
    file_parser.add_argument("--input", required=True, help="本地 .csv / .json / .parquet 文件路径")
    file_parser.add_argument("--output-dir", required=True, help="产物输出目录")
    _add_common_arguments(file_parser)
    file_parser.add_argument(
        "--max-interpolation-steps",
        type=int,
        default=DEFAULT_STEPS,
        help="允许线性插补的最大连续缺失步数",
    )
    file_parser.set_defaults(handler=_audit_file)

    api_parser = subparsers.add_parser("audit-api", help="审计官方接口水位数据")
    api_parser.add_argument("--output-dir", required=True, help="产物输出目录")
    api_parser.add_argument(
        "--page-size",
        type=_page_size_argument,
        default=MAX_PAGE_SIZE,
        help=f"单页记录数，取值 1 ~ {MAX_PAGE_SIZE}",
    )
    _add_common_arguments(api_parser)
    api_parser.set_defaults(handler=_audit_api)

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA 时区名")
    parser.add_argument("--frequency", default=DEFAULT_FREQUENCY, help="重采样频率")
    parser.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=list(DEFAULT_HORIZONS),
        help="预测步长列表，单位为步长而非小时",
    )
    parser.add_argument("--train-ratio", type=float, default=DEFAULT_TRAIN_RATIO)
    parser.add_argument("--validation-ratio", type=float, default=DEFAULT_VALIDATION_RATIO)
    parser.add_argument("--station-code", default=None, help="目标断面代码；多断面源数据必填")


def _page_size_argument(value: str) -> int:
    """校验 ``--page-size``：非整数与越界都转成中文错误，绝不静默截断。

    ``argparse.ArgumentTypeError`` 会被 argparse 捕获并以非 0 退出码打印
    ``用法错误``，因此 ``0`` / ``201`` / ``abc`` 三种输入都会得到中文提示。
    """
    try:
        page_size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"--page-size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数，无法解析：{value!r}"
        ) from error
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(
            f"--page-size 必须落在 1 到 {MAX_PAGE_SIZE} 之间，收到 {page_size}"
        )
    return page_size


def _resolve_appsecret() -> str | None:
    """从配置里读取接口凭据；空字符串按缺失处理。"""
    from river_sentinel_ml.config import Settings  # noqa: PLC0415

    secret = Settings().wenzhou_data_appsecret
    if secret is None:
        return None
    value = secret.get_secret_value().strip()
    return value or None


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return RUNTIME_ERROR


def _report_success(result: BaselineResult, title: str) -> None:
    # 裁定 1.6：把 run 目录的绝对路径打印到 stdout。
    run_dir = result.artifacts.report_path.parent.resolve()
    splits = result.split_sizes
    print(f"{title}：")
    print(f"  run 目录：{run_dir}")
    print(f"  原始快照：{result.artifacts.snapshot_path.resolve()}")
    print(f"  原始记录数：{result.source.record_count}")
    print(f"  快照 SHA-256：{result.source.sha256}")
    print(f"  有效行数：{result.quality.valid_rows} / {result.quality.total_rows}")
    print(
        "  切分规模："
        f"train={splits['train']}, validation={splits['validation']}, test={splits['test']}"
    )
    print(f"  代码提交：{result.experiment.code_commit}")
    print("  产物：" + "、".join(_ARTIFACT_NAMES))
    print("  提示：本次为历史回放 / 离线数据，仅用于教学科研辅助，不替代官方防汛决策。")


if __name__ == "__main__":  # pragma: no cover - 仅供 python -m 调用
    sys.exit(main())
