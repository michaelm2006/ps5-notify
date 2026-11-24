from __future__ import annotations

from pathlib import Path

import pytest

from ps5_notify.config import ConfigError, load_settings

VALID_WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefTOKEN"


def test_load_settings_defaults():
    settings = load_settings(environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK})
    assert settings.discord_webhook_url == VALID_WEBHOOK
    assert settings.discord_mention is None
    assert [t.retailer_key for t in settings.targets] == ["playstation_uk"]
    assert settings.targets[0].url == "https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/"
    assert settings.targets[0].fetcher_backend == "http"
    assert settings.poll_interval_seconds == 30.0
    assert settings.poll_jitter_seconds == 5.0
    assert settings.restock_cooldown_seconds == 300.0
    assert settings.error_alert_threshold == 5


def test_load_settings_with_argos_enabled():
    settings = load_settings(
        environ={
            "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
            "ENABLED_RETAILERS": "playstation_uk,argos",
            "ARGOS_POSTCODE": "sw1a   1aa",
        }
    )
    assert [t.retailer_key for t in settings.targets] == ["playstation_uk", "argos"]
    assert settings.targets[0].fetcher_backend == "http"
    assert settings.targets[1].url == "https://www.argos.co.uk/product/7797426"
    assert settings.targets[1].fetcher_backend == "playwright"
    assert settings.targets[1].options_dict() == {
        "postcode": "SW1A 1AA",
        "fulfilment_mode": "either",
    }


def test_load_settings_overrides_and_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK}",
                "DISCORD_MENTION=<@999>",
                "ENABLED_RETAILERS=argos,playstation_uk",
                "ARGOS_POSTCODE=E1 6AN",
                "ARGOS_FULFILMENT_MODE=delivery",
                "PLAYSTATION_UK_FETCHER=playwright",
                "POLL_INTERVAL_SECONDS=45",
                "POLL_JITTER_SECONDS=3",
                "ERROR_ALERT_THRESHOLD=3",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(
        env_file=env_file,
        environ={"POLL_INTERVAL_SECONDS": "60"},
    )
    assert settings.discord_mention == "<@999>"
    assert [t.retailer_key for t in settings.targets] == ["argos", "playstation_uk"]
    assert settings.targets[0].fetcher_backend == "playwright"
    assert settings.targets[0].options_dict() == {
        "postcode": "E1 6AN",
        "fulfilment_mode": "delivery",
    }
    assert settings.targets[1].fetcher_backend == "playwright"
    assert settings.poll_interval_seconds == 60.0
    assert settings.error_alert_threshold == 3


def test_missing_webhook_rejected_unless_optional():
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_settings(environ={})

    settings = load_settings(environ={}, require_webhook=False)
    assert settings.discord_webhook_url == ""


def test_argos_config_validation():
    with pytest.raises(ConfigError, match="ARGOS_POSTCODE"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "ENABLED_RETAILERS": "playstation_uk,argos",
            }
        )

    with pytest.raises(ConfigError, match="placeholder"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "ENABLED_RETAILERS": "argos",
                "ARGOS_POSTCODE": "YOUR_POSTCODE",
            }
        )

    with pytest.raises(ConfigError, match="ARGOS_FULFILMENT_MODE"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "ENABLED_RETAILERS": "argos",
                "ARGOS_POSTCODE": "SW1A 1AA",
                "ARGOS_FULFILMENT_MODE": "same_day_drone",
            }
        )

    with pytest.raises(ConfigError, match="require Playwright"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "ENABLED_RETAILERS": "argos",
                "ARGOS_POSTCODE": "SW1A 1AA",
                "ARGOS_FETCHER": "http",
            }
        )


def test_placeholder_and_invalid_webhook_rejected():
    with pytest.raises(ConfigError, match="placeholder"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN",
            }
        )

    with pytest.raises(ConfigError, match="valid https://discord.com/api/webhooks/"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": "https://example.com/hook",
            }
        )


def test_invalid_retailer_and_numeric_bounds():
    with pytest.raises(ConfigError, match="Unknown retailer"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "ENABLED_RETAILERS": "nonexistent_store",
            }
        )

    with pytest.raises(ConfigError, match="BACKOFF_MAX_SECONDS"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "BACKOFF_BASE_SECONDS": "120",
                "BACKOFF_MAX_SECONDS": "60",
            }
        )

    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        load_settings(
            environ={
                "DISCORD_WEBHOOK_URL": VALID_WEBHOOK,
                "POLL_INTERVAL_SECONDS": "2",
            }
        )
