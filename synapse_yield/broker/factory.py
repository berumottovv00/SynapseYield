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
