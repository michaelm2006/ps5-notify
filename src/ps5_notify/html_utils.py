from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ps5_notify.models import StockStatus

CURRENCY_SYMBOLS: dict[str, str] = {
    "GBP": "£",
    "EUR": "€",
    "USD": "$",
}

IN_STOCK_SCHEMA_TOKENS = frozenset(
    {
        "instock",
        "onlineonly",
        "limitedavailability",
        "preorder",
    }
)

OUT_OF_STOCK_SCHEMA_TOKENS = frozenset(
    {
        "outofstock",
        "soldout",
        "discontinued",
        "backorder",
    }
)


def _is_product_type(type_value: Any) -> bool:
    if isinstance(type_value, str):
        return type_value.lower() == "product"
    if isinstance(type_value, list):
        return any(isinstance(item, str) and item.lower() == "product" for item in type_value)
    return False


def _collect_products(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_products(item, out)
        return
    if not isinstance(node, dict):
        return
    if _is_product_type(node.get("@type")):
        out.append(node)
    graph = node.get("@graph")
    if graph is not None:
        _collect_products(graph, out)


def extract_json_ld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract all Schema.org Product dicts from <script type='application/ld+json'> tags."""
    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _collect_products(payload, products)
    return products


def iter_offers(product_ld: dict[str, Any]) -> list[dict[str, Any]]:
    offers = product_ld.get("offers")
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [offer for offer in offers if isinstance(offer, dict)]
    return []


def parse_schema_availability(value: Any) -> StockStatus | None:
    """Parse a Schema.org availability URL or short token into StockStatus."""
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip().rsplit("/", maxsplit=1)[-1].lower()
    if token in IN_STOCK_SCHEMA_TOKENS:
        return StockStatus.IN_STOCK
    if token in OUT_OF_STOCK_SCHEMA_TOKENS:
        return StockStatus.OUT_OF_STOCK
    return None


def normalize_url(raw: str | None, base_url: str) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    return urljoin(base_url, cleaned)


def format_price(price: Any, currency: str | None = "GBP") -> str | None:
    if price is None:
        return None
    if isinstance(price, (int, float)):
        symbol = CURRENCY_SYMBOLS.get((currency or "GBP").upper(), f"{currency} " if currency else "")
        return f"{symbol}{price:.2f}"
    text = str(price).strip()
    if not text:
        return None
    if any(sym in text for sym in ("£", "€", "$")):
        return re.sub(r"\s+", " ", text)
    try:
        numeric = float(text.replace(",", ""))
    except ValueError:
        return text
    symbol = CURRENCY_SYMBOLS.get((currency or "GBP").upper(), f"{currency} " if currency else "")
    return f"{symbol}{numeric:.2f}"


def extract_first_image(
    product_ld: dict[str, Any] | None,
    soup: BeautifulSoup,
    base_url: str,
) -> str | None:
    if product_ld:
        image_field = product_ld.get("image")
        candidates: list[Any]
        if isinstance(image_field, list):
            candidates = image_field
        elif image_field is not None:
            candidates = [image_field]
        else:
            candidates = []

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return normalize_url(candidate, base_url)
            if isinstance(candidate, dict):
                nested = candidate.get("url") or candidate.get("contentUrl")
                if isinstance(nested, str) and nested.strip():
                    return normalize_url(nested, base_url)

    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image and og_image.get("content"):
        return normalize_url(str(og_image["content"]), base_url)

    return None


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
