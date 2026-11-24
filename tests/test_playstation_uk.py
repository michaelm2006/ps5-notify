from __future__ import annotations

from ps5_notify.fetcher import PageResponse
from ps5_notify.models import StockStatus
from ps5_notify.retailers.playstation_uk import PlayStationUKAdapter

PS_URL = "https://www.playstation.com/en-gb/ps5/ps5-pro/buy-now/"


def _make_response(html: str) -> PageResponse:
    return PageResponse(
        url=PS_URL,
        final_url=PS_URL,
        status_code=200,
        html=html,
        headers={"content-type": "text/html"},
    )


def test_playstation_out_of_stock_matches_live_structure():
    html = """
    <html>
      <head>
        <title>PS5 Pro 2TB PDP</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "PlayStation®5 Pro Console - 2 TB",
          "sku": "PSD001-PSDE01942_39-PS5PROCHASASIRSE-E000K5EGO",
          "image": "https://media.direct.playstation.com/is/image/psd/ps5-pro-hero",
          "offers": {
            "@type": "Offer",
            "price": "789.99",
            "priceCurrency": "GBP",
            "availability": "https://schema.org/OutOfStock"
          }
        }
        </script>
      </head>
      <body>
        <div data-qa="mfeCtaMain#offer0#finalPrice">Not available for purchase</div>
        <a id="unavailableLearnMore" href="#">Learn More</a>
        <div data-qa="mfeCtaMain#lease"><a href="#">Lease from £20.95/mo</a></div>
        <p>Currently Out of stock, check back soon.</p>
      </body>
    </html>
    """
    result = PlayStationUKAdapter().parse(_make_response(html))
    assert result.status == StockStatus.OUT_OF_STOCK
    assert result.title == "PlayStation®5 Pro Console - 2 TB"
    assert result.price == "£789.99"
    assert result.image_url == "https://media.direct.playstation.com/is/image/psd/ps5-pro-hero"
    assert result.retailer_key == "playstation_uk"


def test_playstation_in_stock_via_json_ld_ssr():
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "PlayStation 5 Pro Console - 2 TB",
          "image": ["https://media.direct.playstation.com/ps5-pro.jpg"],
          "offers": {
            "@type": "Offer",
            "price": 789.99,
            "priceCurrency": "GBP",
            "availability": "https://schema.org/InStock"
          }
        }
        </script>
      </head>
      <body>
        <div id="mfe-mount"></div>
      </body>
    </html>
    """
    result = PlayStationUKAdapter().parse(_make_response(html))
    assert result.status == StockStatus.IN_STOCK
    assert result.price == "£789.99"
    assert result.signal == "jsonld:InStock"


def test_playstation_in_stock_via_rendered_add_to_cart_cta():
    html = """
    <html>
      <head>
        <meta property="og:title" content="PlayStation 5 Pro Console - 2 TB" />
        <meta property="og:image" content="https://media.direct.playstation.com/hero.png" />
      </head>
      <body>
        <div data-qa="mfeCtaMain#offer0#finalPrice">£789.99</div>
        <div data-qa="mfeCtaMain#cta">
          <button type="button">Add to Cart</button>
        </div>
      </body>
    </html>
    """
    result = PlayStationUKAdapter().parse(_make_response(html))
    assert result.status == StockStatus.IN_STOCK
    assert result.price == "£789.99"
    assert result.image_url == "https://media.direct.playstation.com/hero.png"
    assert "add to cart" in result.signal


def test_playstation_unrecognized_or_blocked_html_fails_closed():
    html = "<html><head><title>Access Denied</title></head><body>Reference #18.abc</body></html>"
    result = PlayStationUKAdapter().parse(_make_response(html))
    assert result.status == StockStatus.UNKNOWN
    assert result.signal == "no-stock-signal"
