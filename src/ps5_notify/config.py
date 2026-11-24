from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from ps5_notify.models import TargetConfig
from ps5_notify.retailers import get_adapter, list_adapter_keys

VALID_FETCHER_BACKENDS = frozenset({"http", "playwright"})
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
VALID_ARGOS_FULFILMENT_MODES = frozenset({"either", "delivery", "collection"})
DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
    }
)


class ConfigError(ValueError):
    """Raised when environment configuration is invalid or incomplete."""


@dataclass(frozen=True)
class Settings:
    discord_webhook_url: str
    discord_mention: str | None
    targets: tuple[TargetConfig, ...]
    poll_interval_seconds: float = 30.0
    poll_jitter_seconds: float = 5.0
    restock_cooldown_seconds: float = 300.0
    error_alert_threshold: int = 5
    backoff_base_seconds: float = 60.0
    backoff_max_seconds: float = 1800.0
    http_timeout_seconds: float = 15.0
    state_file_path: Path = Path("data/state.json")
    log_level: str = "INFO"


def _parse_float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _parse_int(env: Mapping[str, str], key: str, default: int, *, minimum: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _validate_webhook_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ConfigError("DISCORD_WEBHOOK_URL is required (copy .env.example to .env and set it).")
    parsed = urlparse(cleaned)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in DISCORD_WEBHOOK_HOSTS
        or not parsed.path.startswith("/api/webhooks/")
    ):
        raise ConfigError(
            "DISCORD_WEBHOOK_URL must be a valid https://discord.com/api/webhooks/... URL."
        )
    if "YOUR_WEBHOOK_ID" in cleaned or "YOUR_WEBHOOK_TOKEN" in cleaned:
        raise ConfigError(
            "DISCORD_WEBHOOK_URL still contains placeholder values from .env.example."
        )
    return cleaned


def _validate_http_url(url: str, key: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{key} must be a valid http(s) URL, got {url!r}")
    return cleaned


def _validate_argos_postcode(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip()).upper()
    if not cleaned:
        raise ConfigError(
            "ARGOS_POSTCODE is required when 'argos' is enabled in ENABLED_RETAILERS "
            "(set a UK postcode or town in .env, e.g. ARGOS_POSTCODE=SW1A 1AA, "
            "or remove 'argos' from ENABLED_RETAILERS)."
        )
    if "YOUR_POSTCODE" in cleaned:
        raise ConfigError("ARGOS_POSTCODE still contains a placeholder value from .env.example.")
    return cleaned


def load_settings(
    env_file: Path | None = None,
    *,
    require_webhook: bool = True,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load and validate application settings from .env and process environment."""
    merged: dict[str, str] = {}

    if environ is None:
        dotenv_path = env_file if env_file is not None else Path(".env")
        if dotenv_path.is_file():
            for key, value in dotenv_values(dotenv_path).items():
                if key and value is not None:
                    merged[key] = value
        for key, value in os.environ.items():
            merged[key] = value
    else:
        if env_file is not None and env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                if key and value is not None:
                    merged[key] = value
        merged.update(environ)

    raw_webhook = merged.get("DISCORD_WEBHOOK_URL", "").strip()
    webhook_url = _validate_webhook_url(raw_webhook) if require_webhook else raw_webhook

    raw_mention = merged.get("DISCORD_MENTION", "").strip()
    discord_mention = raw_mention or None

    global_fetcher = merged.get("FETCHER_BACKEND", "").strip().lower()
    if global_fetcher and global_fetcher not in VALID_FETCHER_BACKENDS:
        raise ConfigError(
            f"FETCHER_BACKEND must be one of {sorted(VALID_FETCHER_BACKENDS)}, got {global_fetcher!r}"
        )

    raw_retailers = merged.get("ENABLED_RETAILERS", "playstation_uk").strip()
    retailer_keys = [item.strip().lower() for item in raw_retailers.split(",") if item.strip()]
    if not retailer_keys:
        raise ConfigError("ENABLED_RETAILERS must specify at least one retailer.")

    available = set(list_adapter_keys())
    targets: list[TargetConfig] = []
    seen_keys: set[str] = set()
    for retailer_key in retailer_keys:
        if retailer_key not in available:
            raise ConfigError(
                f"Unknown retailer {retailer_key!r} in ENABLED_RETAILERS. "
                f"Available: {', '.join(sorted(available))}"
            )
        if retailer_key in seen_keys:
            continue
        seen_keys.add(retailer_key)

        adapter = get_adapter(retailer_key)
        url_env_key = f"{retailer_key.upper()}_URL"
        raw_url = merged.get(url_env_key, "").strip() or adapter.default_url
        url = _validate_http_url(raw_url, url_env_key)

        fetcher_env_key = f"{retailer_key.upper()}_FETCHER"
        fetcher_override = merged.get(fetcher_env_key, "").strip().lower()
        fetcher_backend = fetcher_override or global_fetcher or adapter.default_fetcher
        if fetcher_backend not in VALID_FETCHER_BACKENDS:
            raise ConfigError(
                f"{fetcher_env_key} must be one of {sorted(VALID_FETCHER_BACKENDS)}, "
                f"got {fetcher_backend!r}"
            )

        target_options: list[tuple[str, str]] = []
        if retailer_key == "argos":
            postcode = _validate_argos_postcode(merged.get("ARGOS_POSTCODE", ""))
            fulfilment_mode = (
                merged.get("ARGOS_FULFILMENT_MODE", "either").strip().lower() or "either"
            )
            if fulfilment_mode not in VALID_ARGOS_FULFILMENT_MODES:
                raise ConfigError(
                    f"ARGOS_FULFILMENT_MODE must be one of {sorted(VALID_ARGOS_FULFILMENT_MODES)}, "
                    f"got {fulfilment_mode!r}"
                )
            if fetcher_backend != "playwright":
                raise ConfigError(
                    "Argos postcode stock checks require Playwright (set ARGOS_FETCHER=playwright "
                    "or unset FETCHER_BACKEND so Argos uses its Playwright default)."
                )
            target_options.append(("postcode", postcode))
            target_options.append(("fulfilment_mode", fulfilment_mode))

        targets.append(
            TargetConfig(
                retailer_key=retailer_key,
                url=url,
                fetcher_backend=fetcher_backend,
                options=tuple(target_options),
            )
        )

    poll_interval = _parse_float(merged, "POLL_INTERVAL_SECONDS", 30.0, minimum=5.0)
    poll_jitter = _parse_float(merged, "POLL_JITTER_SECONDS", 5.0, minimum=0.0)
    restock_cooldown = _parse_float(merged, "RESTOCK_COOLDOWN_SECONDS", 300.0, minimum=0.0)
    error_threshold = _parse_int(merged, "ERROR_ALERT_THRESHOLD", 5, minimum=1)
    backoff_base = _parse_float(merged, "BACKOFF_BASE_SECONDS", 60.0, minimum=5.0)
    backoff_max = _parse_float(merged, "BACKOFF_MAX_SECONDS", 1800.0, minimum=backoff_base)
    http_timeout = _parse_float(merged, "HTTP_TIMEOUT_SECONDS", 15.0, minimum=1.0)

    state_file_raw = merged.get("STATE_FILE_PATH", "data/state.json").strip() or "data/state.json"
    state_file_path = Path(state_file_raw)

    log_level = (merged.get("LOG_LEVEL", "INFO").strip() or "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}, got {log_level!r}"
        )

    return Settings(
        discord_webhook_url=webhook_url,
        discord_mention=discord_mention,
        targets=tuple(targets),
        poll_interval_seconds=poll_interval,
        poll_jitter_seconds=poll_jitter,
        restock_cooldown_seconds=restock_cooldown,
        error_alert_threshold=error_threshold,
        backoff_base_seconds=backoff_base,
        backoff_max_seconds=backoff_max,
        http_timeout_seconds=http_timeout,
        state_file_path=state_file_path,
        log_level=log_level,
    )
