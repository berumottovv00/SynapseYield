from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = Field(default="dev", alias="APP_ENV")
    database_url: str = Field(
        default="mysql+pymysql://synapse:password@127.0.0.1:3306/synapse_yield",
        alias="DATABASE_URL",
    )
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    enable_external_order_submission: bool = Field(
        default=False,
        alias="ENABLE_EXTERNAL_ORDER_SUBMISSION",
    )
    broker_type: str = Field(default="local_sim", alias="BROKER_TYPE")
    longbridge_mode: str = Field(default="paper", alias="LONGBRIDGE_MODE")
    longbridge_app_key: SecretStr | None = Field(
        default=None,
        alias="LONGBRIDGE_APP_KEY",
    )
    longbridge_app_secret: SecretStr | None = Field(
        default=None,
        alias="LONGBRIDGE_APP_SECRET",
    )
    longbridge_access_token: SecretStr | None = Field(
        default=None,
        alias="LONGBRIDGE_ACCESS_TOKEN",
    )
    run_longbridge_integration: bool = Field(
        default=False,
        alias="RUN_LONGBRIDGE_INTEGRATION",
    )
    run_longbridge_paper_order: bool = Field(
        default=False,
        alias="RUN_LONGBRIDGE_PAPER_ORDER",
    )
    longbridge_test_symbol: str = Field(
        default="AAPL.US",
        alias="LONGBRIDGE_TEST_SYMBOL",
    )
    base_currency: str = Field(default="USD", alias="BASE_CURRENCY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
