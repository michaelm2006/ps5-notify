from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ps5_notify.config import Settings
from ps5_notify.discord import DiscordWebhookClient, DiscordWebhookError
from ps5_notify.fetcher import FetchError, PageFetcher
from ps5_notify.models import CheckResult, StockStatus, TargetConfig, utc_now
from ps5_notify.retailers import get_adapter
from ps5_notify.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckOutcome:
    target: TargetConfig
    result: CheckResult | None = None
    error: str | None = None
    restock_notified: bool = False
    suppressed_by_cooldown: bool = False
    error_notified: bool = False
    next_delay_seconds: float = 0.0


class StockMonitor:
    """Orchestrates per-retailer polling, jitter, exponential backoff, and Discord alerts."""

    def __init__(
        self,
        *,
        settings: Settings,
        state_store: StateStore,
        fetchers: dict[str, PageFetcher],
        notifier: DiscordWebhookClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._state = state_store
        self._fetchers = fetchers
        self._notifier = notifier
        self._clock = clock
        self._rng = rng or random.Random()
        self._next_check_at: dict[str, datetime] = {}

        now = self._clock()
        for target in settings.targets:
            retailer_state = self._state.get(target.retailer_key)
            if retailer_state.backoff_until and retailer_state.backoff_until > now:
                self._next_check_at[target.retailer_key] = retailer_state.backoff_until
            else:
                self._next_check_at[target.retailer_key] = now

    def compute_poll_delay(self) -> float:
        jitter = self._rng.uniform(
            -self._settings.poll_jitter_seconds,
            self._settings.poll_jitter_seconds,
        )
        return max(5.0, self._settings.poll_interval_seconds + jitter)

    def compute_backoff_delay(
        self,
        consecutive_errors: int,
        *,
        retry_after: float | None = None,
    ) -> float:
        exponent = max(0, consecutive_errors - 1)
        exp_delay = self._settings.backoff_base_seconds * (2**exponent)
        base_delay = min(self._settings.backoff_max_seconds, exp_delay)
        if retry_after is not None and retry_after > base_delay:
            base_delay = min(self._settings.backoff_max_seconds, retry_after)
        positive_jitter = self._rng.uniform(0.0, self._settings.poll_jitter_seconds)
        return min(self._settings.backoff_max_seconds, base_delay + positive_jitter)

    async def send_startup_notification(self) -> None:
        if self._notifier is None:
            logger.info("[dry-run] Would send quiet startup notification to Discord.")
            return

        target_summary = [
            (get_adapter(target.retailer_key).display_name, target.url)
            for target in self._settings.targets
        ]
        try:
            await self._notifier.send_startup_alert(
                targets=target_summary,
                poll_interval_seconds=self._settings.poll_interval_seconds,
                poll_jitter_seconds=self._settings.poll_jitter_seconds,
                restock_cooldown_seconds=self._settings.restock_cooldown_seconds,
                error_alert_threshold=self._settings.error_alert_threshold,
            )
            logger.info("Sent startup notification to Discord.")
        except DiscordWebhookError as exc:
            logger.warning("Failed to send startup notification to Discord: %s", exc)

    async def check_target(self, target: TargetConfig) -> CheckOutcome:
        adapter = get_adapter(target.retailer_key)
        fetcher = self._fetchers[target.fetcher_backend]
        state = self._state.get(target.retailer_key)
        now = self._clock()

        try:
            result = await adapter.check(
                fetcher,
                target.url,
                options=target.options_dict(),
            )
        except FetchError as exc:
            return await self._handle_error(
                target=target,
                retailer_name=adapter.display_name,
                error_message=exc.message,
                retry_after=exc.retry_after,
                now=now,
            )
        except Exception as exc:
            logger.exception("Unexpected error checking %s", adapter.display_name)
            return await self._handle_error(
                target=target,
                retailer_name=adapter.display_name,
                error_message=f"Unexpected error: {exc}",
                retry_after=None,
                now=now,
            )

        if result.status == StockStatus.UNKNOWN:
            return await self._handle_error(
                target=target,
                retailer_name=adapter.display_name,
                error_message=f"Could not determine stock status ({result.signal or 'unknown'})",
                retry_after=None,
                now=now,
                result=result,
            )

        previous_status = state.last_status
        was_erroring = state.consecutive_errors > 0
        state.consecutive_errors = 0
        state.last_error = None
        state.error_alert_sent = False
        state.backoff_until = None
        state.last_checked_at = now

        if was_erroring:
            logger.info("%s recovered and returned %s.", adapter.display_name, result.status.value)

        restock_notified = False
        suppressed_by_cooldown = False

        if result.status == StockStatus.IN_STOCK and previous_status != StockStatus.IN_STOCK:
            in_cooldown = (
                state.last_notified_at is not None
                and (now - state.last_notified_at).total_seconds()
                < self._settings.restock_cooldown_seconds
            )
            if in_cooldown:
                remaining = self._settings.restock_cooldown_seconds - (
                    now - state.last_notified_at
                ).total_seconds()
                logger.info(
                    "%s is IN STOCK (%s), but alert suppressed by flicker cooldown (%.0fs remaining).",
                    adapter.display_name,
                    result.signal,
                    max(0.0, remaining),
                )
                suppressed_by_cooldown = True
                state.last_status = StockStatus.IN_STOCK
            else:
                if self._notifier is None:
                    logger.info(
                        "[dry-run] RESTOCK DETECTED at %s: %s (%s) -> %s",
                        adapter.display_name,
                        result.title,
                        result.price or "price n/a",
                        result.url,
                    )
                    state.last_status = StockStatus.IN_STOCK
                    state.last_notified_at = now
                    restock_notified = True
                else:
                    try:
                        await self._notifier.send_restock_alert(result)
                        logger.info(
                            "RESTOCK ALERT SENT for %s: %s (%s) [%s]",
                            adapter.display_name,
                            result.title,
                            result.price or "price n/a",
                            result.signal,
                        )
                        state.last_status = StockStatus.IN_STOCK
                        state.last_notified_at = now
                        restock_notified = True
                    except DiscordWebhookError as exc:
                        # Do NOT advance last_status to IN_STOCK so the next poll retries notifying.
                        logger.error(
                            "Failed to deliver Discord restock alert for %s (will retry next poll): %s",
                            adapter.display_name,
                            exc,
                        )
        else:
            state.last_status = result.status
            logger.info(
                "%s: %s (%s) [price=%s, signal=%s]",
                adapter.display_name,
                result.status.value,
                result.title,
                result.price or "n/a",
                result.signal,
            )

        delay = self.compute_poll_delay()
        self._next_check_at[target.retailer_key] = now + timedelta(seconds=delay)
        self._state.save()

        return CheckOutcome(
            target=target,
            result=result,
            restock_notified=restock_notified,
            suppressed_by_cooldown=suppressed_by_cooldown,
            next_delay_seconds=delay,
        )

    async def _handle_error(
        self,
        *,
        target: TargetConfig,
        retailer_name: str,
        error_message: str,
        retry_after: float | None,
        now: datetime,
        result: CheckResult | None = None,
    ) -> CheckOutcome:
        state = self._state.get(target.retailer_key)
        state.consecutive_errors += 1
        state.last_error = error_message
        state.last_checked_at = now
        # Note: deliberately preserve state.last_status across transient errors.

        delay = self.compute_backoff_delay(
            state.consecutive_errors,
            retry_after=retry_after,
        )
        state.backoff_until = now + timedelta(seconds=delay)
        self._next_check_at[target.retailer_key] = state.backoff_until

        logger.warning(
            "%s check failed (%d consecutive): %s — backing off for %.1fs",
            retailer_name,
            state.consecutive_errors,
            error_message,
            delay,
        )

        error_notified = False
        if (
            state.consecutive_errors >= self._settings.error_alert_threshold
            and not state.error_alert_sent
        ):
            if self._notifier is None:
                logger.warning(
                    "[dry-run] Would send quiet Discord error alert for %s (%d consecutive errors).",
                    retailer_name,
                    state.consecutive_errors,
                )
                state.error_alert_sent = True
                error_notified = True
            else:
                try:
                    await self._notifier.send_error_alert(
                        retailer_name=retailer_name,
                        url=target.url,
                        consecutive_errors=state.consecutive_errors,
                        error_message=error_message,
                        backoff_seconds=delay,
                    )
                    state.error_alert_sent = True
                    error_notified = True
                    logger.warning(
                        "Sent quiet error alert to Discord for %s (%d consecutive failures).",
                        retailer_name,
                        state.consecutive_errors,
                    )
                except DiscordWebhookError as exc:
                    logger.error(
                        "Failed to send Discord error alert for %s: %s",
                        retailer_name,
                        exc,
                    )

        self._state.save()
        return CheckOutcome(
            target=target,
            result=result,
            error=error_message,
            error_notified=error_notified,
            next_delay_seconds=delay,
        )

    async def run_once(self) -> list[CheckOutcome]:
        """Run a single check pass across all configured targets."""
        tasks = [self.check_target(target) for target in self._settings.targets]
        return list(await asyncio.gather(*tasks))

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Continuously poll targets according to their individual schedules and backoffs."""
        await self.send_startup_notification()

        while not stop_event.is_set():
            now = self._clock()
            due_targets = [
                target
                for target in self._settings.targets
                if self._next_check_at.get(target.retailer_key, now) <= now
            ]

            if due_targets:
                await asyncio.gather(*(self.check_target(target) for target in due_targets))
                if stop_event.is_set():
                    break

            now_after = self._clock()
            next_due = min(self._next_check_at.values())
            wait_seconds = max(0.2, (next_due - now_after).total_seconds())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue
