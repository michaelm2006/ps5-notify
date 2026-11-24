from __future__ import annotations

import re
from collections.abc import Mapping

from bs4 import BeautifulSoup, Tag

from ps5_notify.fetcher import PageResponse
from ps5_notify.html_utils import (
    extract_first_image,
    extract_json_ld_products,
    format_price,
    iter_offers,
    normalize_whitespace,
    parse_schema_availability,
)
from ps5_notify.models import CheckResult, StockStatus, utc_now
from ps5_notify.retailers.base import RetailerAdapter

_UNAVAILABLE_PHRASES = (
    "not available for purchase",
    "currently out of stock",
    "out of stock",
    "currently unavailable",
    "sold out",
)

_PURCHASE_CTA_PHRASES = (
    "add to cart",
    "add to bag",
    "buy now",
)

_EXCLUDED_CTA_PHRASES = (
    "lease",
    "trade-in",
    "trade in",
    "learn more",
)


def _is_disabled(element: Tag) -> bool:
    if element.has_attr("disabled"):
        return True
    aria_disabled = str(element.get("aria-disabled", "")).strip().lower()
    return aria_disabled == "true"


class PlayStationUKAdapter(RetailerAdapter):
    """Stock adapter for PlayStation Direct UK product pages hosted on playstation.com."""

    key = "playstation_uk"
    display_name = "PlayStation UK"
    default_url = "https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/"
    default_fetcher = "http"

    def parse(
        self,
        response: PageResponse,
        *,
        options: Mapping[str, str] | None = None,
    ) -> CheckResult:
        soup = BeautifulSoup(response.html, "html.parser")
        products = extract_json_ld_products(soup)
        product_ld = products[0] if products else None

        title = self._extract_title(product_ld, soup)
        price = self._extract_price(product_ld, soup)
        image_url = extract_first_image(product_ld, soup, response.final_url)
        status, signal = self._determine_status(product_ld, soup)

        return CheckResult(
            retailer_key=self.key,
            retailer_name=self.display_name,
            url=response.final_url or response.url,
            status=status,
            title=title,
            price=price,
            image_url=image_url,
            signal=signal,
            checked_at=utc_now(),
        )

    def _extract_title(self, product_ld: dict | None, soup: BeautifulSoup) -> str:
        if product_ld and isinstance(product_ld.get("name"), str) and product_ld["name"].strip():
            return normalize_whitespace(product_ld["name"])

        h1 = soup.find("h1")
        if h1:
            text = normalize_whitespace(h1.get_text(" ", strip=True))
            if text:
                return text

        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            text = normalize_whitespace(str(og_title["content"]))
            if text:
                return text

        if soup.title and soup.title.string:
            text = normalize_whitespace(soup.title.string)
            if text:
                return text

        return "PlayStation 5 Pro Console"

    def _extract_price(self, product_ld: dict | None, soup: BeautifulSoup) -> str | None:
        if product_ld:
            for offer in iter_offers(product_ld):
                formatted = format_price(offer.get("price"), offer.get("priceCurrency") or "GBP")
                if formatted:
                    return formatted

        cta_price = soup.select_one('[data-qa="mfeCtaMain#offer0#finalPrice"]')
        if cta_price:
            text = normalize_whitespace(cta_price.get_text(" ", strip=True))
            if re.search(r"[£€$]\s*\d", text):
                return text

        return None

    def _determine_status(
        self,
        product_ld: dict | None,
        soup: BeautifulSoup,
    ) -> tuple[StockStatus, str]:
        # 1. Explicit unavailable learn-more link rendered in PlayStation Direct buy box.
        if soup.select_one("#unavailableLearnMore") is not None:
            return StockStatus.OUT_OF_STOCK, "dom:#unavailableLearnMore"

        # 2. Live PlayStation Store / Direct buy-box CTA elements.
        cta_elements = soup.select('[data-qa*="mfeCtaMain"]')
        if cta_elements:
            for container in cta_elements:
                actionables = [container] if container.name in {"button", "a"} else container.select("button, a")
                for actionable in actionables:
                    if _is_disabled(actionable):
                        continue
                    label = normalize_whitespace(actionable.get_text(" ", strip=True)).lower()
                    if any(excl in label for excl in _EXCLUDED_CTA_PHRASES):
                        continue
                    if any(phrase in label for phrase in _PURCHASE_CTA_PHRASES):
                        return StockStatus.IN_STOCK, f"dom:mfeCtaMain({label})"

            cta_text = " ".join(
                normalize_whitespace(el.get_text(" ", strip=True)).lower() for el in cta_elements
            )
            if any(phrase in cta_text for phrase in _UNAVAILABLE_PHRASES):
                return StockStatus.OUT_OF_STOCK, "dom:mfeCtaMain(unavailable)"

        # 3. Embedded Schema.org Product JSON-LD offers.availability.
        if product_ld:
            parsed_statuses: list[StockStatus] = []
            for offer in iter_offers(product_ld):
                parsed = parse_schema_availability(offer.get("availability"))
                if parsed is not None:
                    parsed_statuses.append(parsed)
            if StockStatus.IN_STOCK in parsed_statuses:
                return StockStatus.IN_STOCK, "jsonld:InStock"
            if StockStatus.OUT_OF_STOCK in parsed_statuses:
                return StockStatus.OUT_OF_STOCK, "jsonld:OutOfStock"

        # 4. Fallback: explicit out-of-stock copy on a recognized PlayStation product page.
        body_text = normalize_whitespace(soup.get_text(" ", strip=True)).lower()
        if product_ld is not None or "playstation" in body_text:
            for phrase in ("currently out of stock", "not available for purchase"):
                if phrase in body_text:
                    return StockStatus.OUT_OF_STOCK, f"text:{phrase}"

        return StockStatus.UNKNOWN, "no-stock-signal"
