"""``Settings`` 默认值与环境覆盖（DS-8A）。

覆盖点：默认走历史回放 + 小型样例；本地路径只由配置传入，不写死绝对路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.config import PROJECT_ROOT, Settings


def test_defaults_use_small_sample_and_history_replay() -> None:
    settings = Settings()
    assert settings.sample_path.name == "water_level_sample.csv"
    assert settings.data_mode == "history_replay"
    assert settings.llm_provider == "local"
    assert settings.api_prefix == "/api/v1"
    assert settings.stale_after_minutes == 180


def test_sample_path_is_never_hardcoded_absolute() -> None:
    settings = Settings()
    assert not settings.sample_path.is_absolute()
    resolved = settings.resolved_sample_path
    assert resolved.is_absolute()
    assert resolved == PROJECT_ROOT / settings.sample_path
    assert resolved.is_file()


def test_artifact_dir_defaults_to_none() -> None:
    settings = Settings()
    assert settings.resolved_artifact_dir is None
    assert settings.comparison_artifact_path is None
    assert settings.persistence_available is True


def test_artifact_dir_accepts_explicit_path(tmp_path: Path) -> None:
    settings = Settings(artifact_dir=tmp_path)
    assert settings.resolved_artifact_dir == tmp_path
    assert settings.comparison_artifact_path == tmp_path / "model_comparison.json"


def test_invalid_data_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(data_mode="realtime")


def test_invalid_llm_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="gpt")


def test_stale_after_minutes_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(stale_after_minutes=0)
