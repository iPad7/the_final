"""Process configuration read from environment variables (and `.env` in development)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Default points at the local docker-compose database (development credentials only).
    database_url: str = "postgresql+psycopg://fulfill:fulfill@localhost:5432/fulfill"


@lru_cache
def get_settings() -> Settings:
    return Settings()
