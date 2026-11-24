from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bs4 import BeautifulSoup, Tag

from ps5_notify.fetcher import FetchError, PageFetcher, PageResponse
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

_COOKIE_ACCEPT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    '[data-test="accept-cookies"]',
    'button:has-text("Accept all")',
)

_POSTCODE_INPUT_SELECTOR = '[data-test="fulfilment-search-stock-search-input"]'
_POSTCODE_BUTTON_SELECTOR = '[data-test="fulfilment-search-stock-search-button"]'
_RESULT_READY_SELECTOR = (
    '[data-testid="fulfilment-option-collection"], '
    '[data-testid="fulfilment-option-delivery"], '
    '[data-test="out-of-stock-message"], '
    '[data-test="fulfilment-email-panel"]'
)

_NEGATIVE_FULFILMENT_PHRASES = (
    "not in stock",
    "out of stock",
    "currently unavailable",
    "unavailable",
    "not available",
    "no longer available",
    "cannot deliver",
    "cannot collect",
    "no stock",
    "sold out",
)

_POSITIVE_FULFILMENT_PHRASES = (
    "in stock",
    "collect today",
    "collect tomorrow",
    "collect from",
    "collect in",
    "ready to collect",
    "available",
    "free delivery",
    "same day",
    "next day",
    "tomorrow",
    "deliver",
)


def _classify_fulfilment_message(text: str) -> StockStatus | None:
    normalized = normalize_whitespace(text).lower()
    if not normalized:
        return None
    if any(phrase in normalized for phrase in _NEGATIVE_FULFILMENT_PHRASES):
        return StockStatus.OUT_OF_STOCK
    if any(phrase in normalized for phrase in _POSITIVE_FULFILMENT_PHRASES):
        return StockStatus.IN_STOCK
    return None


def _extract_toggle_message(toggle: Tag, fallback_label: str) -> str:
    msg_el = toggle.select_one('[data-testid="message"]')
    if msg_el:
        return normalize_whitespace(msg_el.get_text(" ", strip=True))
    full_text = normalize_whitespace(toggle.get_text(" ", strip=True))
    cleaned = re.sub(rf"^{re.escape(fallback_label)}\b", "", full_text, flags=re.I).strip()
    return cleaned


class ArgosAdapter(RetailerAdapter):
    """Stock adapter for Argos UK using postcode-checked Delivery & Collection availability."""

    key = "argos"
    display_name = "Argos UK"
    default_url = "https://www.argos.co.uk/product/7797426"
    default_fetcher = "playwright"

    async def check(
        self,
        fetcher: PageFetcher,
        url: str,
        *,
        options: Mapping[str, str] | None = None,
    ) -> CheckResult:
        postcode = (options or {}).get("postcode", "").strip()
        if not postcode:
            raise FetchError("ARGOS_POSTCODE must be configured to check Argos UK stock.")

        async def _submit_postcode(page: Any) -> None:
            # Dismiss cookie banner if present without blocking.
            for selector in _COOKIE_ACCEPT_SELECTORS:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click(timeout=2000)
                        break
                except Exception:
                    continue

            # Wait for either the postcode form or an already-rendered availability state.
            combined_ready = f"{_POSTCODE_INPUT_SELECTOR}, {_RESULT_READY_SELECTOR}"
            await page.locator(combined_ready).first.wait_for(state="visible", timeout=10000)

            postcode_input = page.locator(_POSTCODE_INPUT_SELECTOR).first
            if await postcode_input.is_visible():
                await postcode_input.fill(postcode)
                await page.locator(_POSTCODE_BUTTON_SELECTOR).first.click()
                await page.locator(_RESULT_READY_SELECTOR).first.wait_for(
                    state="visible",
                    timeout=10000,
                )
                # Allow toggle status messages (Collection / Delivery) to finish populating.
                try:
                    await page.locator('[data-testid="message"]').first.wait_for(
                        state="visible",
                        timeout=5000,
                    )
                except Exception:
                    pass

        response = await fetcher.fetch(url, on_page=_submit_postcode)
        return self.parse(response, options=options)

    def parse(
        self,
        response: PageResponse,
        *,
        options: Mapping[str, str] | None = None,
    ) -> CheckResult:
        soup = BeautifulSoup(response.html, "html.parser")
        products = extract_json_ld_products(soup)
        product_ld = products[0] if products else None

        postcode = (options or {}).get("postcode", "").strip()
        fulfilment_mode = ((options or {}).get("fulfilment_mode") or "either").strip().lower()

        title = self._extract_title(product_ld, soup)
        price = self._extract_price(product_ld, soup)
        image_url = extract_first_image(product_ld, soup, response.final_url)
        status, signal = self._determine_status(
            product_ld,
            soup,
            postcode=postcode,
            fulfilment_mode=fulfilment_mode,
        )

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
        *,
        postcode: str,
        fulfilment_mode: str,
    ) -> tuple[StockStatus, str]:
        # Keep raw user postcode out of signal strings (which can appear in Discord footers).
        _ = postcode

        # 1. Explicit Argos out-of-stock / unavailable DOM banners.
        for selector in _OOS_SELECTORS:
            if soup.select_one(selector) is not None:
                return StockStatus.OUT_OF_STOCK, f"dom:{selector}"

        # 2. Postcode-checked Collection & Delivery fulfilment toggles (Argos fulfilment-box-v2).
        collection_toggle = soup.select_one('[data-testid="fulfilment-option-collection"]')
        delivery_toggle = soup.select_one('[data-testid="fulfilment-option-delivery"]')

        if collection_toggle is not None or delivery_toggle is not None:
            col_msg = _extract_toggle_message(collection_toggle, "Collection") if collection_toggle else ""
            del_msg = _extract_toggle_message(delivery_toggle, "Delivery") if delivery_toggle else ""
            col_status = _classify_fulfilment_message(col_msg) if collection_toggle else None
            del_status = _classify_fulfilment_message(del_msg) if delivery_toggle else None

            if fulfilment_mode == "delivery":
                if del_status == StockStatus.IN_STOCK:
                    return StockStatus.IN_STOCK, f"argos:delivery({del_msg})"
                if del_status == StockStatus.OUT_OF_STOCK:
                    return StockStatus.OUT_OF_STOCK, f"argos:delivery-oos({del_msg})"
                return StockStatus.UNKNOWN, f"argos:delivery-unrecognized({del_msg})"

            if fulfilment_mode == "collection":
                if col_status == StockStatus.IN_STOCK:
                    return StockStatus.IN_STOCK, f"argos:collection({col_msg})"
                if col_status == StockStatus.OUT_OF_STOCK:
                    return StockStatus.OUT_OF_STOCK, f"argos:collection-oos({col_msg})"
                return StockStatus.UNKNOWN, f"argos:collection-unrecognized({col_msg})"

            # Default mode: "either" (Delivery OR Collection).
            if del_status == StockStatus.IN_STOCK and col_status == StockStatus.IN_STOCK:
                return (
                    StockStatus.IN_STOCK,
                    f"argos:delivery({del_msg})+collection({col_msg})",
                )
            if del_status == StockStatus.IN_STOCK:
                return StockStatus.IN_STOCK, f"argos:delivery({del_msg})"
            if col_status == StockStatus.IN_STOCK:
                return StockStatus.IN_STOCK, f"argos:collection({col_msg})"

            present_statuses = [
                status
                for toggle, status in ((delivery_toggle, del_status), (collection_toggle, col_status))
                if toggle is not None
            ]
            if present_statuses and all(s == StockStatus.OUT_OF_STOCK for s in present_statuses):
                return (
                    StockStatus.OUT_OF_STOCK,
                    f"argos:oos(delivery={del_msg or 'n/a'}, collection={col_msg or 'n/a'})",
                )

            return (
                StockStatus.UNKNOWN,
                f"argos:unrecognized(delivery={del_msg or 'n/a'}, collection={col_msg or 'n/a'})",
            )

        # 3. Delivery out-of-stock email notification panel or unavailable fulfilment box copy.
        if soup.select_one('[data-test="fulfilment-email-panel"]') is not None:
            return StockStatus.OUT_OF_STOCK, "dom:fulfilment-email-panel"

        fulfilment_box = soup.select_one('[data-test="fulfilment-box-container"]')
        if fulfilment_box is not None:
            box_text = normalize_whitespace(fulfilment_box.get_text(" ", strip=True)).lower()
            for phrase in ("out of stock for delivery", "currently unavailable", "no longer available", "out of stock"):
                if phrase in box_text:
                    return StockStatus.OUT_OF_STOCK, f"dom:fulfilment-box({phrase})"

        # 4. Unsubmitted postcode form — do NOT treat Argos's always-enabled Add to trolley button as IN_STOCK.
        if (
            soup.select_one('[data-test="fulfilment-search-stock-form"]') is not None
            or soup.select_one(_POSTCODE_INPUT_SELECTOR) is not None
        ):
            return StockStatus.UNKNOWN, "argos:postcode-not-checked"

        # 5. Schema.org Product JSON-LD offers.availability (if ever provided).
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

        return StockStatus.UNKNOWN, "no-stock-signal"
