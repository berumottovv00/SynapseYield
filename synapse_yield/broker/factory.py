"""Broker 工厂。

根据 .env 中的 BROKER_TYPE 决定创建哪种 Broker：
  - local_sim  : 本地模拟盘，不连接任何外部服务，用于开发和测试
  - longbridge : 长桥真实/模拟账户，需要配置 API 密钥和安全开关

对外只暴露 build_broker()，调用方无需感知底层实现。
"""

from synapse_yield.broker.base import BrokerAdapter
from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.broker.longbridge.adapter import (
    LongbridgeBroker,
    LongbridgeBrokerConfig,
)
from synapse_yield.broker.longbridge.gateway import (
    LongbridgeGateway,
    LongbridgeSDKGateway,
)
from synapse_yield.config import Settings, get_settings
from synapse_yield.harness.order_service import OrderService


# 根据 BROKER_TYPE 创建对应的 Broker 实例。
# longbridge_gateway 可在测试中注入 mock，不传则从 .env 密钥自动构建。
def build_broker(
    *,
    settings: Settings | None = None,
    order_service: OrderService | None = None,
    longbridge_gateway: LongbridgeGateway | None = None,
) -> BrokerAdapter:
    """根据配置创建本地模拟盘或长桥 Broker。"""

    current_settings = settings or get_settings()
    current_order_service = order_service or OrderService()
    broker_type = current_settings.broker_type.lower()
    if broker_type == "local_sim":
        return LocalSimBroker(order_service=current_order_service)
    if broker_type == "longbridge":
        gateway = longbridge_gateway or _build_longbridge_gateway(current_settings)
        return LongbridgeBroker(
            gateway,
            order_service=current_order_service,
            config=LongbridgeBrokerConfig(
                mode=current_settings.longbridge_mode,
                enable_order_submission=current_settings.enable_external_order_submission,
                enable_live_trading=current_settings.enable_live_trading,
            ),
        )
    raise ValueError(f"Unsupported BROKER_TYPE: {current_settings.broker_type}")


# 从 settings 读取长桥三件套（APP_KEY / APP_SECRET / ACCESS_TOKEN），
# 任一缺失则抛出明确错误，防止以空值连接长桥。
def _build_longbridge_gateway(settings: Settings) -> LongbridgeSDKGateway:
    """使用 Pydantic 从 .env 读取的密钥创建官方 SDK Gateway。"""

    credentials = {
        "LONGBRIDGE_APP_KEY": settings.longbridge_app_key,
        "LONGBRIDGE_APP_SECRET": settings.longbridge_app_secret,
        "LONGBRIDGE_ACCESS_TOKEN": settings.longbridge_access_token,
    }
    missing = [
        name
        for name, value in credentials.items()
        if value is None or not value.get_secret_value().strip()
    ]
    if missing:
        raise ValueError(
            "Missing Longbridge environment variables: " + ", ".join(missing)
        )

    return LongbridgeSDKGateway(
        app_key=settings.longbridge_app_key.get_secret_value(),
        app_secret=settings.longbridge_app_secret.get_secret_value(),
        access_token=settings.longbridge_access_token.get_secret_value(),
    )
