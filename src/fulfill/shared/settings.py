"""환경변수(개발 시 `.env`)에서 읽는 프로세스 설정."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 기본값은 로컬 docker-compose DB를 가리킨다(개발용 자격 증명 전용).
    database_url: str = "postgresql+psycopg://fulfill:fulfill@localhost:5432/fulfill"


@lru_cache
def get_settings() -> Settings:
    return Settings()
