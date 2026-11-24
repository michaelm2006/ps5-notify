from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from ps5_notify.fetcher import PageFetcher, PageResponse
from ps5_notify.models import CheckResult


class RetailerAdapter(ABC):
    """Base interface for pluggable retailer stock checkers."""

    key: str
    display_name: str
    default_url: str
    default_fetcher: str = "http"

    @abstractmethod
    def parse(
        self,
        response: PageResponse,
        *,
        options: Mapping[str, str] | None = None,
    ) -> CheckResult:
        """Parse an HTTP/Playwright page response into a normalized CheckResult."""

    async def check(
        self,
        fetcher: PageFetcher,
        url: str,
        *,
        options: Mapping[str, str] | None = None,
    ) -> CheckResult:
        """Fetch the target URL and parse its stock availability."""
        response = await fetcher.fetch(url)
        return self.parse(response, options=options)
