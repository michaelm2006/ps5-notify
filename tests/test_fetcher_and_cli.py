from __future__ import annotations

import httpx
import pytest
import respx

from ps5_notify.cli import main
from ps5_notify.fetcher import FetchError, HttpPageFetcher, parse_retry_after


def test_parse_retry_after_seconds():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("") is None
    assert parse_retry_after("invalid-date") is None


@pytest.mark.asyncio
async def test_http_page_fetcher_success_error_and_on_page_rejection():
    url = "https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/"
    with respx.mock:
        respx.get(url).mock(
            side_effect=[
                httpx.Response(200, text="<html>ok</html>"),
                httpx.Response(429, headers={"Retry-After": "90"}, text="rate limited"),
                httpx.Response(403, text="forbidden"),
            ]
        )
        fetcher = HttpPageFetcher(timeout_seconds=5.0)
        try:
            page = await fetcher.fetch(url)
            assert page.status_code == 200
            assert page.html == "<html>ok</html>"

            with pytest.raises(FetchError) as exc_429:
                await fetcher.fetch(url)
            assert exc_429.value.status_code == 429
            assert exc_429.value.retry_after == 90.0

            with pytest.raises(FetchError) as exc_403:
                await fetcher.fetch(url)
            assert exc_403.value.status_code == 403

            async def _noop(page):
                return None

            with pytest.raises(FetchError, match="Playwright"):
                await fetcher.fetch(url, on_page=_noop)
        finally:
            await fetcher.close()


def test_cli_rejects_conflicting_flags():
    assert main(["--test-webhook", "--dry-run"]) == 2


def test_cli_test_webhook_e2e(tmp_path):
    webhook = "https://discord.com/api/webhooks/123456/tokenvalue"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DISCORD_WEBHOOK_URL={webhook}\nARGOS_POSTCODE=SW1A 1AA\n",
        encoding="utf-8",
    )

    with respx.mock:
        route = respx.post(webhook).mock(return_value=httpx.Response(204))
        code = main(["--env-file", str(env_file), "--test-webhook"])

    assert code == 0
    assert route.called
