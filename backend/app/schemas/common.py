"""统一响应外壳、证据字段与错误码（DS-8A 对外契约）。

所有 HTTP 接口都返回 :class:`ApiResponse`：

```json
{"request_id": "...", "status": "ok", "data": {}, "evidence": [], "warnings": []}
```

``data_mode`` 只能是 ``history_replay``（历史回放）或 ``local_snapshot``（本地快照），
**不允许 ``realtime``**：本项目没有实时推送接口，任何"实时"表述都会误导使用方。

错误不再新增外壳字段：失败原因统一写入 ``warnings``，格式为 ``"<CODE>: <信息>"``，
``status`` 取 ``error`` / ``unavailable`` / ``degraded`` 之一。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, field_validator

#: 数据模式。``history_replay`` 为默认；``local_snapshot`` 表示本地处理产物快照。
DataMode = Literal["history_replay", "local_snapshot"]
DATA_MODES: tuple[str, ...] = ("history_replay", "local_snapshot")

#: 统一响应状态。``degraded`` 表示已按允许的方式降级（例如回退持久性基线）。
ResponseStatus = Literal["ok", "degraded", "unavailable", "error"]

#: 数据新鲜度。``unknown`` 用于无法判定新鲜度的场景（例如调用方自供水位）。
Freshness = Literal["fresh", "stale", "unknown"]

#: 统一错误码（沿用 DS-8 设计规格 §9）。
INVALID_REQUEST = "INVALID_REQUEST"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
STALE_SNAPSHOT = "STALE_SNAPSHOT"
AGENT_TOOL_FAILED = "AGENT_TOOL_FAILED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
PROVIDER_OVERRIDE_BLOCKED = "PROVIDER_OVERRIDE_BLOCKED"
COMPARISON_ARTIFACT_UNAVAILABLE = "COMPARISON_ARTIFACT_UNAVAILABLE"
MISSING_HORIZONS = "MISSING_HORIZONS"
CLARIFY_INTENT = "CLARIFY_INTENT"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: 必带免责声明。所有回答、简报和证据说明都必须携带。
DISCLAIMER = "本结果为历史回放 / 本地快照数据上的教学科研辅助分析，不替代官方防汛决策。"

#: 研究性高水位阈值（DS-7：train 段 p90 拟合，validation / test 段不参与）。
RESEARCH_THRESHOLD = 7.18
RESEARCH_THRESHOLD_SOURCE = "train p90 research_high_water"
RESEARCH_THRESHOLD_UNIT = "m"

#: 允许的预测时域（小时网格上的 1 / 3 / 6 步）。
SUPPORTED_HORIZONS: tuple[int, ...] = (1, 3, 6)

#: 已验证的持久性基线（DS-5b 产物 schema_version）。
PERSISTENCE_MODEL = "persistence"
PERSISTENCE_VERSION = "ds5b-hourly-baseline-1"

#: 默认目标断面：温州市公共数据开放平台数据集 12720 的接口目录。
DEFAULT_STATION_ID = "cata_12720"
DEFAULT_SOURCE_URL = "https://data.wenzhou.gov.cn/jdop_front/detail/data.do?iid=12720"


def warning_of(code: str, message: str) -> str:
    """Format one warning entry as ``"<CODE>: <message>"``."""
    return f"{code}: {message}"


class Evidence(BaseModel):
    """Traceability record attached to every response.

    ``tool_trace`` 记录本次响应实际调用的白名单工具（``工具名:摘要``），智能体回答、
    简报和前端证据栏都直接消费该字段，不允许由大语言模型自行填写。
    """

    source_url: str | None = None
    data_mode: DataMode = "history_replay"
    observed_at: datetime | None = None
    freshness: Freshness = "unknown"
    model_version: str | None = None
    tool_trace: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("observed_at must be timezone-aware")
        return value


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Unified response envelope returned by every endpoint."""

    request_id: str
    status: ResponseStatus = "ok"
    data: T | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Serialize with ``mode="json"`` so datetimes become ISO strings."""
        return self.model_dump(mode="json")
