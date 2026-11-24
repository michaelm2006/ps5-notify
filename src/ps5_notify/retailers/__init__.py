from __future__ import annotations

from ps5_notify.retailers.argos import ArgosAdapter
from ps5_notify.retailers.base import RetailerAdapter
from ps5_notify.retailers.playstation_uk import PlayStationUKAdapter

_ADAPTER_REGISTRY: dict[str, type[RetailerAdapter]] = {
    PlayStationUKAdapter.key: PlayStationUKAdapter,
    ArgosAdapter.key: ArgosAdapter,
}


def register_adapter(adapter_cls: type[RetailerAdapter]) -> None:
    """Register a custom retailer adapter class by its key."""
    _ADAPTER_REGISTRY[adapter_cls.key.lower()] = adapter_cls


def get_adapter(key: str) -> RetailerAdapter:
    normalized = key.strip().lower()
    adapter_cls = _ADAPTER_REGISTRY.get(normalized)
    if adapter_cls is None:
        available = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise KeyError(f"Unknown retailer adapter {key!r}. Available: {available}")
    return adapter_cls()


def list_adapter_keys() -> list[str]:
    return sorted(_ADAPTER_REGISTRY)


__all__ = [
    "ArgosAdapter",
    "PlayStationUKAdapter",
    "RetailerAdapter",
    "get_adapter",
    "list_adapter_keys",
    "register_adapter",
]
