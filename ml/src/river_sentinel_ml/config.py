"""Environment-backed project configuration."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by file and API data-ingestion commands."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    wenzhou_data_appsecret: SecretStr | None = None
    timezone: str = Field(default="Asia/Shanghai", alias="RIVER_SENTINEL_TIMEZONE")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"timezone must be a valid IANA name, got {value!r}") from exc
        return value
