"""A and B share explicit application configuration; no implicit .env loading."""

from app.core import Settings


def get_settings():
    return Settings.from_env()
