from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from ps5_notify.discord import DiscordWebhookClient, DiscordWebhookError
from ps5_notify.models import CheckResult, StockStatus

WEBHOOK_URL = "https://discord.com/api/webhooks/12345/secret-token"


@pytest.mark.asyncio
async def test_restock_payload_includes_mention_and_embed_fields():
    result = CheckResult(
        retailer_key="playstation_uk",
        retailer_name="PlayStation UK",
        url="https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/",
        status=StockStatus.IN_STOCK,
        title="PlayStation 5 Pro Console - 2 TB",
        price="£789.99",
        image_url="https://example.com/ps5pro.png",
        signal="jsonld:InStock",
        checked_at=datetime(2026, 11, 24, 21, 0, 0, tzinfo=timezone.utc),
    )

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        client = DiscordWebhookClient(WEBHOOK_URL, mention="<@123456>")
        try:
            await client.send_restock_alert(result)
        finally:
            await client.close()

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["content"] == "<@123456>"
    embed = body["embeds"][0]
    assert "PlayStation 5 Pro Console - 2 TB" in embed["title"]
    assert embed["url"] == result.url
    assert embed["thumbnail"]["url"] == result.image_url
    field_map = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_map["Retailer"] == "PlayStation UK"
    assert field_map["Price"] == "£789.99"


@pytest.mark.asyncio
async def test_error_and_startup_alerts_never_attach_mention():
    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        client = DiscordWebhookClient(WEBHOOK_URL, mention="@everyone")
        try:
            await client.send_startup_alert(
                targets=[("PlayStation UK", "https://example.com/ps")],
                poll_interval_seconds=30,
                poll_jitter_seconds=5,
                restock_cooldown_seconds=300,
                error_alert_threshold=5,
            )
            await client.send_error_alert(
                retailer_name="Argos UK",
                url="https://www.argos.co.uk/product/7797426",
                consecutive_errors=5,
                error_message="HTTP 403 Forbidden",
                backoff_seconds=480,
            )
        finally:
            await client.close()

    assert len(route.calls) == 2
    for call in route.calls:
        body = json.loads(call.request.content)
        assert "content" not in body


@pytest.mark.asyncio
async def test_discord_retries_once_on_429():
    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(
            side_effect=[
                httpx.Response(429, json={"retry_after": 0.01}),
                httpx.Response(204),
            ]
        )
        client = DiscordWebhookClient(WEBHOOK_URL)
        try:
            await client.send_test_alert()
        finally:
            await client.close()

    assert len(route.calls) == 2


@pytest.mark.asyncio
async def test_discord_raises_on_http_error():
    with respx.mock:
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(401, text="Invalid Webhook Token"))
        client = DiscordWebhookClient(WEBHOOK_URL)
        try:
            with pytest.raises(DiscordWebhookError, match="HTTP 401"):
                await client.send_test_alert()
        finally:
            await client.close()
