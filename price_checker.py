"""
Unified price & availability checker for products.json
--------------------------------------------------------------------------
Reads products.json, visits every store URL, and updates both price and
availability.

CHANGELOG (this version):
- FIX (czone / junaidtech "no JSON-LD price matching this URL"):
    * Webx stores now try a fast curl_cffi request first, then fall back to
      the browser. The page is only trusted if its final URL matches.
    * Browser waits for the page to really load (JSON-LD present) and waits
      out Cloudflare / "checking your browser" pages instead of reading them.
    * URL matching is now forgiving: slug compared ignoring case, trailing
      slashes and punctuation, and checked against JSON-LD url/@id/
      mainEntityOfPage, canonical and og:url.
    * If the JSON-LD Product has no URL (or a different format) but the page
      itself is verified to be the right one, and there is exactly one
      priced Product on it, that price is accepted. Meta tags are used as a
      last verified fallback.
    * When a read still fails, a diagnostic line is printed and the page is
      saved to debug_pages/ so you can see exactly what the runner received.
- Earlier fixes kept: stale-page protection, Zah reader, price-change
  confirmation, max-change-pct, availability only on successful read.

Install deps:
    pip install beautifulsoup4 selenium webdriver-manager curl_cffi

Usage:
    py price_checker.py products.json
    py price_checker.py products.json --delay 2
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

# curl_cffi impersonates a real Chrome TLS fingerprint (helps with Cloudflare)
from curl_cffi import requests
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
    "Accept-Encoding": "gzip, deflate",   # no 'br' to avoid brotli issues
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

PAGE_LOAD_TIMEOUT = 45      # seconds
CHALLENGE_WAIT = 25         # max seconds to wait for a bot-check page to clear
DEBUG_DIR = "debug_pages"

# Lazily-created Selenium driver
_selenium_driver = None
_warmed_up_domains = set()


def get_selenium_driver():
    global _selenium_driver
    if _selenium_driver is None:
        print("    [selenium] launching Chrome (first run may need to "
              "download ChromeDriver, can take a minute)...", file=sys.stderr)
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        # Not headless: needed for GitHub Actions + xvfb to pass bot checks
        options.add_argument("--window-size=1400,2000")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.page_load_strategy = "eager"   # don't wait for every image/ad

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


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def url_slug(url):
    """Last path segment of a URL, used to check a page belongs to that URL."""
    return urlparse(url).path.rstrip("/").split("/")[-1]


def _norm(s):
    """Lowercase and strip everything except letters/digits, for loose compare."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slug_in(slug, candidate):
    """True if the product slug appears in the candidate URL/string."""
    if not slug:
        return False
    return _norm(slug) in _norm(urlparse(str(candidate)).path or str(candidate))


def page_identity_ok(html, url):
    """
    True if the page's own canonical / og:url says it is this product.
    Used to decide whether a JSON-LD block with a missing or odd URL can
    still be trusted.
    """
    slug = url_slug(url)
    soup = BeautifulSoup(html, "html.parser")
    for tag, attr in (("link", {"rel": "canonical"}),
                      ("meta", {"property": "og:url"})):
        el = soup.find(tag, attrs=attr)
        if el:
            val = el.get("href") or el.get("content") or ""
            if slug_in(slug, val):
                return True
    return False


# --------------------------------------------------------------------------
# Debug helpers
# --------------------------------------------------------------------------

def describe_page(html):
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.get_text().strip() if soup.title else "")[:80]
    ld = soup.find_all("script", type="application/ld+json")
    return f"title={title!r} len={len(html or '')} ld+json_blocks={len(ld)}"


def dump_debug_page(url, html, note=""):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = _norm(urlparse(url).netloc) + "_" + (_norm(url_slug(url))[:60] or "page")
        path = os.path.join(DEBUG_DIR, name + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<!-- {note} {url} -->\n")
            f.write((html or "")[:400000])
        print(f"    [debug] saved what the runner received to {path}", file=sys.stderr)
    except Exception as e:
        print(f"    [debug] could not save page: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Browser page loading
# --------------------------------------------------------------------------

def load_page_in_browser(url):
    """
    Load `url` in the shared Selenium browser and return page_source, or None
    if the page can't be trusted (timeout, redirect to another page, bot wall
    that never cleared, etc). Never returns the previous product's HTML.
    """
    from selenium.common.exceptions import TimeoutException

    driver = get_selenium_driver()
    try:
        driver.get("about:blank")   # drop the previous product's page
        driver.get(url)
    except TimeoutException:
        print(f"    [warning] page load timed out after {PAGE_LOAD_TIMEOUT}s; "
              f"checking whether enough loaded: {url}", file=sys.stderr)
        # With 'eager' loading a timeout can still leave a usable page, but
        # only trust it if the URL is right (checked below).

    except Exception as e:
        print(f"    [warning] browser error: {e}", file=sys.stderr)
        try:
            driver.get("about:blank")
        except Exception:
            pass
        return None

    # Wait for any bot-check page to clear and for JSON-LD to appear
    deadline = time.time() + CHALLENGE_WAIT
    html = ""
    while time.time() < deadline:
        try:
            html = driver.page_source or ""
        except Exception:
            html = ""
        has_ld = 'application/ld+json' in html
        if has_ld and not (is_cloudflare_challenge(html) and not has_ld):
            break
        time.sleep(1.5)
    time.sleep(1.0)

    slug = url_slug(url)
    current = driver.current_url or ""
    if slug and not slug_in(slug, current):
        print(f"    [warning] browser is on {current}, not {url}; skipping",
              file=sys.stderr)
        return None

    try:
        return driver.page_source
    except Exception:
        return None


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
    """True if the HTML looks like a bot-check interstitial, not a product page."""
    indicators = [
        "checking your browser",
        "just a moment",
        "cf-browser-verification",
        "enable javascript and cookies to continue",
        "ddos protection by cloudflare",
        "challenge-platform",
        "turnstile",
        "attention required",
        "verify you are human",
    ]
    text = (html or "").lower()
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
        except Exception as e:
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
    raw = (str(raw).lower().replace(" ", "").replace("_", "").replace("-", "")
           .replace("https://schema.org/", "").replace("http://schema.org/", ""))
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


def _iter_products(html):
    """Yield every JSON-LD Product node on the page."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            try:   # some sites emit stray control characters
                payload = json.loads(raw, strict=False)
            except Exception:
                continue
        candidates = payload if isinstance(payload, list) else [payload]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            subs = graph if isinstance(graph, list) else [node]
            for sub in subs:
                if _is_product_schema(sub):
                    yield sub


def _first_offer(product):
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    return offers if isinstance(offers, dict) else None


def _node_urls(product, offer):
    """All URL-ish strings a JSON-LD Product/Offer might carry."""
    vals = [product.get("url"), product.get("@id"), product.get("mainEntityOfPage")]
    if offer:
        vals += [offer.get("url"), offer.get("@id")]
    out = []
    for v in vals:
        if isinstance(v, dict):
            v = v.get("@id") or v.get("url")
        if v:
            out.append(str(v))
    return out


def extract_woocommerce_availability(html):
    soup = BeautifulSoup(html, "html.parser")

    # 1. Schema.org JSON-LD
    for sub in _iter_products(html):
        offer = _first_offer(sub)
        if offer:
            avail = normalize_availability(offer.get("availability"))
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

def _offer_price(offer):
    if not offer:
        return None
    price = offer.get("price") or offer.get("lowPrice")
    if not price:
        spec = offer.get("priceSpecification")
        if isinstance(spec, list):
            spec = spec[0] if spec else None
        if isinstance(spec, dict):
            price = spec.get("price")
    return clean_price(str(price)) if price else None


def extract_structured_price(html, url=None, strict=False, page_verified=False):
    """
    Read the price from schema.org JSON-LD (then a few meta tags).

    strict=True + url: only accept a JSON-LD Product that belongs to `url`:
      1. its url/@id/offer url contains the product slug, OR
      2. the page itself is verified (page_verified, or canonical/og:url
         matches) AND the page has exactly one priced Product.
    Meta-tag fallbacks are used in strict mode only for a verified page.
    """
    slug = url_slug(url) if (url and strict) else None

    priced = []   # (price, matches_url)
    for sub in _iter_products(html):
        offer = _first_offer(sub)
        price = _offer_price(offer)
        if price is None:
            continue
        matches = any(slug_in(slug, u) for u in _node_urls(sub, offer)) if slug else True
        priced.append((price, matches))

    if not strict:
        if priced:
            return priced[0][0]
    else:
        for price, matches in priced:
            if matches:
                return price
        verified = page_verified or page_identity_ok(html, url)
        if verified and len(priced) == 1:
            return priced[0][0]
        if not verified:
            return None

    # Meta-tag fallbacks (non-strict always; strict only if page verified)
    soup = BeautifulSoup(html, "html.parser")
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
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


def extract_webx_price(session, url):
    """
    Junaid Tech / czone (Webx platform). Structured data only, URL-verified.
    Tries a plain curl_cffi request first (fast), then the browser.
    Returns (price, html). (None, html_or_None) if nothing trustworthy found.
    """
    slug = url_slug(url)
    last_html = None

    # --- Attempt 1: plain request
    try:
        warm_up_domain(session, url)
        resp = fetch_with_retries(session, url, retries=1)
        html = resp.text
        final_url = str(getattr(resp, "url", url))
        last_html = html
        if is_cloudflare_challenge(html) and 'application/ld+json' not in html:
            print("    [webx] plain request got a bot-check page, using browser",
                  file=sys.stderr)
        elif not slug_in(slug, final_url):
            print(f"    [webx] plain request ended on {final_url}, using browser",
                  file=sys.stderr)
        else:
            price = extract_structured_price(html, url, strict=True, page_verified=True)
            if price is not None:
                return price, html
            print(f"    [webx] plain request has no usable price "
                  f"({describe_page(html)}), trying browser", file=sys.stderr)
    except Exception as e:
        print(f"    [webx] plain request failed ({e}), trying browser", file=sys.stderr)

    # --- Attempt 2: real browser
    html = load_page_in_browser(url)
    if html is not None:
        last_html = html
        price = extract_structured_price(html, url, strict=True, page_verified=True)
        if price is not None:
            return price, html

    # --- Failed: leave evidence
    print(f"    [webx] could not read a trustworthy price: {url}", file=sys.stderr)
    if last_html is not None:
        print(f"    [webx] last page seen: {describe_page(last_html)} "
              f"challenge={is_cloudflare_challenge(last_html)}", file=sys.stderr)
        dump_debug_page(url, last_html, "webx price read failed")
    else:
        print("    [webx] no page could be loaded at all (blocked / timeout)",
              file=sys.stderr)
    return None, last_html


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
# Zah Computers (ShopinoErp) - server-rendered JSON-LD
# --------------------------------------------------------------------------

MAX_CONSECUTIVE_BLOCKS = 3
_block_counts = {}


def _find_matching_offer(html, url):
    """Return the JSON-LD Offer of the Product whose URL matches `url`."""
    slug = url_slug(url)
    for sub in _iter_products(html):
        offers = _first_offer(sub)
        if not offers:
            continue
        for u in _node_urls(sub, offers):
            if slug and slug_in(slug, u):
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
        if slug_in(url_slug(url), page_url):
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
SHOPINO_DOMAINS = ["zahcomputers.pk"]
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

            if is_cloudflare_challenge(html):
                print(f"    [woocommerce] bot challenge detected, using browser for {url}",
                      file=sys.stderr)
                return fetch_woocommerce_via_selenium(url)

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

            if price is None:
                print(f"    [woocommerce] no price extracted, trying browser for {url}",
                      file=sys.stderr)
                return fetch_woocommerce_via_selenium(url)

        except Exception as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            if status == 403:
                print(f"    [woocommerce] 403 from requests, falling back to "
                      f"browser for {url}", file=sys.stderr)
                try:
                    return fetch_woocommerce_via_selenium(url)
                except Exception as e2:
                    print(f"    [woocommerce selenium fallback failed] {e2}", file=sys.stderr)
                    return None, "unknown"
            else:
                print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
                return None, "unknown"

    # --- Webx stores (plain request, then browser; structured data only)
    elif any(d in domain for d in WEBX_DOMAINS):
        try:
            price, html = extract_webx_price(session, url)
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

    session = requests.Session(impersonate="chrome")

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
                    sp["previousprice"] = old_price
                    sp["price"] = new_price
                    sp["updatedAt"] = int(time.time())  # unix seconds
                    updated += 1

                time.sleep(args.delay)
    finally:
        close_selenium_driver()

    with open(args.json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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
