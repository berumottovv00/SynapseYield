from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = Field(default="dev", alias="APP_ENV")
    database_url: str = Field(
        default="mysql+pymysql://synapse:password@127.0.0.1:3306/synapse_yield",
        alias="DATABASE_URL",
    )
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    base_currency: str = Field(default="USD", alias="BASE_CURRENCY")


@lru_cache
def get_settings() -> Settings:
    return Settings()

