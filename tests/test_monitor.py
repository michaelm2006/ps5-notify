from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ps5_notify.config import Settings
from ps5_notify.discord import DiscordWebhookError
from ps5_notify.fetcher import FetchError, PageResponse
from ps5_notify.models import CheckResult, StockStatus, TargetConfig
from ps5_notify.monitor import StockMonitor
from ps5_notify.state import StateStore

PS_URL = "https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/"
ARGOS_URL = "https://www.argos.co.uk/product/7797426"

PS_OOS_HTML = """
<html><head><script type="application/ld+json">
{"@type":"Product","name":"PS5 Pro","offers":{"price":"789.99","priceCurrency":"GBP","availability":"https://schema.org/OutOfStock"}}
</script></head><body></body></html>
"""

PS_IN_STOCK_HTML = """
<html><head><script type="application/ld+json">
{"@type":"Product","name":"PS5 Pro","offers":{"price":"789.99","priceCurrency":"GBP","availability":"https://schema.org/InStock"}}
</script></head><body></body></html>
"""

ARGOS_OOS_HTML = """
<html><body><div data-test="out-of-stock-message">Out of stock</div></body></html>
"""


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class ScriptedFetcher:
    def __init__(self, responses: dict[str, list[PageResponse | Exception]]) -> None:
        self._responses = {url: list(items) for url, items in responses.items()}
        self.calls: list[str] = []

    async def fetch(self, url: str, *, on_page: Any = None) -> PageResponse:
        self.calls.append(url)
        queue = self._responses[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        return None


class FakeNotifier:
    def __init__(self, *, fail_restock: bool = False) -> None:
        self.restocks: list[CheckResult] = []
        self.errors: list[dict[str, Any]] = []
        self.startups: int = 0
        self.fail_restock = fail_restock

    async def send_restock_alert(self, result: CheckResult) -> None:
        if self.fail_restock:
            raise DiscordWebhookError("Simulated Discord 500")
        self.restocks.append(result)

    async def send_error_alert(self, **kwargs: Any) -> None:
        self.errors.append(kwargs)

    async def send_startup_alert(self, **kwargs: Any) -> None:
        self.startups += 1


def _page(url: str, html: str) -> PageResponse:
    return PageResponse(url=url, final_url=url, status_code=200, html=html, headers={})


def _settings(tmp_path: Path, *, targets: tuple[TargetConfig, ...], **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "discord_webhook_url": "https://discord.com/api/webhooks/1/tok",
        "discord_mention": "@everyone",
        "targets": targets,
        "poll_interval_seconds": 30.0,
        "poll_jitter_seconds": 0.0,
        "restock_cooldown_seconds": 300.0,
        "error_alert_threshold": 3,
        "backoff_base_seconds": 60.0,
        "backoff_max_seconds": 600.0,
        "state_file_path": tmp_path / "state.json",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_transition_dedup_and_flicker_cooldown(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc))
    target = TargetConfig(retailer_key="playstation_uk", url=PS_URL)
    fetcher = ScriptedFetcher(
        {
            PS_URL: [
                _page(PS_URL, PS_OOS_HTML),  # 1: OOS (no alert)
                _page(PS_URL, PS_IN_STOCK_HTML),  # 2: OOS -> IN_STOCK (ALERT #1)
                _page(PS_URL, PS_IN_STOCK_HTML),  # 3: stays IN_STOCK (no alert)
                _page(PS_URL, PS_OOS_HTML),  # 4: drops back to OOS
                _page(PS_URL, PS_IN_STOCK_HTML),  # 5: flickers IN_STOCK within cooldown (suppressed)
                _page(PS_URL, PS_OOS_HTML),  # 6: OOS again
                _page(PS_URL, PS_IN_STOCK_HTML),  # 7: IN_STOCK after cooldown expires (ALERT #2)
            ]
        }
    )
    notifier = FakeNotifier()
    store = StateStore(tmp_path / "state.json")
    monitor = StockMonitor(
        settings=_settings(tmp_path, targets=(target,)),
        state_store=store,
        fetchers={"http": fetcher},
        notifier=notifier,
        clock=clock,
        rng=random.Random(0),
    )

    o1 = await monitor.check_target(target)
    assert o1.restock_notified is False
    assert len(notifier.restocks) == 0

    clock.advance(30)
    o2 = await monitor.check_target(target)
    assert o2.restock_notified is True
    assert len(notifier.restocks) == 1

    clock.advance(30)
    o3 = await monitor.check_target(target)
    assert o3.restock_notified is False
    assert len(notifier.restocks) == 1

    clock.advance(30)
    await monitor.check_target(target)

    clock.advance(30)  # only 90s since alert #1 (< 300s cooldown)
    o5 = await monitor.check_target(target)
    assert o5.restock_notified is False
    assert o5.suppressed_by_cooldown is True
    assert len(notifier.restocks) == 1

    clock.advance(300)
    await monitor.check_target(target)  # OOS
    clock.advance(30)
    o7 = await monitor.check_target(target)  # IN_STOCK after cooldown
    assert o7.restock_notified is True
    assert len(notifier.restocks) == 2


@pytest.mark.asyncio
async def test_transient_error_preserves_sticky_in_stock_status(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc))
    target = TargetConfig(retailer_key="playstation_uk", url=PS_URL)
    fetcher = ScriptedFetcher(
        {
            PS_URL: [
                _page(PS_URL, PS_IN_STOCK_HTML),  # initial IN_STOCK -> ALERT #1
                FetchError("HTTP 503", status_code=503),  # transient error
                _page(PS_URL, PS_IN_STOCK_HTML),  # recovers still IN_STOCK -> must NOT double-alert
            ]
        }
    )
    notifier = FakeNotifier()
    monitor = StockMonitor(
        settings=_settings(tmp_path, targets=(target,)),
        state_store=StateStore(tmp_path / "state.json"),
        fetchers={"http": fetcher},
        notifier=notifier,
        clock=clock,
        rng=random.Random(0),
    )

    await monitor.check_target(target)
    assert len(notifier.restocks) == 1

    clock.advance(30)
    err_outcome = await monitor.check_target(target)
    assert err_outcome.error is not None

    clock.advance(600)
    rec_outcome = await monitor.check_target(target)
    assert rec_outcome.restock_notified is False
    assert len(notifier.restocks) == 1


@pytest.mark.asyncio
async def test_webhook_failure_retries_restock_alert_on_next_poll(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc))
    target = TargetConfig(retailer_key="playstation_uk", url=PS_URL)
    fetcher = ScriptedFetcher(
        {
            PS_URL: [
                _page(PS_URL, PS_OOS_HTML),
                _page(PS_URL, PS_IN_STOCK_HTML),
                _page(PS_URL, PS_IN_STOCK_HTML),
            ]
        }
    )
    notifier = FakeNotifier(fail_restock=True)
    store = StateStore(tmp_path / "state.json")
    monitor = StockMonitor(
        settings=_settings(tmp_path, targets=(target,)),
        state_store=store,
        fetchers={"http": fetcher},
        notifier=notifier,
        clock=clock,
        rng=random.Random(0),
    )

    await monitor.check_target(target)
    clock.advance(30)
    o_fail = await monitor.check_target(target)
    assert o_fail.restock_notified is False
    assert store.get("playstation_uk").last_status == StockStatus.OUT_OF_STOCK

    notifier.fail_restock = False
    clock.advance(30)
    o_retry = await monitor.check_target(target)
    assert o_retry.restock_notified is True
    assert len(notifier.restocks) == 1


@pytest.mark.asyncio
async def test_exponential_backoff_and_single_error_alert_with_reset(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc))
    target = TargetConfig(retailer_key="playstation_uk", url=PS_URL)
    fetcher = ScriptedFetcher(
        {
            PS_URL: [
                FetchError("HTTP 403 Forbidden", status_code=403),  # err 1 -> 60s
                FetchError("HTTP 429", status_code=429, retry_after=180.0),  # err 2 -> max(120, 180) = 180s
                FetchError("HTTP 403 Forbidden", status_code=403),  # err 3 -> 240s + ALERT #1
                FetchError("HTTP 403 Forbidden", status_code=403),  # err 4 -> 480s (no duplicate alert)
                _page(PS_URL, PS_OOS_HTML),  # recovery resets consecutive_errors & error_alert_sent
                FetchError("HTTP 403", status_code=403),
                FetchError("HTTP 403", status_code=403),
                FetchError("HTTP 403", status_code=403),  # reaches threshold again -> ALERT #2
            ]
        }
    )
    notifier = FakeNotifier()
    monitor = StockMonitor(
        settings=_settings(tmp_path, targets=(target,), error_alert_threshold=3),
        state_store=StateStore(tmp_path / "state.json"),
        fetchers={"http": fetcher},
        notifier=notifier,
        clock=clock,
        rng=random.Random(0),
    )

    o1 = await monitor.check_target(target)
    assert o1.next_delay_seconds == 60.0
    assert o1.error_notified is False

    o2 = await monitor.check_target(target)
    assert o2.next_delay_seconds == 180.0
    assert o2.error_notified is False

    o3 = await monitor.check_target(target)
    assert o3.next_delay_seconds == 240.0
    assert o3.error_notified is True
    assert len(notifier.errors) == 1

    o4 = await monitor.check_target(target)
    assert o4.next_delay_seconds == 480.0
    assert o4.error_notified is False
    assert len(notifier.errors) == 1

    o5 = await monitor.check_target(target)
    assert o5.error is None

    for _ in range(3):
        await monitor.check_target(target)
    assert len(notifier.errors) == 2


@pytest.mark.asyncio
async def test_run_forever_sends_startup_and_isolates_retailer_backoff(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc))
    ps_target = TargetConfig(retailer_key="playstation_uk", url=PS_URL)
    argos_target = TargetConfig(
        retailer_key="argos",
        url=ARGOS_URL,
        fetcher_backend="playwright",
        options=(("postcode", "SW1A 1AA"), ("fulfilment_mode", "either")),
    )
    fetcher = ScriptedFetcher(
        {
            PS_URL: [FetchError("HTTP 403", status_code=403)],
            ARGOS_URL: [_page(ARGOS_URL, ARGOS_OOS_HTML)],
        }
    )
    notifier = FakeNotifier()
    monitor = StockMonitor(
        settings=_settings(tmp_path, targets=(ps_target, argos_target)),
        state_store=StateStore(tmp_path / "state.json"),
        fetchers={"http": fetcher, "playwright": fetcher},
        notifier=notifier,
        clock=clock,
        rng=random.Random(0),
    )

    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.gather(monitor.run_forever(stop_event), stop_soon())

    assert notifier.startups == 1
    # PlayStation UK backed off to +60s while Argos scheduled cleanly at +30s
    assert monitor._next_check_at["playstation_uk"] == clock.current + timedelta(seconds=60)
    assert monitor._next_check_at["argos"] == clock.current + timedelta(seconds=30)
