"""
Unified price & availability checker for products.json
--------------------------------------------------------------------------
Reads products.json, visits every store URL, and updates both price and
availability.

Usage:
    py price_checker.py products.json
    py price_checker.py products.json --delay 2
    py price_checker.py products.json --category-id graphics_cards
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
    "Accept-Encoding": "gzip, deflate",
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

PAGE_LOAD_TIMEOUT = 30
CHALLENGE_WAIT = 20
DEBUG_DIR = "debug_pages"

_selenium_driver = None
_warmed_up_domains = set()


def get_selenium_driver():
    global _selenium_driver
    if _selenium_driver is None:
        print("    [selenium] launching Chrome...", file=sys.stderr)
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        options.add_argument("--window-size=1400,2000")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.page_load_strategy = "eager"

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
# URL & Slug Helpers
# --------------------------------------------------------------------------

def url_slug(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slug_in(slug, candidate):
    if not slug:
        return False
    return _norm(slug) in _norm(urlparse(str(candidate)).path or str(candidate))


def page_identity_ok(html, url):
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


def is_cloudflare_challenge(html):
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


def warm_up_domain(session, url):
    domain = urlparse(url).netloc
    if domain in _warmed_up_domains:
        return
    _warmed_up_domains.add(domain)
    homepage = f"{urlparse(url).scheme}://{domain}/"
    try:
        session.get(homepage, headers=HEADERS, timeout=10)
        time.sleep(0.3)
    except Exception:
        pass


def fetch_with_retries(session, url, retries=1, backoff=1.5):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=15)
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
# Parsing Helpers
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
    if not isinstance(node, dict):
        return False
    types = node.get("@type", [])
    if isinstance(types, str):
        types = [types]
    return "Product" in types


def _iter_products(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            try:
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


def load_page_in_browser(url):
    from selenium.common.exceptions import TimeoutException

    driver = get_selenium_driver()
    try:
        driver.get("about:blank")
        driver.get(url)
    except TimeoutException:
        pass
    except Exception:
        return None

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
        time.sleep(1.0)
    time.sleep(0.5)

    slug = url_slug(url)
    current = driver.current_url or ""
    if slug and not slug_in(slug, current):
        return None

    try:
        return driver.page_source
    except Exception:
        return None


def extract_woocommerce_availability(html):
    soup = BeautifulSoup(html, "html.parser")
    for sub in _iter_products(html):
        offer = _first_offer(sub)
        if offer:
            avail = normalize_availability(offer.get("availability"))
            if avail != "unknown":
                return avail
    if soup.select_one(".stock.out-of-stock, .out-of-stock, .sold-out, .unavailable"):
        return "not available"
    if soup.select_one(".stock.in-stock, .in-stock, .available"):
        return "available"
    return "unknown"


def extract_webx_availability(html):
    avail = extract_woocommerce_availability(html)
    if avail != "unknown":
        return avail
    soup = BeautifulSoup(html, "html.parser")
    main_area = soup.select_one(".product-detail") or soup.select_one("main") or soup
    text = main_area.get_text().lower()
    if any(x in text for x in ["out of stock", "sold out", "unavailable"]):
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
    slug = url_slug(url) if (url and strict) else None
    priced = []
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
    return None


def extract_woocommerce_price(html):
    soup = BeautifulSoup(html, "html.parser")
    price_el = soup.select_one("p.price, span.price, .summary .price, div.price-wrapper")
    if not price_el:
        return None
    ins_el = price_el.select_one("ins")
    if ins_el:
        return clean_price(ins_el.get_text())
    return clean_price(price_el.get_text())


def extract_webx_price(session, url):
    slug = url_slug(url)
    last_html = None
    try:
        warm_up_domain(session, url)
        resp = fetch_with_retries(session, url, retries=1)
        html = resp.text
        final_url = str(getattr(resp, "url", url))
        last_html = html
        if not is_cloudflare_challenge(html) and slug_in(slug, final_url):
            price = extract_structured_price(html, url, strict=True, page_verified=True)
            if price is not None:
                return price, html
    except Exception:
        pass

    html = load_page_in_browser(url)
    if html is not None:
        last_html = html
        price = extract_structured_price(html, url, strict=True, page_verified=True)
        if price is not None:
            return price, html

    return None, last_html


def fetch_woocommerce_via_selenium(url):
    html = load_page_in_browser(url)
    if html is None:
        return None, "unknown"
    price = extract_structured_price(html) or extract_woocommerce_price(html)
    availability = extract_woocommerce_availability(html)
    return price, availability


def _find_matching_offer(html, url):
    slug = url_slug(url)
    for sub in _iter_products(html):
        offers = _first_offer(sub)
        if not offers:
            continue
        for u in _node_urls(sub, offers):
            if slug and slug_in(slug, u):
                return offers
    return None


def get_shopino_price_and_availability(session, url):
    html = None
    try:
        warm_up_domain(session, url)
        resp = fetch_with_retries(session, url, retries=1)
        html = resp.text
        if is_cloudflare_challenge(html):
            html = None
    except Exception:
        html = None

    if html is None:
        html = load_page_in_browser(url)

    if html is None:
        return None, "unknown"

    offers = _find_matching_offer(html, url)
    if offers:
        return clean_price(str(offers.get("price") or "")), normalize_availability(offers.get("availability"))
    return None, "unknown"


# --------------------------------------------------------------------------
# Unified fetch
# --------------------------------------------------------------------------

WOOCOMMERCE_DOMAINS = ["amdhouse.pk", "zicomputer.com", "rbtechngames.com"]
SHOPINO_DOMAINS = ["zahcomputers.pk"]
WEBX_DOMAINS = ["junaidtech.pk", "czone.com.pk"]


def get_price_and_availability(session, url):
    domain = urlparse(url).netloc.replace("www.", "")

    if any(d in domain for d in SHOPINO_DOMAINS):
        return get_shopino_price_and_availability(session, url)

    elif any(d in domain for d in WOOCOMMERCE_DOMAINS):
        try:
            warm_up_domain(session, url)
            resp = fetch_with_retries(session, url)
            html = resp.text
            if is_cloudflare_challenge(html):
                return fetch_woocommerce_via_selenium(url)

            price = extract_structured_price(html) or extract_woocommerce_price(html)
            availability = extract_woocommerce_availability(html)
            if price is None:
                return fetch_woocommerce_via_selenium(url)
            return price, availability
        except Exception:
            try:
                return fetch_woocommerce_via_selenium(url)
            except Exception:
                return None, "unknown"

    elif any(d in domain for d in WEBX_DOMAINS):
        try:
            price, html = extract_webx_price(session, url)
            if price is None:
                return None, "unknown"
            return price, extract_webx_availability(html)
        except Exception:
            return None, "unknown"

    return None, "unknown"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Check and update prices & availability in products.json")
    parser.add_argument("json_file", help="Path to products.json")
    parser.add_argument("--delay", type=float, default=2, help="Seconds between requests")
    parser.add_argument("--max-change-pct", type=float, default=20.0, help="Max percent change limit")
    parser.add_argument("--no-confirm", action="store_true", help="Don't re-fetch to confirm change")
    parser.add_argument("--no-backup", action="store_true", help="Skip writing .bak copy")
    parser.add_argument("--store", metavar="TEXT", help="Only check store prices whose storeName contains TEXT")
    parser.add_argument("--skip-store", metavar="TEXT", help="Skip store prices whose storeName contains TEXT")
    parser.add_argument("--category-id", metavar="ID", help="Only check products matching this categoryId")
    args = parser.parse_args()

    with open(args.json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not args.no_backup:
        with open(args.json_file + ".bak", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    session = requests.Session(impersonate="chrome")
    updated, unchanged, failed, suspicious = 0, 0, 0, 0
    processed = []

    try:
        for product in data["products"]:
            # Filter by category if specified via CLI
            if args.category_id and product.get("categoryId") != args.category_id:
                continue

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

                if new_price is not None:
                    sp["availability"] = availability

                if new_price is None:
                    print(f"FAILED     {sp.get('storeName')} | {label} -> could not read price")
                    failed += 1
                elif new_price == old_price:
                    print(f"NO CHANGE  {sp.get('storeName')} | {label} : {old_price} [availability: {availability}]")
                    unchanged += 1
                elif is_suspicious_change(old_price, new_price, args.max_change_pct):
                    print(f"SUSPICIOUS {sp.get('storeName')} | {label} : {old_price} -> {new_price} (skipped)")
                    suspicious += 1
                else:
                    if not args.no_confirm:
                        time.sleep(args.delay)
                        confirm_price, _ = get_price_and_availability(session, url)
                        if confirm_price != new_price:
                            suspicious += 1
                            continue

                    print(f"UPDATED    {sp.get('storeName')} | {label} : {old_price} -> {new_price}")
                    sp["previousprice"] = old_price
                    sp["price"] = new_price
                    sp["updatedAt"] = int(time.time())
                    updated += 1

                time.sleep(args.delay)
    finally:
        close_selenium_driver()

    with open(args.json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {updated} updated, {unchanged} unchanged, {suspicious} suspicious, {failed} failed.")


if __name__ == "__main__":
    main()
