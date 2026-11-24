from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PageAction = Callable[[Any], Awaitable[None]]


class FetchError(Exception):
    """Raised when fetching a retailer product page fails or is rate-limited/blocked."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class PageResponse:
    url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str]


class PageFetcher(Protocol):
    async def fetch(self, url: str, *, on_page: PageAction | None = None) -> PageResponse: ...

    async def close(self) -> None: ...


def parse_retry_after(raw_value: str | None) -> float | None:
    """Parse HTTP Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not raw_value:
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    try:
        seconds = float(cleaned)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(cleaned)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


class HttpPageFetcher:
    """Lightweight HTTP page fetcher powered by httpx."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-GB,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )

    async def fetch(self, url: str, *, on_page: PageAction | None = None) -> PageResponse:
        if on_page is not None:
            raise FetchError(
                f"Interactive browser step requested for {url}, but HTTP fetcher is active. "
                "Switch this retailer to Playwright (e.g. ARGOS_FETCHER=playwright)."
            )

        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise FetchError(f"Request timed out for {url}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP transport error for {url}: {exc}") from exc

        retry_after = parse_retry_after(response.headers.get("retry-after"))
        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} ({response.reason_phrase}) from {url}",
                status_code=response.status_code,
                retry_after=retry_after,
            )

        return PageResponse(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            html=response.text,
            headers={k.lower(): v for k, v in response.headers.items()},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class PlaywrightPageFetcher:
    """Headless-browser fetcher for retailers that require JS rendering or interactive checks.

    Requires optional dependency: `pip install '.[playwright]' && playwright install chromium`.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._timeout_ms = int(timeout_seconds * 1000)
        self._user_agent = user_agent
        self._playwright = None
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise FetchError(
                "Playwright backend requested, but playwright is not installed. "
                "Install with: pip install '.[playwright]' && playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def fetch(self, url: str, *, on_page: PageAction | None = None) -> PageResponse:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=self._user_agent,
            locale="en-GB",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
            if response is None:
                raise FetchError(f"Playwright received no HTTP response for {url}")

            status = response.status
            headers = {k.lower(): v for k, v in response.headers.items()}
            retry_after = parse_retry_after(headers.get("retry-after"))
            if status >= 400:
                raise FetchError(
                    f"HTTP {status} from {url} (Playwright)",
                    status_code=status,
                    retry_after=retry_after,
                )

            if on_page is not None:
                await on_page(page)
            else:
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(self._timeout_ms, 5000))
                except Exception:
                    pass

            html = await page.content()
            return PageResponse(
                url=url,
                final_url=page.url,
                status_code=status,
                html=html,
                headers=headers,
            )
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"Playwright navigation error for {url}: {exc}") from exc
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def create_fetcher(backend: str, *, timeout_seconds: float) -> PageFetcher:
    normalized = backend.strip().lower()
    if normalized == "http":
        return HttpPageFetcher(timeout_seconds=timeout_seconds)
    if normalized == "playwright":
        return PlaywrightPageFetcher(timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported fetcher backend: {backend!r}")
