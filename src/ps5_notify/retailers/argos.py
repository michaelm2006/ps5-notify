from __future__ import annotations

import re

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

_OOS_SELECTORS = (
    '[data-test="out-of-stock-message"]',
    '[data-test="pdp-out-of-stock"]',
    '[data-test="unavailable-message"]',
    '[data-test="fulfilment-unavailable"]',
)

_PRIMARY_BUY_CONTAINER_SELECTORS = (
    '[data-test="fulfilment-box-container"]',
    '[data-test="add-trolley-button-wrapper"]',
    '[data-test="pdp-right-column"]',
)

_UNAVAILABLE_PHRASES = (
    "currently unavailable",
    "out of stock",
    "no longer available",
    "sold out",
    "sorry, this item is currently unavailable",
    "sorry, this product is currently unavailable",
)


def _is_disabled(element: Tag) -> bool:
    if element.has_attr("disabled"):
        return True
    aria_disabled = str(element.get("aria-disabled", "")).strip().lower()
    return aria_disabled == "true"


class ArgosAdapter(RetailerAdapter):
    """Stock adapter for Argos UK general product page availability (no postcode check)."""

    key = "argos"
    display_name = "Argos UK"
    default_url = "https://www.argos.co.uk/product/7797426"

    def parse(self, response: PageResponse) -> CheckResult:
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
        for selector in ('[data-test="product-name-main"]', '[data-test="product-title"]'):
            el = soup.select_one(selector)
            if el:
                text = normalize_whitespace(el.get_text(" ", strip=True))
                if text:
                    return text

        if product_ld and isinstance(product_ld.get("name"), str) and product_ld["name"].strip():
            return normalize_whitespace(product_ld["name"])

        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            text = normalize_whitespace(str(og_title["content"]))
            if text:
                return text

        h1 = soup.find("h1")
        if h1:
            text = normalize_whitespace(h1.get_text(" ", strip=True))
            if text:
                return text

        return "PlayStation 5 Pro Console (Argos)"

    def _extract_price(self, product_ld: dict | None, soup: BeautifulSoup) -> str | None:
        if product_ld:
            for offer in iter_offers(product_ld):
                formatted = format_price(offer.get("price"), offer.get("priceCurrency") or "GBP")
                if formatted:
                    return formatted

        price_el = soup.select_one('[data-test="product-price-primary"]')
        if price_el:
            text = normalize_whitespace(price_el.get_text(" ", strip=True))
            match = re.search(r"[£€$]\s*\d[\d,]*(?:\.\d{2})?", text)
            if match:
                return match.group(0).replace(" ", "")

        return None

    def _determine_status(
        self,
        product_ld: dict | None,
        soup: BeautifulSoup,
    ) -> tuple[StockStatus, str]:
        # 1. Explicit Argos out-of-stock / unavailable DOM banners.
        for selector in _OOS_SELECTORS:
            if soup.select_one(selector) is not None:
                return StockStatus.OUT_OF_STOCK, f"dom:{selector}"

        # 2. Out-of-stock text inside primary buy / fulfilment containers.
        for selector in _PRIMARY_BUY_CONTAINER_SELECTORS:
            container = soup.select_one(selector)
            if not container:
                continue
            text = normalize_whitespace(container.get_text(" ", strip=True)).lower()
            for phrase in _UNAVAILABLE_PHRASES:
                if phrase in text:
                    return StockStatus.OUT_OF_STOCK, f"dom:{selector}({phrase})"

        # 3. Scoped primary Add to Trolley button inside add-trolley-button-wrapper.
        # Scoping prevents false positives from cross-sell bundle "Add all items to trolley" buttons.
        wrapper = soup.select_one('[data-test="add-trolley-button-wrapper"]')
        if wrapper is not None:
            button = wrapper.select_one('[data-test="add-to-trolley-button-button"]')
            if button is None:
                for candidate in wrapper.select("button"):
                    label = normalize_whitespace(candidate.get_text(" ", strip=True)).lower()
                    if "add to trolley" in label and "all items" not in label:
                        button = candidate
                        break

            if button is not None:
                label = normalize_whitespace(button.get_text(" ", strip=True)).lower()
                if "all items" not in label:
                    if _is_disabled(button):
                        return StockStatus.OUT_OF_STOCK, "dom:add-to-trolley-disabled"
                    return StockStatus.IN_STOCK, "dom:add-to-trolley-enabled"

        # 4. Schema.org Product JSON-LD offers.availability (when present).
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

        # 5. Fallback: recognized Argos product page with explicit unavailable notice.
        has_pdp_marker = (
            product_ld is not None
            or soup.select_one('[data-test="product-name-main"]') is not None
            or soup.select_one('[data-test="fulfilment-box-container"]') is not None
        )
        if has_pdp_marker:
            body_text = normalize_whitespace(soup.get_text(" ", strip=True)).lower()
            for phrase in _UNAVAILABLE_PHRASES:
                if phrase in body_text:
                    return StockStatus.OUT_OF_STOCK, f"text:{phrase}"

        return StockStatus.UNKNOWN, "no-stock-signal"
