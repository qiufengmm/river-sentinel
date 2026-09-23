"""Shared fixtures for the DS-8A backend tests."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from backend.app.config import Settings
from backend.app.services.container import ServiceContainer, build_container

TIMEZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def settings() -> Settings:
    """Default settings pointing at the small sample."""
    return Settings()


@pytest.fixture
def container(settings: Settings) -> ServiceContainer:
    """Default service container."""
    return build_container(settings)
