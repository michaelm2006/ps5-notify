from __future__ import annotations

from abc import ABC, abstractmethod

from ps5_notify.fetcher import PageFetcher, PageResponse
from ps5_notify.models import CheckResult


class RetailerAdapter(ABC):
    """Base interface for pluggable retailer stock checkers."""

    key: str
    display_name: str
    default_url: str

    @abstractmethod
    def parse(self, response: PageResponse) -> CheckResult:
        """Parse an HTTP/Playwright page response into a normalized CheckResult."""

    async def check(self, fetcher: PageFetcher, url: str) -> CheckResult:
        """Fetch the target URL and parse its stock availability."""
        response = await fetcher.fetch(url)
        return self.parse(response)
