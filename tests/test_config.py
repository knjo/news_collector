from news_collector.config import Settings


def test_default_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.request_timeout_seconds == 30.0
