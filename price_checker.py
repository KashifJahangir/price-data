"""
Unified price checker for products.json
--------------------------------------------------------------------------
Reads products.json, visits every store URL already in it, and updates
the price if it changed. Uses store-specific extraction logic (borrowed
from your individual scrapers) with a fallback chain:

    1. Try the store-specific parser (matched by domain in the URL)
    2. If that fails, try a generic WooCommerce parser
    3. If that fails, try a generic regex-based price sweep of the page
    4. If all fail, leave the price untouched and log it as failed

WooCommerce stores (amdhouse.pk, zahcomputers.pk, zicomputer.com,
rbtechngames.com) are fetched with plain `requests` — no JS rendering
needed, matches your existing scrapers.

Junaid Tech and Czone run on Webx Ecommerce (Vue/Nuxt) and need
Selenium to render the price — this is only spun up (lazily) if a
URL from those domains is encountered, so requests-only runs stay fast.

Install deps:
    pip install requests beautifulsoup4 selenium webdriver-manager

Usage:
    python price_checker.py products.json
    python price_checker.py products.json --delay 1.5
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
    "Accept-Language": "en-US,en;q=0.9",
}

# Lazily-created Selenium driver, shared across all Junaid Tech / Czone
# lookups in a single run so we don't spin up a new browser per product.
_selenium_driver = None


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
        # Without this, a page that never fires "load" (stuck spinner, endless
        # polling JS, etc.) hangs driver.get() forever with zero output.
        _selenium_driver.set_page_load_timeout(30)
        print("    [selenium] browser ready", file=sys.stderr)
    return _selenium_driver


def close_selenium_driver():
    global _selenium_driver
    if _selenium_driver is not None:
        _selenium_driver.quit()
        _selenium_driver = None


def clean_price(text):
    """Turn 'Rs. 24,500' / '₨24,500.00' / '24500' into 24500.0

    Extracts the FIRST well-formed number in the text instead of blindly
    stripping non-digit characters. This avoids two failure modes seen
    in production:
      - "Rs.14,999" -> old code kept the period from "Rs." and produced
        0.14999 instead of 14999.0
      - "Rs.32,155 - Rs.30,993" (a price range / two adjacent price nodes
        with no separator) -> old code glued both numbers together into
        3215530993.0 instead of picking one
    """
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
    """Flag implausible jumps (wrong element scraped) instead of trusting them blindly."""
    if old_price in (None, 0) or new_price in (None, 0):
        return False
    change_pct = abs(new_price - old_price) / old_price * 100
    return change_pct > max_change_pct


# --------------------------------------------------------------------------
# Store-specific extractors (built from your own scrapers' selectors)
# --------------------------------------------------------------------------

def extract_woocommerce_price(html):
    """Shared by amdhouse.pk, zahcomputers.pk, zicomputer.com, rbtechngames.com."""
    soup = BeautifulSoup(html, "html.parser")

    # Scope to the real per-product info panel first. The theme's actual
    # price container is div.price-wrapper, which ".price" alone never
    # matches (different class name) -- that mismatch was letting the old
    # selector fall through to unrelated price elements elsewhere on the
    # page (related-product grids, etc).
    price_el = (
        soup.select_one(".product-summary .price-wrapper")
        or soup.select_one(".entry-summary .price-wrapper")
        or soup.select_one("div.price-wrapper")
        or soup.select_one("p.price, span.price, .summary .price")
    )
    if not price_el:
        return None

    # Prefer the sale price (<ins>) over the struck-through original (<del>)
    ins_el = price_el.select_one("ins .woocommerce-Price-amount, ins")
    if ins_el:
        amt = ins_el.select_one(".woocommerce-Price-amount") or ins_el
        return clean_price(amt.get_text())

    amt_el = price_el.select_one(".woocommerce-Price-amount")
    if amt_el:
        return clean_price(amt_el.get_text())

    return clean_price(price_el.get_text())


def extract_structured_price(html):
    """Look for the product's official price in structured data (JSON-LD
    schema.org/Product, or Open Graph / itemprop meta tags). This data is
    written for search engines / social previews and is scoped to the
    actual product on the page -- unlike visible CSS price elements it
    can't accidentally match a 'related products' widget.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- JSON-LD schema.org/Product ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = payload if isinstance(payload, list) else [payload]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            # some sites wrap the Product inside "@graph"
            graph = node.get("@graph")
            sub_candidates = graph if isinstance(graph, list) else [node]
            for sub in sub_candidates:
                if not isinstance(sub, dict):
                    continue
                if sub.get("@type") not in ("Product", ["Product"]):
                    continue
                offers = sub.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        cleaned = clean_price(str(price))
                        if cleaned:
                            return cleaned

    # --- Meta tags (Open Graph / itemprop) ---
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


def extract_webx_price(url):
    """Junaid Tech / Czone -- Vue/Nuxt rendered, needs Selenium."""
    from selenium.common.exceptions import TimeoutException

    driver = get_selenium_driver()
    try:
        driver.get(url)
    except TimeoutException:
        # Page didn't finish loading within set_page_load_timeout(30).
        # Chrome usually has still rendered most of the DOM by then, so
        # try to read whatever's there instead of losing the product.
        print(f"    [warning] page load timed out after 30s, trying partial content: {url}",
              file=sys.stderr)
    time.sleep(2.5)
    html = driver.page_source

    # Prefer structured data: it's tied to the actual product regardless
    # of what "related products" widgets are also on the page.
    price = extract_structured_price(html)
    if price is not None:
        return price

    soup = BeautifulSoup(html, "html.parser")
    price_el = (
        soup.select_one("div.product-price")
        or soup.select_one(".price-wrapper .product-price")
        or soup.select_one(".product-detail-price")
    )
    if price_el:
        print(
            "    [warning] no structured data found, fell back to a CSS "
            "selector that may match related-product widgets -- verify this "
            "price manually",
            file=sys.stderr,
        )
        return clean_price(price_el.get_text())

    return None


# --------------------------------------------------------------------------
# Generic fallbacks (used if the store-specific method fails)
# --------------------------------------------------------------------------

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
    """Last resort: sweep the raw HTML for something that looks like 'Rs. 24,500'."""
    matches = re.findall(r"Rs\.?\s?[\d,]{4,}", html)
    if matches:
        return clean_price(matches[0])
    return None


# --------------------------------------------------------------------------
# Dispatch: pick the right method chain based on the URL's domain
# --------------------------------------------------------------------------

WOOCOMMERCE_DOMAINS = ["amdhouse.pk", "zahcomputers.pk", "zicomputer.com", "rbtechngames.com"]
WEBX_DOMAINS = ["junaidtech.pk", "czone.com.pk"]


def get_price_for_url(session, url):
    domain = urlparse(url).netloc.replace("www.", "")

    # --- WooCommerce stores: requests + WooCommerce parser, then generic fallbacks
    if any(d in domain for d in WOOCOMMERCE_DOMAINS):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            price = extract_structured_price(resp.text)
            if price is not None:
                return price
            price = extract_woocommerce_price(resp.text)
            if price is not None:
                return price
            price = extract_generic_woocommerce(resp.text)
            if price is not None:
                return price
            return extract_generic_regex(resp.text)
        except Exception as e:
            print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
            return None

    # --- Webx stores: Selenium-rendered parser, then generic fallback on the same HTML
    if any(d in domain for d in WEBX_DOMAINS):
        try:
            price = extract_webx_price(url)
            if price is not None:
                return price
            # fall back to a generic regex sweep of the rendered page
            driver = get_selenium_driver()
            return extract_generic_regex(driver.page_source)
        except Exception as e:
            print(f"    [webx fetch failed] {e}", file=sys.stderr)
            return None

    # --- Unknown domain: try requests + every generic method as a best effort
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        price = extract_generic_woocommerce(resp.text)
        if price is not None:
            return price
        return extract_generic_regex(resp.text)
    except Exception as e:
        print(f"    [unknown-domain fetch failed] {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Main: walk products.json, check every URL, update prices in place
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Check and update prices in products.json")
    parser.add_argument("json_file", help="Path to products.json")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests")
    parser.add_argument(
        "--max-change-pct",
        type=float,
        default=50.0,
        help="If a scraped price differs from the stored price by more than this "
             "percent, treat it as a likely scraping error and don't overwrite it "
             "(default: 50)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing a .bak copy of the JSON file before overwriting it",
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

    try:
        for product in data["products"]:
            for sp in product["storePrices"]:
                url = sp.get("url")
                if not url:
                    continue

                label = sp.get("Name") or sp.get("name") or product.get("name")
                print(f"Checking {sp.get('storeName')} | {label} ...", flush=True)
                new_price = get_price_for_url(session, url)

                old_price = sp.get("price")

                if new_price is None:
                    print(f"FAILED     {sp.get('storeName')} | {label} -> could not read price ({url})")
                    failed += 1
                elif new_price == old_price:
                    print(f"NO CHANGE  {sp.get('storeName')} | {label} : {old_price}")
                    unchanged += 1
                elif is_suspicious_change(old_price, new_price, args.max_change_pct):
                    print(
                        f"SUSPICIOUS {sp.get('storeName')} | {label} : {old_price} -> {new_price} "
                        f"(>{args.max_change_pct:.0f}% change, left unchanged, check manually) ({url})"
                    )
                    suspicious += 1
                else:
                    print(f"UPDATED    {sp.get('storeName')} | {label} : {old_price} -> {new_price}")
                    sp["price"] = new_price
                    updated += 1

                time.sleep(args.delay)
    finally:
        close_selenium_driver()

    with open(args.json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(
        f"\nDone. {updated} updated, {unchanged} unchanged, "
        f"{suspicious} suspicious (skipped), {failed} failed."
    )
    if suspicious:
        print(
            f"{suspicious} price(s) changed by more than {args.max_change_pct:.0f}% and were "
            f"left untouched — review those URLs manually, the scraper likely grabbed the "
            f"wrong element on the page."
        )


if __name__ == "__main__":
    main()