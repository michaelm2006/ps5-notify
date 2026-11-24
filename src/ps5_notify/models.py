from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_iso_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TargetConfig:
    retailer_key: str
    url: str
    fetcher_backend: str = "http"
    options: tuple[tuple[str, str], ...] = ()

    def options_dict(self) -> dict[str, str]:
        return dict(self.options)


@dataclass(frozen=True)
class CheckResult:
    retailer_key: str
    retailer_name: str
    url: str
    status: StockStatus
    title: str
    price: str | None = None
    image_url: str | None = None
    signal: str = ""
    checked_at: datetime = field(default_factory=utc_now)


@dataclass
class RetailerState:
    last_status: StockStatus | None = None
    last_checked_at: datetime | None = None
    last_notified_at: datetime | None = None
    consecutive_errors: int = 0
    last_error: str | None = None
    error_alert_sent: bool = False
    backoff_until: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_status": self.last_status.value if self.last_status else None,
            "last_checked_at": _format_iso_dt(self.last_checked_at),
            "last_notified_at": _format_iso_dt(self.last_notified_at),
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "error_alert_sent": self.error_alert_sent,
            "backoff_until": _format_iso_dt(self.backoff_until),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetailerState:
        raw_status = data.get("last_status")
        status: StockStatus | None = None
        if isinstance(raw_status, str):
            try:
                status = StockStatus(raw_status)
            except ValueError:
                status = None

        return cls(
            last_status=status,
            last_checked_at=_parse_iso_dt(data.get("last_checked_at")),
            last_notified_at=_parse_iso_dt(data.get("last_notified_at")),
            consecutive_errors=max(0, int(data.get("consecutive_errors", 0) or 0)),
            last_error=data.get("last_error") if isinstance(data.get("last_error"), str) else None,
            error_alert_sent=bool(data.get("error_alert_sent", False)),
            backoff_until=_parse_iso_dt(data.get("backoff_until")),
        )
