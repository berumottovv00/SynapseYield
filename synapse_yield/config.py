from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8")

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
    enable_llm_trading_agent: bool = Field(
        default=False,
        alias="ENABLE_LLM_TRADING_AGENT",
    )
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.4-mini", alias="OPENAI_MODEL")
    base_currency: str = Field(default="USD", alias="BASE_CURRENCY")
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=7878, alias="WEB_PORT")


# 只在第一次调用时真正执行，之后每次调用都直接返回缓存的同一个 Settings 实例。
# Settings() 每次创建都会读取 .env 文件和环境变量，有 IO 开销。用 lru_cache 相当于把它变成全局单例，整个进程生命周期里只初始化一次。
@lru_cache
def get_settings() -> Settings:
    return Settings()
