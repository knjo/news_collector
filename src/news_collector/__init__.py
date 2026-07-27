"""News collection and analysis package."""

from news_collector.config import Settings

__all__ = ["Settings"]


def main() -> None:
    """Run the command-line entry point."""
    settings = Settings()
    print(f"news-collector ready (environment={settings.environment})")
