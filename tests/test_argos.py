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


def _argos_pdp_html(collection_msg: str, delivery_msg: str, *, include_add_button: bool = True) -> str:
    add_button = (
        """
        <div data-test="add-trolley-button-wrapper">
          <button data-test="add-to-trolley-button-button" type="button">Add to trolley</button>
        </div>
        """
        if include_add_button
        else ""
    )
    return f"""
    <html>
      <head>
        <meta property="og:image" content="//media.4rgos.it/s/Argos/7797426_R_SET?$Main768$" />
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@graph": [
            {{
              "@type": "Product",
              "name": "PlayStation 5 Pro Console - 2 TB Digital Edition",
              "sku": "7797426",
              "image": [
                "//media.4rgos.it/i/Argos/7797426_R_Z001A",
                "//media.4rgos.it/i/Argos/7797426_R_Z002A"
              ],
              "offers": {{
                "@type": "Offer",
                "price": "789.99",
                "priceCurrency": "GBP",
                "url": "https://www.argos.co.uk/product/7797426"
              }}
            }}
          ]
        }}
        </script>
      </head>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console - 2 TB Digital Edition</h1>
        <li data-test="product-price-primary"><h2>£789.99</h2></li>
        <div data-test="fulfilment-box-container">
          <div class="fulfilment-box-v2">
            <button data-testid="fulfilment-option-collection" value="collection">
              <span>Collection</span>
              <span data-testid="message"><span>{collection_msg}</span></span>
            </button>
            <button data-testid="fulfilment-option-delivery" value="delivery">
              <span>Delivery</span>
              <span data-testid="message"><span>{delivery_msg}</span></span>
            </button>
          </div>
          {add_button}
        </div>
      </body>
    </html>
    """


def test_argos_both_oos_ignores_enabled_add_to_trolley_and_hides_postcode():
    html = _argos_pdp_html("Not in stock", "Not in stock", include_add_button=True)
    result = ArgosAdapter().parse(
        _make_response(html),
        options={"postcode": "SW1A 1AA", "fulfilment_mode": "either"},
    )
    assert result.status == StockStatus.OUT_OF_STOCK
    assert result.title == "PlayStation 5 Pro Console - 2 TB Digital Edition"
    assert result.price == "£789.99"
    assert result.image_url == "https://media.4rgos.it/i/Argos/7797426_R_Z001A"
    assert "SW1A" not in result.signal


def test_argos_delivery_in_stock_respects_fulfilment_mode():
    html = _argos_pdp_html("Not in stock", "In stock")
    adapter = ArgosAdapter()

    r_either = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "either"})
    assert r_either.status == StockStatus.IN_STOCK
    assert "delivery" in r_either.signal
    assert "SW1A" not in r_either.signal

    r_delivery = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "delivery"})
    assert r_delivery.status == StockStatus.IN_STOCK

    r_collection = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "collection"})
    assert r_collection.status == StockStatus.OUT_OF_STOCK


def test_argos_collection_in_stock_respects_fulfilment_mode():
    html = _argos_pdp_html("Collect today", "Not in stock")
    adapter = ArgosAdapter()

    r_either = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "either"})
    assert r_either.status == StockStatus.IN_STOCK
    assert "collection" in r_either.signal

    r_collection = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "collection"})
    assert r_collection.status == StockStatus.IN_STOCK

    r_delivery = adapter.parse(_make_response(html), options={"postcode": "SW1A 1AA", "fulfilment_mode": "delivery"})
    assert r_delivery.status == StockStatus.OUT_OF_STOCK


def test_argos_unsubmitted_postcode_form_never_false_positives_on_add_to_trolley():
    html = """
    <html>
      <body>
        <h1 data-test="product-name-main">PlayStation 5 Pro Console - 2 TB Digital Edition</h1>
        <div data-test="fulfilment-box-container">
          <form data-test="fulfilment-search-stock-form">
            <input data-test="fulfilment-search-stock-search-input" placeholder="Postcode or town" />
            <button data-test="fulfilment-search-stock-search-button" type="submit">Check</button>
          </form>
          <div data-test="add-trolley-button-wrapper">
            <button data-test="add-to-trolley-button-button" type="button">Add to trolley</button>
          </div>
        </div>
      </body>
    </html>
    """
    result = ArgosAdapter().parse(
        _make_response(html),
        options={"postcode": "SW1A 1AA", "fulfilment_mode": "either"},
    )
    assert result.status == StockStatus.UNKNOWN
    assert result.signal == "argos:postcode-not-checked"


def test_argos_explicit_oos_banner_and_email_panel():
    banner_html = """
    <html><body>
      <h1 data-test="product-name-main">PlayStation 5 Pro Console</h1>
      <div data-test="out-of-stock-message">Out of stock</div>
    </body></html>
    """
    assert ArgosAdapter().parse(_make_response(banner_html)).status == StockStatus.OUT_OF_STOCK

    email_panel_html = """
    <html><body>
      <div data-test="fulfilment-box-container">
        <div data-test="fulfilment-email-panel">Out of stock for delivery</div>
      </div>
    </body></html>
    """
    assert ArgosAdapter().parse(_make_response(email_panel_html)).status == StockStatus.OUT_OF_STOCK


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
