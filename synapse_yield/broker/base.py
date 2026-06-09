from typing import Protocol

from sqlalchemy.orm import Session

from synapse_yield.domain.schemas import BrokerOrderResult, MarketQuote
from synapse_yield.storage.models import Order


class BrokerAdapter(Protocol):
    """Harness 可调用的统一 Broker 接口。"""

    broker_name: str

    def submit_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote,
    ) -> BrokerOrderResult:
        """提交风控通过的订单。"""

    def cancel_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
    ) -> BrokerOrderResult:
        """撤销 Broker 已接收的订单。"""
