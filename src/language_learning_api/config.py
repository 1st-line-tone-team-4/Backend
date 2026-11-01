from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    jwt_public_key: str
    jwt_issuer: str
    jwt_audience: str
    jwt_algorithm: str = "RS256"
    cursor_secret: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
