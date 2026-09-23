"""Environment-backed backend settings (DS-8A).

默认值一律指向仓库内的小型样例，**禁止写入本机绝对路径**：相对路径在
:attr:`Settings.resolved_sample_path` 中按仓库根目录解析。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas.common import DEFAULT_SOURCE_URL, DEFAULT_STATION_ID

#: 仓库根目录：``<root>/backend/app/config.py`` 向上三级。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DataMode = Literal["history_replay", "local_snapshot"]
LLMProviderName = Literal["local", "external"]


class Settings(BaseSettings):
    """Backend settings read from ``RIVER_SENTINEL_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="RIVER_SENTINEL_", env_file=".env", extra="ignore")

    #: 小型样例路径（相对仓库根目录）。
    sample_path: Path = Path("data") / "samples" / "water_level_sample.csv"
    #: 可选的本地产物目录；未配置时预测一律回退持久性基线。
    artifact_dir: Path | None = None
    #: 数据陈旧门槛（分钟），与 DS-7 ``stale_after_hours = 3.0`` 对齐。
    stale_after_minutes: int = Field(default=180, ge=1)
    #: Provider 选择；``external`` 未配置凭据时只会报告不可用。
    llm_provider: LLMProviderName = "local"
    #: 数据模式，默认历史回放。
    data_mode: DataMode = "history_replay"
    #: HTTP 路由前缀。
    api_prefix: str = "/api/v1"
    #: 目标断面。
    station_id: str = DEFAULT_STATION_ID
    #: 样例时间戳本地化所用时区。
    timezone: str = "Asia/Shanghai"
    #: 数据来源页面（教学科研用途，需账号或接口审批时按审批状态说明）。
    source_url: str = DEFAULT_SOURCE_URL
    #: 服务名与版本，写入健康检查与证据。
    service_name: str = "river-sentinel-backend"
    service_version: str = "0.1.0-ds8a"

    #: 外部 Provider（可选）。未配置时不可调用，也不影响本地模式。
    external_llm_base_url: str | None = None
    external_llm_model: str | None = None
    external_llm_api_key: SecretStr | None = None
    external_llm_timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def resolved_sample_path(self) -> Path:
        """Absolute sample path; relative values resolve against the repo root."""
        if self.sample_path.is_absolute():
            return self.sample_path
        return PROJECT_ROOT / self.sample_path

    @property
    def resolved_artifact_dir(self) -> Path | None:
        """Absolute artifact directory (``None`` when not configured)."""
        if self.artifact_dir is None:
            return None
        if self.artifact_dir.is_absolute():
            return self.artifact_dir
        return PROJECT_ROOT / self.artifact_dir

    @property
    def comparison_artifact_path(self) -> Path | None:
        """Optional model-comparison artifact inside the configured artifact dir."""
        directory = self.resolved_artifact_dir
        if directory is None:
            return None
        return directory / "model_comparison.json"

    @property
    def persistence_available(self) -> bool:
        """Whether the fallback baseline is usable (sample file exists)."""
        return self.resolved_sample_path.is_file()
