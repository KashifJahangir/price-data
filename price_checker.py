"""
Unified price & availability checker for products.json
--------------------------------------------------------------------------
Reads products.json, visits every store URL, and updates both price and
availability.

CHANGELOG (this version):
- FIX: stale-page bug. Selenium page-load timeouts used to fall through and
  read driver.page_source from the PREVIOUS product's page, saving that
  product's price under the wrong entry. Now:
    * the browser is sent to about:blank before every product,
    * a timeout means "skip this product" (no price is saved),
    * the browser's current URL must match the product URL,
    * the JSON-LD Product on the page must match the product URL.
- FIX: Junaid Tech / czone (webx) use JSON-LD only. No CSS-selector or regex
  guessing, so a failed read leaves the old price in place.
- NEW: Zah Computers now has its own reader (it is a ShopinoErp site, not
  WooCommerce). It reads the URL-matched JSON-LD from a plain request, falls
  back to headless Chrome only if blocked, and skips the rest of the site
  after 3 blocks in a row so a Cloudflare block can't stall the whole run.
- NEW: a price change is only saved after a second fetch gives the same
  value (--no-confirm to disable).
- NEW: default --max-change-pct lowered from 50 to 20.
- NEW: availability is only updated when the price was read successfully.
- When a price changes, the old price is saved in "previousprice" and the new
  price in "price"; "updatedAt" (unix seconds) records when. If the price did
  not change, previousprice / updatedAt are left untouched.

Install deps:
    pip install requests beautifulsoup4 selenium webdriver-manager

Usage:
    py price_checker.py products.json
    py price_checker.py products.json --delay 2
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # removed 'br' to avoid brotli issues
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "DNT": "1",
}

PAGE_LOAD_TIMEOUT = 45  # seconds

# Lazily-created Selenium driver
_selenium_driver = None
_warmed_up_domains = set()


def get_selenium_driver():
    global _selenium_driver
    if _selenium_driver is None:
        print("    [selenium] launching headless Chrome (first run may need to "
              "download ChromeDriver, can take a minute)...", file=sys.stderr)
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,2000")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        _selenium_driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
        _selenium_driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        print("    [selenium] browser ready", file=sys.stderr)
    return _selenium_driver


def close_selenium_driver():
    global _selenium_driver
    if _selenium_driver is not None:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
        _selenium_driver = None


def url_slug(url):
    """Last path segment of a URL, used to check a page belongs to that URL."""
    return urlparse(url).path.rstrip("/").split("/")[-1]


def load_page_in_browser(url):
    """
    Load `url` in the shared Selenium browser and return page_source, or None
    if the page can't be trusted (timeout, redirect to another page, etc).
    Never returns the previous product's HTML.
    """
    from selenium.common.exceptions import TimeoutException

    driver = get_selenium_driver()
    try:
        driver.get("about:blank")   # drop the previous product's page
        driver.get(url)
    except TimeoutException:
        print(f"    [warning] page load timed out after {PAGE_LOAD_TIMEOUT}s, "
              f"skipping (page_source could be stale): {url}", file=sys.stderr)
        # Reset the browser so the next product starts clean
        try:
            driver.get("about:blank")
        except Exception:
            pass
        return None

    time.sleep(2.5)

    slug = url_slug(url)
    current = driver.current_url or ""
    if slug and slug not in current:
        print(f"    [warning] browser is on {current}, not {url}; skipping",
              file=sys.stderr)
        return None

    return driver.page_source


def clean_price(text):
    if not text:
        return None
    match = re.search(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", text)
    if not match:
        return None
    num_str = match.group(0).replace(",", "")
    try:
        return float(num_str)
    except ValueError:
        return None


def is_suspicious_change(old_price, new_price, max_change_pct):
    if old_price in (None, 0) or new_price in (None, 0):
        return False
    change_pct = abs(new_price - old_price) / old_price * 100
    return change_pct > max_change_pct


# --------------------------------------------------------------------------
# Bot-protection / challenge detection
# --------------------------------------------------------------------------

def is_cloudflare_challenge(html):
    """Return True if the HTML is a Cloudflare 'Checking your browser'
    interstitial or similar bot wall instead of a real product page."""
    indicators = [
        "checking your browser",
        "just a moment",
        "cf-browser-verification",
        "enable javascript and cookies to continue",
        "ddos protection by cloudflare",
        "challenge-platform",
        "turnstile",
        "please wait",
        "redirecting",
    ]
    text = html.lower()
    return any(ind in text for ind in indicators)


# --------------------------------------------------------------------------
# Warm-up & fetch helpers
# --------------------------------------------------------------------------

def warm_up_domain(session, url):
    domain = urlparse(url).netloc
    if domain in _warmed_up_domains:
        return
    _warmed_up_domains.add(domain)
    homepage = f"{urlparse(url).scheme}://{domain}/"
    try:
        session.get(homepage, headers=HEADERS, timeout=15)
        time.sleep(0.5)
    except Exception:
        pass


def fetch_with_retries(session, url, retries=2, backoff=2.0):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else None
            if status in (403, 429, 500, 502, 503) and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    if last_exc:
        raise last_exc


# --------------------------------------------------------------------------
# Availability helpers
# --------------------------------------------------------------------------

def normalize_availability(raw):
    if not raw:
        return "unknown"
    raw = raw.lower().replace(" ", "").replace("_", "").replace("-", "").replace("https://schema.org/", "")
    if raw in ("instock", "available", "instockforshipping"):
        return "available"
    if raw in ("outofstock", "unavailable", "soldout"):
        return "not available"
    return "unknown"


def _is_product_schema(node):
    """Flexible check for schema.org Product type."""
    if not isinstance(node, dict):
        return False
    types = node.get("@type", [])
    if isinstance(types, str):
        types = [types]
    return "Product" in types


def extract_woocommerce_availability(html):
    soup = BeautifulSoup(html, "html.parser")

    # 1. Schema.org JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            subs = graph if isinstance(graph, list) else [node]
            for sub in subs:
                if not _is_product_schema(sub):
                    continue
                offers = sub.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict):
                    avail = normalize_availability(offers.get("availability"))
                    if avail != "unknown":
                        return avail

    # 2. CSS selectors
    if soup.select_one(".stock.out-of-stock, .out-of-stock, .sold-out, .unavailable"):
        return "not available"
    if soup.select_one(".stock.in-stock, .in-stock, .available"):
        return "available"

    # 3. Text inside stock wrapper
    stock_el = soup.select_one(".stock, .availability, .product-availability")
    if stock_el:
        text = stock_el.get_text().lower()
        if any(x in text for x in ["out of stock", "sold out", "unavailable"]):
            return "not available"
        if any(x in text for x in ["in stock", "available"]):
            return "available"

    return "unknown"


def extract_webx_availability(html):
    soup = BeautifulSoup(html, "html.parser")
    avail = extract_woocommerce_availability(html)
    if avail != "unknown":
        return avail

    main_area = (
        soup.select_one(".product-detail")
        or soup.select_one(".product-page")
        or soup.select_one("main")
        or soup
    )
    text = main_area.get_text().lower()

    if any(x in text for x in ["out of stock", "sold out", "unavailable"]):
        return "not available"

    btn = soup.select_one(".add-to-cart, .btn-add-cart, [class*='addToCart'], [class*='add-cart']")
    if btn and btn.has_attr("disabled"):
        return "not available"

    if any(x in text for x in ["in stock", "available", "add to cart"]):
        return "available"

    return "unknown"


def extract_generic_availability(html):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main, .content, .product, article") or soup
    text = main.get_text().lower()
    if any(x in text for x in ["out of stock", "sold out", "unavailable"]):
        return "not available"
    if any(x in text for x in ["in stock", "available"]):
        return "available"
    return "unknown"


# --------------------------------------------------------------------------
# Price extractors
# --------------------------------------------------------------------------

def extract_structured_price(html, url=None, strict=False):
    """
    Read the price from schema.org JSON-LD (then a few meta tags).

    url + strict=True: only accept a JSON-LD Product whose own URL matches
    the product URL we asked for, and skip the meta-tag fallbacks entirely.
    This prevents picking up another product's price from a stale or
    wrong page.
    """
    soup = BeautifulSoup(html, "html.parser")
    slug = url_slug(url) if (url and strict) else None

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            sub_candidates = graph if isinstance(graph, list) else [node]
            for sub in sub_candidates:
                if not _is_product_schema(sub):
                    continue
                offers = sub.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if not isinstance(offers, dict):
                    continue
                if slug:
                    node_url = str(offers.get("url") or sub.get("url") or "")
                    if slug not in node_url:
                        continue  # JSON-LD belongs to a different product
                price = offers.get("price") or offers.get("lowPrice")
                if price:
                    cleaned = clean_price(str(price))
                    if cleaned:
                        return cleaned

    if strict:
        return None

    for attrs in (
        {"property": "product:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            cleaned = clean_price(tag["content"])
            if cleaned:
                return cleaned
    return None


def extract_woocommerce_price(html):
    soup = BeautifulSoup(html, "html.parser")
    price_el = (
        soup.select_one(".product-summary .price-wrapper")
        or soup.select_one(".entry-summary .price-wrapper")
        or soup.select_one("div.price-wrapper")
        or soup.select_one("p.price, span.price, .summary .price")
    )
    if not price_el:
        return None
    ins_el = price_el.select_one("ins .woocommerce-Price-amount, ins")
    if ins_el:
        amt = ins_el.select_one(".woocommerce-Price-amount") or ins_el
        return clean_price(amt.get_text())
    amt_el = price_el.select_one(".woocommerce-Price-amount")
    if amt_el:
        return clean_price(amt_el.get_text())
    return clean_price(price_el.get_text())


def extract_webx_price(url):
    """
    Junaid Tech / czone (Webx platform). JSON-LD only, URL-matched.
    Returns (price, html) so availability can be read from the same page.
    Returns (None, None) if the page can't be trusted or has no matching price.
    """
    html = load_page_in_browser(url)
    if html is None:
        return None, None
    price = extract_structured_price(html, url, strict=True)
    if price is None:
        print(f"    [webx] no JSON-LD price matching this URL, not guessing: {url}",
              file=sys.stderr)
        return None, html
    return price, html


def extract_generic_woocommerce(html):
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        ".woocommerce-Price-amount",
        ".product-price",
        ".price-amount",
        ".current-price",
        "[itemprop='price']",
    ]:
        el = soup.select_one(selector)
        if el:
            price = clean_price(el.get_text() if not el.has_attr("content") else el["content"])
            if price:
                return price
    return None


def extract_generic_regex(html):
    matches = re.findall(r"Rs\.?\s?[\d,]{4,}", html)
    if matches:
        return clean_price(matches[0])
    return None


# --------------------------------------------------------------------------
# WooCommerce via Selenium fallback
# --------------------------------------------------------------------------

def fetch_woocommerce_via_selenium(url):
    html = load_page_in_browser(url)
    if html is None:
        return None, "unknown"

    price = extract_structured_price(html)
    if price is None:
        price = extract_woocommerce_price(html)
    if price is None:
        price = extract_generic_woocommerce(html)
    if price is None:
        price = extract_generic_regex(html)

    availability = extract_woocommerce_availability(html)
    if availability == "unknown":
        availability = extract_generic_availability(html)

    return price, availability


# --------------------------------------------------------------------------
# Zah Computers (ShopinoErp) - server-rendered JSON-LD, no Selenium needed
# when the site lets the request through
# --------------------------------------------------------------------------

MAX_CONSECUTIVE_BLOCKS = 3
_block_counts = {}


def _find_matching_offer(html, url):
    """Return the JSON-LD Offer of the Product whose URL matches `url`."""
    soup = BeautifulSoup(html, "html.parser")
    slug = url_slug(url)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            for sub in (graph if isinstance(graph, list) else [node]):
                if not _is_product_schema(sub):
                    continue
                offers = sub.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if not isinstance(offers, dict):
                    continue
                node_url = str(offers.get("url") or sub.get("@id") or sub.get("url") or "")
                if slug and slug in node_url:
                    return offers
    return None


def _meta_content(html, **attrs):
    tag = BeautifulSoup(html, "html.parser").find("meta", attrs=attrs)
    return tag.get("content") if tag and tag.get("content") else None


def get_shopino_price_and_availability(session, url):
    domain = urlparse(url).netloc.replace("www.", "")

    if _block_counts.get(domain, 0) >= MAX_CONSECUTIVE_BLOCKS:
        print(f"    [shopino] {domain} blocked {MAX_CONSECUTIVE_BLOCKS} times in a row, "
              f"skipping the rest of this site for this run", file=sys.stderr)
        return None, "unknown"

    html = None
    try:
        warm_up_domain(session, url)
        resp = fetch_with_retries(session, url, retries=1)
        html = resp.text
        # Only treat it as a bot wall if the real product data is missing
        if is_cloudflare_challenge(html) and _find_matching_offer(html, url) is None:
            html = None
    except Exception as e:
        print(f"    [shopino] plain request failed ({e}), trying headless browser", file=sys.stderr)
        html = None

    if html is None:
        try:
            html = load_page_in_browser(url)
        except Exception as e:
            print(f"    [shopino] headless browser failed: {e}", file=sys.stderr)
            html = None

    if html is None or (is_cloudflare_challenge(html) and _find_matching_offer(html, url) is None):
        _block_counts[domain] = _block_counts.get(domain, 0) + 1
        return None, "unknown"

    _block_counts[domain] = 0  # got a real page

    offers = _find_matching_offer(html, url)
    price = None
    availability = "unknown"
    if offers:
        price = clean_price(str(offers.get("price") or ""))
        availability = normalize_availability(offers.get("availability"))

    # Meta-tag fallback, only if the page's own URL matches the product URL
    if price is None:
        page_url = _meta_content(html, property="og:url") or ""
        if url_slug(url) in page_url:
            price = clean_price(_meta_content(html, property="product:price:amount") or "")
    if availability == "unknown":
        availability = normalize_availability(
            (_meta_content(html, property="product:availability") or "")
        )

    if price is None:
        print(f"    [shopino] no price matching this URL, not guessing: {url}", file=sys.stderr)
        return None, "unknown"
    return price, availability


# --------------------------------------------------------------------------
# Unified fetch: returns (price, availability)
# --------------------------------------------------------------------------

WOOCOMMERCE_DOMAINS = ["amdhouse.pk", "zicomputer.com", "rbtechngames.com"]
SHOPINO_DOMAINS = ["zahcomputers.pk"]   # ShopinoErp storefront (server-rendered JSON-LD)
WEBX_DOMAINS = ["junaidtech.pk", "czone.com.pk"]


def get_price_and_availability(session, url):
    domain = urlparse(url).netloc.replace("www.", "")
    price = None
    availability = "unknown"

    # --- Zah Computers (ShopinoErp)
    if any(d in domain for d in SHOPINO_DOMAINS):
        return get_shopino_price_and_availability(session, url)

    # --- WooCommerce stores
    elif any(d in domain for d in WOOCOMMERCE_DOMAINS):
        try:
            warm_up_domain(session, url)
            resp = fetch_with_retries(session, url)
            html = resp.text

            # If we got a bot challenge page, skip straight to Selenium
            if is_cloudflare_challenge(html):
                print(f"    [woocommerce] bot challenge detected, using headless browser for {url}",
                      file=sys.stderr)
                return fetch_woocommerce_via_selenium(url)

            # Price
            price = extract_structured_price(html)
            if price is None:
                price = extract_woocommerce_price(html)
            if price is None:
                price = extract_generic_woocommerce(html)
            if price is None:
                price = extract_generic_regex(html)

            # Availability
            availability = extract_woocommerce_availability(html)
            if availability == "unknown":
                availability = extract_generic_availability(html)

            # If we still have no price, the page likely rendered but uses JS
            # or unusual markup -- fall back to Selenium as a last resort.
            if price is None:
                print(f"    [woocommerce] no price extracted, trying headless browser for {url}",
                      file=sys.stderr)
                return fetch_woocommerce_via_selenium(url)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 403:
                print(f"    [woocommerce] 403 from requests, falling back to "
                      f"headless browser for {url}", file=sys.stderr)
                try:
                    return fetch_woocommerce_via_selenium(url)
                except Exception as e2:
                    print(f"    [woocommerce selenium fallback failed] {e2}", file=sys.stderr)
                    return None, "unknown"
            else:
                print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
                return None, "unknown"
        except Exception as e:
            print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
            return None, "unknown"

    # --- Webx stores (Selenium, JSON-LD only)
    elif any(d in domain for d in WEBX_DOMAINS):
        try:
            price, html = extract_webx_price(url)
            if price is None:
                return None, "unknown"
            availability = extract_webx_availability(html)
        except Exception as e:
            print(f"    [webx fetch failed] {e}", file=sys.stderr)
            return None, "unknown"

    # --- Unknown domain
    else:
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            html = resp.text
            price = extract_generic_woocommerce(html)
            if price is None:
                price = extract_generic_regex(html)
            availability = extract_generic_availability(html)
        except Exception as e:
            print(f"    [unknown-domain fetch failed] {e}", file=sys.stderr)
            return None, "unknown"

    return price, availability


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Check and update prices & availability in products.json")
    parser.add_argument("json_file", help="Path to products.json")
    parser.add_argument("--delay", type=float, default=2, help="Seconds between requests")
    parser.add_argument(
        "--max-change-pct",
        type=float,
        default=20.0,
        help="If a scraped price differs from the stored price by more than this "
             "percent, treat it as a likely scraping error and don't overwrite it "
             "(default: 20)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Don't re-fetch a page to confirm a changed price before saving it",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing a .bak copy of the JSON file before overwriting it",
    )
    parser.add_argument(
        "--store",
        metavar="TEXT",
        help="Only check store prices whose storeName contains TEXT (case-insensitive). "
             "Example: --store zah",
    )
    parser.add_argument(
        "--skip-store",
        metavar="TEXT",
        help="Skip store prices whose storeName contains TEXT (case-insensitive). "
             "Useful on GitHub Actions for Cloudflare-blocked stores like Zah. "
             "Example: --skip-store zah",
    )
    args = parser.parse_args()

    with open(args.json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not args.no_backup:
        backup_path = args.json_file + ".bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Backup written to {backup_path}")

    session = requests.Session()
    updated, unchanged, failed, suspicious = 0, 0, 0, 0
    processed = []

    try:
        for product in data["products"]:
            for sp in product["storePrices"]:
                url = sp.get("url")
                if not url:
                    continue

                store_name = sp.get("storeName") or ""
                if args.store and args.store.lower() not in store_name.lower():
                    continue
                if args.skip_store and args.skip_store.lower() in store_name.lower():
                    continue

                processed.append(sp)
                label = sp.get("Name") or sp.get("name") or product.get("name")
                print(f"Checking {sp.get('storeName')} | {label} ...", flush=True)

                new_price, availability = get_price_and_availability(session, url)
                old_price = sp.get("price")

                # Only trust availability when the page was read successfully
                if new_price is not None:
                    sp["availability"] = availability

                if new_price is None:
                    print(f"FAILED     {sp.get('storeName')} | {label} -> could not read price, "
                          f"kept {old_price} ({url})")
                    failed += 1
                elif new_price == old_price:
                    # Price unchanged: leave previousprice / updatedAt as they are,
                    # so a recent real change is not erased by this run.
                    print(f"NO CHANGE  {sp.get('storeName')} | {label} : {old_price} "
                          f"[availability: {availability}]")
                    unchanged += 1
                elif is_suspicious_change(old_price, new_price, args.max_change_pct):
                    print(
                        f"SUSPICIOUS {sp.get('storeName')} | {label} : {old_price} -> {new_price} "
                        f"(>{args.max_change_pct:.0f}% change, left unchanged, check manually) "
                        f"[availability: {availability}] ({url})"
                    )
                    suspicious += 1
                else:
                    # Confirm the change with a second fetch before saving it
                    if not args.no_confirm:
                        time.sleep(args.delay)
                        confirm_price, _ = get_price_and_availability(session, url)
                        if confirm_price != new_price:
                            print(f"UNCONFIRMED {sp.get('storeName')} | {label} : "
                                  f"{old_price} -> {new_price} (re-read gave {confirm_price}), "
                                  f"left unchanged")
                            suspicious += 1
                            time.sleep(args.delay)
                            continue

                    print(f"UPDATED    {sp.get('storeName')} | {label} : {old_price} -> {new_price} "
                          f"[availability: {availability}]")
                    # Old price moves to previousprice, new price goes in price.
                    # The app computes price - previousprice (0 = no notification,
                    # positive = increase, negative = decrease).
                    sp["previousprice"] = old_price
                    sp["price"] = new_price
                    sp["updatedAt"] = int(time.time())  # unix seconds
                    updated += 1

                time.sleep(args.delay)
    finally:
        close_selenium_driver()

    with open(args.json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Availability summary
    avail_stats = {}
    for sp in processed:
        a = sp.get("availability", "unknown")
        avail_stats[a] = avail_stats.get(a, 0) + 1

    print(
        f"\nDone. {updated} updated, {unchanged} unchanged, "
        f"{suspicious} suspicious/unconfirmed (skipped), {failed} failed."
    )
    print(
        f"Availability summary: "
        f"{avail_stats.get('available', 0)} available | "
        f"{avail_stats.get('not available', 0)} not available | "
        f"{avail_stats.get('unknown', 0)} unknown."
    )
    if suspicious:
        print(
            f"{suspicious} price(s) were suspicious or unconfirmed and left untouched — "
            f"review those URLs manually."
        )


if __name__ == "__main__":
    main()
