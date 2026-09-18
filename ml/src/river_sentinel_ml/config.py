"""Environment-backed project configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by file and API data-ingestion commands."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    wenzhou_data_appsecret: SecretStr | None = None
    timezone: str = Field(default="Asia/Shanghai", alias="RIVER_SENTINEL_TIMEZONE")
