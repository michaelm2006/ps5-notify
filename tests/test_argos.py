from __future__ import annotations

from ps5_notify.fetcher import PageResponse
from ps5_notify.models import StockStatus
from ps5_notify.retailers.argos import ArgosAdapter

ARGOS_URL = "https://www.argos.co.uk/product/7797426"


def _make_response(html: str) -> PageResponse:
    return PageResponse(
        url=ARGOS_URL,
        final_url=ARGOS_URL,
        status_code=200,
        html=html,
        headers={"content-type": "text/html"},
    )


def test_argos_in_stock_matches_live_structure():
    html = """
    <html>
      <head>
        <meta property="og:image" content="//media.4rgos.it/s/Argos/7797426_R_SET?$Main768$" />
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@graph": [
            {
              "@type": "Product",
              "name": "PlayStation 5 Pro Console - 2 TB Digital Edition",
              "sku": "7797426",
              "image": [
                "//media.4rgos.it/i/Argos/7797426_R_Z001A",
                "//media.4rgos.it/i/Argos/7797426_R_Z002A"
              ],
              "offers": {
                "@type": "Offer",
                "price": "789.99",
                "priceCurrency": "GBP",
                "url": "https://www.argos.co.uk/product/7797426"
              }
            }
          ]
        }
        </script>
      </head>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console - 2 TB Digital Edition</h1>
        <li data-test="product-price-primary"><h2>£789.99</h2></li>
        <div data-test="fulfilment-box-container">
          <div data-test="check-store-box">
            <h2 data-test="fulfilment-heading">Check stock</h2>
          </div>
          <div data-test="add-trolley-button-wrapper">
            <button data-test="add-to-trolley-button-button" type="button">Add to trolley</button>
          </div>
        </div>
      </body>
    </html>
    """
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.IN_STOCK
    assert result.title == "PlayStation 5 Pro Console - 2 TB Digital Edition"
    assert result.price == "£789.99"
    assert result.image_url == "https://media.4rgos.it/i/Argos/7797426_R_Z001A"
    assert result.signal == "dom:add-to-trolley-enabled"


def test_argos_bundle_button_does_not_cause_false_in_stock():
    html = """
    <html>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console - 2 TB Digital Edition</h1>
        <div data-test="fulfilment-box-container">
          <p>Sorry, this item is currently unavailable.</p>
        </div>
        <section class="bundle-cross-sell">
          <button data-test="add-to-trolley-button-button" type="button">Add all items to trolley</button>
        </section>
      </body>
    </html>
    """
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.OUT_OF_STOCK


def test_argos_disabled_add_to_trolley_is_out_of_stock():
    html = """
    <html>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console - 2 TB Digital Edition</h1>
        <div data-test="add-trolley-button-wrapper">
          <button data-test="add-to-trolley-button-button" disabled>Add to trolley</button>
        </div>
      </body>
    </html>
    """
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.OUT_OF_STOCK
    assert result.signal == "dom:add-to-trolley-disabled"


def test_argos_explicit_oos_banner_overrides():
    html = """
    <html>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console</h1>
        <div data-test="out-of-stock-message">Out of stock</div>
      </body>
    </html>
    """
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.OUT_OF_STOCK


def test_argos_json_ld_availability_fallback():
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "PlayStation 5 Pro Console",
          "offers": {
            "@type": "Offer",
            "price": "789.99",
            "priceCurrency": "GBP",
            "availability": "https://schema.org/OutOfStock"
          }
        }
        </script>
      </head>
      <body></body>
    </html>
    """
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.OUT_OF_STOCK
    assert result.signal == "jsonld:OutOfStock"


def test_argos_unknown_html_fails_closed():
    html = "<html><head><title>Access Denied</title></head><body>Blocked</body></html>"
    result = ArgosAdapter().parse(_make_response(html))
    assert result.status == StockStatus.UNKNOWN
