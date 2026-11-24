from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from ps5_notify.models import CheckResult, utc_now

logger = logging.getLogger(__name__)

COLOR_RESTOCK_GREEN = 0x2ECC71
COLOR_ERROR_RED = 0xE74C3C
COLOR_STARTUP_BLUE = 0x0070D1
COLOR_TEST_BLURPLE = 0x5865F2


class DiscordWebhookError(Exception):
    """Raised when delivering a message to Discord fails."""


class DiscordWebhookClient:
    """Async client for delivering rich embeds to a Discord webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        mention: str | None = None,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._mention = mention.strip() if mention and mention.strip() else None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, payload: dict[str, Any]) -> None:
        if not self._webhook_url:
            raise DiscordWebhookError("DISCORD_WEBHOOK_URL is not configured.")

        for attempt in range(2):
            try:
                response = await self._client.post(self._webhook_url, json=payload)
            except httpx.HTTPError as exc:
                raise DiscordWebhookError(f"Discord webhook request failed: {exc}") from exc

            if response.status_code == 429 and attempt == 0:
                retry_after = self._extract_retry_after(response)
                wait_seconds = min(max(retry_after, 0.5), 10.0)
                logger.warning("Discord webhook rate-limited (429); retrying in %.2fs", wait_seconds)
                await asyncio.sleep(wait_seconds)
                continue

            if response.status_code >= 400:
                body_preview = response.text[:300].strip()
                raise DiscordWebhookError(
                    f"Discord webhook returned HTTP {response.status_code}: {body_preview}"
                )
            return

    @staticmethod
    def _extract_retry_after(response: httpx.Response) -> float:
        try:
            body = response.json()
            if isinstance(body, dict) and "retry_after" in body:
                return float(body["retry_after"])
        except Exception:
            pass
        header_val = response.headers.get("retry-after")
        if header_val:
            try:
                return float(header_val)
            except ValueError:
                pass
        return 1.0

    def build_restock_payload(self, result: CheckResult) -> dict[str, Any]:
        fields: list[dict[str, Any]] = [
            {"name": "Retailer", "value": result.retailer_name, "inline": True},
            {"name": "Status", "value": "In Stock", "inline": True},
        ]
        if result.price:
            fields.append({"name": "Price", "value": result.price, "inline": True})

        embed: dict[str, Any] = {
            "title": f"IN STOCK: {result.title}",
            "url": result.url,
            "description": f"[Buy Now on {result.retailer_name}]({result.url})",
            "color": COLOR_RESTOCK_GREEN,
            "fields": fields,
            "timestamp": result.checked_at.isoformat(),
            "footer": {"text": f"ps5-notify • {result.signal}" if result.signal else "ps5-notify"},
        }
        if result.image_url:
            embed["thumbnail"] = {"url": result.image_url}

        payload: dict[str, Any] = {"embeds": [embed]}
        if self._mention:
            payload["content"] = self._mention
        return payload

    async def send_restock_alert(self, result: CheckResult) -> None:
        await self._post(self.build_restock_payload(result))

    def build_error_payload(
        self,
        *,
        retailer_name: str,
        url: str,
        consecutive_errors: int,
        error_message: str,
        backoff_seconds: float,
    ) -> dict[str, Any]:
        # Intentionally quiet: never attaches self._mention.
        embed: dict[str, Any] = {
            "title": f"Checker Warning: {retailer_name}",
            "url": url,
            "description": (
                f"**{retailer_name}** has failed **{consecutive_errors}** times in a row. "
                f"Backing off for **{int(round(backoff_seconds))}s** before retrying."
            ),
            "color": COLOR_ERROR_RED,
            "fields": [
                {"name": "Last Error", "value": error_message[:1000] or "Unknown error", "inline": False},
                {"name": "Target URL", "value": url, "inline": False},
            ],
            "timestamp": utc_now().isoformat(),
            "footer": {"text": "ps5-notify • quiet alert"},
        }
        return {"embeds": [embed]}

    async def send_error_alert(
        self,
        *,
        retailer_name: str,
        url: str,
        consecutive_errors: int,
        error_message: str,
        backoff_seconds: float,
    ) -> None:
        await self._post(
            self.build_error_payload(
                retailer_name=retailer_name,
                url=url,
                consecutive_errors=consecutive_errors,
                error_message=error_message,
                backoff_seconds=backoff_seconds,
            )
        )

    def build_startup_payload(
        self,
        *,
        targets: Sequence[tuple[str, str]],
        poll_interval_seconds: float,
        poll_jitter_seconds: float,
        restock_cooldown_seconds: float,
        error_alert_threshold: int,
    ) -> dict[str, Any]:
        # Intentionally quiet: never attaches self._mention.
        target_lines = "\n".join(f"• **{name}** — {url}" for name, url in targets) or "None"
        embed: dict[str, Any] = {
            "title": "PS5 Pro Stock Notifier Started",
            "description": f"Monitoring **{len(targets)}** UK target(s):\n{target_lines}",
            "color": COLOR_STARTUP_BLUE,
            "fields": [
                {
                    "name": "Poll Interval",
                    "value": f"{poll_interval_seconds:g}s (±{poll_jitter_seconds:g}s)",
                    "inline": True,
                },
                {
                    "name": "Flicker Cooldown",
                    "value": f"{restock_cooldown_seconds:g}s",
                    "inline": True,
                },
                {
                    "name": "Error Alert Threshold",
                    "value": f"{error_alert_threshold} consecutive failures",
                    "inline": True,
                },
            ],
            "timestamp": utc_now().isoformat(),
            "footer": {"text": "ps5-notify • startup"},
        }
        return {"embeds": [embed]}

    async def send_startup_alert(
        self,
        *,
        targets: Sequence[tuple[str, str]],
        poll_interval_seconds: float,
        poll_jitter_seconds: float,
        restock_cooldown_seconds: float,
        error_alert_threshold: int,
    ) -> None:
        await self._post(
            self.build_startup_payload(
                targets=targets,
                poll_interval_seconds=poll_interval_seconds,
                poll_jitter_seconds=poll_jitter_seconds,
                restock_cooldown_seconds=restock_cooldown_seconds,
                error_alert_threshold=error_alert_threshold,
            )
        )

    def build_test_payload(self) -> dict[str, Any]:
        mention_note = f"Configured restock mention: `{self._mention}`" if self._mention else "No restock mention configured (`DISCORD_MENTION` is empty)."
        embed: dict[str, Any] = {
            "title": "PS5 Pro Stock Notifier — Test Alert",
            "description": (
                "Your Discord webhook is configured and reachable.\n"
                f"{mention_note}"
            ),
            "color": COLOR_TEST_BLURPLE,
            "timestamp": utc_now().isoformat(),
            "footer": {"text": "ps5-notify • webhook test"},
        }
        payload: dict[str, Any] = {"embeds": [embed]}
        if self._mention:
            payload["content"] = f"{self._mention} (webhook test)"
        return payload

    async def send_test_alert(self) -> None:
        await self._post(self.build_test_payload())
