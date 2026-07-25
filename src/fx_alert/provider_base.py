from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .models import FxBar, FxQuote


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    configured: bool
    available: bool
    mode: str
    detail: str
    capabilities: list[str] = field(default_factory=list)
    websocket_available: bool = False
    rest_available: bool = False
    connection_status: str = "disconnected"
    last_quote_time: str | None = None
    data_age_seconds: float | None = None
    current_pair: str | None = None
    current_price: float | None = None
    daily_api_calls: int = 0
    estimated_cost_usd: float | None = None
    reported_cost_usd: float | None = None
    monthly_budget_usd: float | None = None
    errors: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "configured": self.configured,
            "available": self.available,
            "mode": self.mode,
            "detail": self.detail,
            "capabilities": self.capabilities,
            "websocket_available": self.websocket_available,
            "rest_available": self.rest_available,
            "connection_status": self.connection_status,
            "last_quote_time": self.last_quote_time,
            "data_age_seconds": self.data_age_seconds,
            "current_pair": self.current_pair,
            "current_price": self.current_price,
            "daily_api_calls": self.daily_api_calls,
            "estimated_cost_usd": self.estimated_cost_usd,
            "reported_cost_usd": self.reported_cost_usd,
            "monthly_budget_usd": self.monthly_budget_usd,
            "errors": self.errors,
        }


class FxDataProvider(ABC):
    name: str

    @abstractmethod
    def status(self, *, probe: bool = False) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def get_quote(self, pair: str) -> FxQuote:
        raise NotImplementedError

    @abstractmethod
    def get_bars(self, pair: str, *, interval: str, outputsize: int) -> list[FxBar]:
        raise NotImplementedError
