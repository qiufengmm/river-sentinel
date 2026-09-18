from river_sentinel_ml.config import Settings


def test_secret_is_optional_for_file_import(monkeypatch) -> None:
    monkeypatch.delenv("WENZHOU_DATA_APPSECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.wenzhou_data_appsecret is None
    assert settings.timezone == "Asia/Shanghai"


def test_secret_is_loaded_from_environment_without_plaintext_repr(monkeypatch) -> None:
    monkeypatch.setenv("WENZHOU_DATA_APPSECRET", "approved-secret")

    settings = Settings(_env_file=None)

    assert settings.wenzhou_data_appsecret is not None
    assert settings.wenzhou_data_appsecret.get_secret_value() == "approved-secret"
    assert "approved-secret" not in repr(settings)
