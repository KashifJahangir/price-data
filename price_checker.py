"""
Unified price & availability checker for products.json
--------------------------------------------------------------------------
Reads products.json, visits every store URL, and updates both price and
availability.

CHANGELOG (this version):
- Detects Cloudflare / bot-protection challenge pages and auto-falls back
  to headless Chrome for WooCommerce stores (fixes Zah Computers & AMD House).
- Falls back to Selenium on WooCommerce when no price is extracted,
  not just on HTTP 403.
- Removed 'br' from Accept-Encoding to avoid brotli decoding issues.
- Relaxed schema.org @type matching so variants like ["Product","Thing"]
  are accepted.
- Better debug prints so you can see what the scraper is doing.
- Re-checks for Cloudflare challenge pages AFTER the Selenium load too
  (previously only checked on the requests path), with an extended wait
  and retry, plus a debug HTML dump when still blocked.
- Switched the headless browser from plain Selenium to undetected-chromedriver,
  which patches over the CDP/automation fingerprints (navigator.webdriver,
  missing chrome runtime object, automation-flagged headers, etc.) that
  Cloudflare and similar bot walls check for. Plain Selenium's masking
  (disable-blink-features, webdriver property override) wasn't enough for
  some sites (e.g. Zah Computers); undetected-chromedriver goes further.

Install deps:
    pip install requests beautifulsoup4 undetected-chromedriver

Note: webdriver-manager/plain selenium are no longer required -- 
undetected-chromedriver manages its own patched chromedriver binary.

Usage:
    py price_checker.py products.json
    py price_checker.py products.json --delay 2
    py price_checker.py products.json --debug             # verbose, step-by-step extractor trace
    py price_checker.py products.json --debug --debug-store "Zah Computers"   # only trace one store
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

# Lazily-created Selenium driver
_selenium_driver = None
_warmed_up_domains = set()

# --- Debug mode -------------------------------------------------------
# Set by main() from --debug / --debug-store. When on, get_price_and_
# availability() prints which extractor found (or didn't find) a price
# at each stage, and saves the raw HTML it worked from to debug_html/
# so you can open it and see exactly what the scraper saw.
DEBUG = False
DEBUG_STORE = None  # if set, only trace this store name (case-insensitive)


def debug_log(msg):
    if DEBUG:
        print(f"    [debug] {msg}", file=sys.stderr)


def debug_save_html(html, store_name, label):
    if not DEBUG:
        return
    import os
    os.makedirs("debug_html", exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{store_name}_{label}")[:120]
    path = os.path.join("debug_html", f"{safe}.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        debug_log(f"saved raw HTML -> {path}")
    except Exception as e:
        debug_log(f"could not save debug HTML: {e}")


def should_trace(store_name):
    if not DEBUG:
        return False
    if DEBUG_STORE is None:
        return True
    return (store_name or "").strip().lower() == DEBUG_STORE.strip().lower()


def _detect_installed_chrome_major_version():
    """Best-effort detection of the installed Chrome's major version number,
    so we can pass it explicitly to undetected-chromedriver as version_main.
    Without this, uc's own auto-detect (version_main=None) can sometimes
    grab the newest available driver instead of one matching the Chrome
    actually installed on this machine, causing a
    'This version of ChromeDriver only supports Chrome version X' error.
    Returns an int, or None if detection fails (caller falls back to
    version_main=None in that case)."""
    import subprocess
    import re as _re

    candidates = []
    if sys.platform.startswith("win"):
        # Try the registry first (works even with Chrome not on PATH)
        try:
            import winreg
            for hive, path in [
                (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
            ]:
                try:
                    key = winreg.OpenKey(hive, path)
                    version, _ = winreg.QueryValueEx(key, "version")
                    candidates.append(version)
                except OSError:
                    continue
        except Exception:
            pass
        # Fallback: ask the exe directly via common install paths
        for exe in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            try:
                out = subprocess.check_output(
                    ["wmic", "datafile", "where", f"name='{exe}'".replace("\\", "\\\\"),
                     "get", "Version", "/value"],
                    stderr=subprocess.DEVNULL, timeout=10,
                ).decode(errors="ignore")
                m = _re.search(r"Version=(\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    candidates.append(m.group(1))
            except Exception:
                continue
    else:
        for cmd in [["google-chrome", "--version"], ["chromium-browser", "--version"],
                    ["chromium", "--version"]]:
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10).decode(errors="ignore")
                candidates.append(out)
            except Exception:
                continue

    for text in candidates:
        m = _re.search(r"(\d+)\.\d+\.\d+\.\d+", text)
        if m:
            return int(m.group(1))
    return None


def get_selenium_driver():
    global _selenium_driver
    if _selenium_driver is None:
        import undetected_chromedriver as uc

        detected_major = _detect_installed_chrome_major_version()
        if detected_major:
            print(f"    [selenium] launching undetected-chromedriver, pinned to "
                  f"detected Chrome major version {detected_major}...", file=sys.stderr)
        else:
            print("    [selenium] launching undetected-chromedriver (could not "
                  "detect installed Chrome version, letting uc auto-detect -- "
                  "if this fails with a version mismatch, update Chrome to the "
                  "latest release)...", file=sys.stderr)

        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,2000")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")

        # Pin version_main to the installed Chrome's actual major version
        # when we can detect it, instead of letting uc guess (which can
        # grab a driver newer than what's actually installed).
        _selenium_driver = uc.Chrome(options=options, version_main=detected_major)
        _selenium_driver.set_page_load_timeout(30)

        print("    [selenium] browser ready", file=sys.stderr)
    return _selenium_driver


def close_selenium_driver():
    global _selenium_driver
    if _selenium_driver is not None:
        _selenium_driver.quit()
        _selenium_driver = None


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

def extract_structured_price(html):
    soup = BeautifulSoup(html, "html.parser")
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
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        cleaned = clean_price(str(price))
                        if cleaned:
                            return cleaned
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
    from selenium.common.exceptions import TimeoutException
    driver = get_selenium_driver()
    try:
        driver.get(url)
    except TimeoutException:
        print(f"    [warning] page load timed out after 30s, trying partial content: {url}",
              file=sys.stderr)
    time.sleep(2.5)
    html = driver.page_source
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
    # "Rs" is common, but several stores (e.g. Zah Computers) render prices
    # with the Unicode Rupee sign (U+20A8, "₨") instead, which this pattern
    # used to miss entirely -- silently returning None on every such page.
    matches = re.findall(r"(?:Rs\.?|\u20a8)\s?[\d,]{4,}(?:\.\d{1,2})?", html)
    if matches:
        return clean_price(matches[0])
    return None


def extract_broad_price(html):
    """Last-resort extractor for themes that don't use WooCommerce's
    default markup (custom Elementor/product-builder themes, etc.).
    Scans any element whose class contains 'price' -- much looser than
    the specific selectors above, so it's tried only after everything
    else has failed."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("[class*='price' i]"):
        text = el.get_text(strip=True)
        if not text:
            continue
        price = clean_price(text)
        if price:
            return price
    return None


# --------------------------------------------------------------------------
# WooCommerce via Selenium fallback
# --------------------------------------------------------------------------

def run_price_extractors(html, store_name="", label=""):
    """Try each price extractor in order, logging (in debug mode) which
    one succeeded or that all of them missed. Centralizes what used to be
    a repeated if-price-is-None chain in three different places."""
    stages = [
        ("structured (JSON-LD/meta)", lambda h: extract_structured_price(h)),
        ("woocommerce (.price-wrapper etc.)", lambda h: extract_woocommerce_price(h)),
        ("generic woocommerce selectors", lambda h: extract_generic_woocommerce(h)),
        ("regex (Rs / \u20a8 symbol)", lambda h: extract_generic_regex(h)),
        ("broad ([class*=price])", lambda h: extract_broad_price(h)),
    ]
    for stage_name, fn in stages:
        price = fn(html)
        if price is not None:
            if should_trace(store_name):
                debug_log(f"{store_name} | {label}: price found at stage "
                          f"'{stage_name}' -> {price}")
            return price
        elif should_trace(store_name):
            debug_log(f"{store_name} | {label}: stage '{stage_name}' found nothing")
    if should_trace(store_name):
        debug_log(f"{store_name} | {label}: ALL price stages returned nothing")
    return None


def fetch_woocommerce_via_selenium(url, store_name="", label=""):
    from selenium.common.exceptions import TimeoutException
    driver = get_selenium_driver()
    try:
        driver.get(url)
    except TimeoutException:
        print(f"    [warning] selenium page load timed out after 30s, trying "
              f"partial content: {url}", file=sys.stderr)
    time.sleep(2.5)
    html = driver.page_source

    # If we're still looking at a Cloudflare/bot-wall interstitial after the
    # initial load, give the JS challenge more time to resolve and re-check
    # once before giving up. Previously this function never re-checked for
    # the challenge page after Selenium loaded, so it would silently try
    # (and fail) to extract a price from the interstitial itself.
    if is_cloudflare_challenge(html):
        print(f"    [woocommerce] still on challenge page after initial wait, "
              f"waiting longer for {url}", file=sys.stderr)
        debug_log(f"{store_name} | {label}: Cloudflare/bot-wall challenge detected "
                  f"after selenium load, waiting 8s and re-checking")
        time.sleep(8)
        html = driver.page_source
        if is_cloudflare_challenge(html):
            print(f"    [woocommerce] still blocked after extended wait -- "
                  f"dumping debug HTML for {url}", file=sys.stderr)
            try:
                with open("debug_last_challenge.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass
            debug_log(f"{store_name} | {label}: still challenged after 8s wait, giving up")
            return None, "unknown"
        else:
            debug_log(f"{store_name} | {label}: challenge cleared after extended wait")

    debug_save_html(html, store_name, label + "_selenium")

    price = run_price_extractors(html, store_name, label)

    availability = extract_woocommerce_availability(html)
    if availability == "unknown":
        availability = extract_generic_availability(html)

    # Dump the HTML whenever we still couldn't find a price, even though the
    # page loaded (not a challenge/block) -- most common cause is a theme
    # using markup none of the selectors above expect. Having this on hand
    # is what let us find the Zah "Rs" (Unicode Rupee sign) bug.
    if price is None:
        try:
            with open("debug_no_price.html", "w", encoding="utf-8") as f:
                f.write(html)
            print(f"    [woocommerce] no price found by any extractor -- "
                  f"dumped HTML to debug_no_price.html for {url}", file=sys.stderr)
        except Exception:
            pass

    return price, availability


# --------------------------------------------------------------------------
# Unified fetch: returns (price, availability)
# --------------------------------------------------------------------------

WOOCOMMERCE_DOMAINS = ["amdhouse.pk", "zahcomputers.pk", "zicomputer.com", "rbtechngames.com"]
WEBX_DOMAINS = ["junaidtech.pk", "czone.com.pk"]


def get_price_and_availability(session, url, store_name="", label=""):
    domain = urlparse(url).netloc.replace("www.", "")
    price = None
    availability = "unknown"

    # --- WooCommerce stores
    if any(d in domain for d in WOOCOMMERCE_DOMAINS):
        try:
            warm_up_domain(session, url)
            resp = fetch_with_retries(session, url)
            html = resp.text
            debug_log(f"{store_name} | {label}: requests fetch OK, "
                      f"{len(html)} chars, status {resp.status_code}")

            # If we got a bot challenge page, skip straight to Selenium
            if is_cloudflare_challenge(html):
                print(f"    [woocommerce] bot challenge detected, using headless browser for {url}",
                      file=sys.stderr)
                debug_log(f"{store_name} | {label}: challenge detected on direct "
                          f"fetch, going straight to selenium")
                return fetch_woocommerce_via_selenium(url, store_name, label)

            debug_save_html(html, store_name, label + "_requests")

            # Price + availability
            price = run_price_extractors(html, store_name, label)
            availability = extract_woocommerce_availability(html)
            if availability == "unknown":
                availability = extract_generic_availability(html)

            # If we still have no price, the page likely rendered but uses JS
            # or unusual markup -- fall back to Selenium as a last resort.
            if price is None:
                print(f"    [woocommerce] no price extracted, trying headless browser for {url}",
                      file=sys.stderr)
                return fetch_woocommerce_via_selenium(url, store_name, label)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 403:
                print(f"    [woocommerce] 403 from requests, falling back to "
                      f"headless browser for {url}", file=sys.stderr)
                debug_log(f"{store_name} | {label}: got HTTP 403 on direct fetch")
                try:
                    return fetch_woocommerce_via_selenium(url, store_name, label)
                except Exception as e2:
                    print(f"    [woocommerce selenium fallback failed] {e2}", file=sys.stderr)
                    debug_log(f"{store_name} | {label}: selenium fallback raised: {e2}")
                    return None, "unknown"
            else:
                print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
                debug_log(f"{store_name} | {label}: HTTPError status={status}: {e}")
                return None, "unknown"
        except Exception as e:
            print(f"    [woocommerce fetch failed] {e}", file=sys.stderr)
            debug_log(f"{store_name} | {label}: unexpected exception: {e}")
            return None, "unknown"

    # --- Webx stores (Selenium)
    elif any(d in domain for d in WEBX_DOMAINS):
        try:
            price = extract_webx_price(url)
            if price is None:
                driver = get_selenium_driver()
                price = extract_generic_regex(driver.page_source)
                if price is None:
                    price = extract_broad_price(driver.page_source)

            driver = get_selenium_driver()
            debug_save_html(driver.page_source, store_name, label + "_webx")
            availability = extract_webx_availability(driver.page_source)

        except Exception as e:
            print(f"    [webx fetch failed] {e}", file=sys.stderr)
            debug_log(f"{store_name} | {label}: webx exception: {e}")
            return None, "unknown"

    # --- Unknown domain
    else:
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            html = resp.text
            debug_save_html(html, store_name, label + "_unknown")
            price = extract_generic_woocommerce(html)
            if price is None:
                price = extract_generic_regex(html)
            if price is None:
                price = extract_broad_price(html)
            availability = extract_generic_availability(html)
        except Exception as e:
            print(f"    [unknown-domain fetch failed] {e}", file=sys.stderr)
            debug_log(f"{store_name} | {label}: unknown-domain exception: {e}")
            return None, "unknown"

    return price, availability


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    global DEBUG, DEBUG_STORE

    parser = argparse.ArgumentParser(description="Check and update prices & availability in products.json")
    parser.add_argument("json_file", help="Path to products.json")
    parser.add_argument("--delay", type=float, default=2, help="Seconds between requests")
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose step-by-step trace: prints which extractor stage found "
             "(or missed) a price for each URL, and saves every fetched page's "
             "raw HTML under debug_html/ so you can inspect it yourself.",
    )
    parser.add_argument(
        "--debug-store",
        type=str,
        default=None,
        help="With --debug, only trace this store name (matches sp['storeName'], "
             "case-insensitive) instead of every store -- much less noisy when "
             "you already know which store is failing, e.g. --debug-store \"Zah Computers\"",
    )
    args = parser.parse_args()

    DEBUG = args.debug
    DEBUG_STORE = args.debug_store
    if DEBUG:
        print(f"[debug] debug mode ON"
              f"{f' (only tracing store: {DEBUG_STORE})' if DEBUG_STORE else ''}"
              f" -- raw HTML will be saved under ./debug_html/", file=sys.stderr)

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

                store_name = sp.get("storeName") or ""
                label = sp.get("Name") or sp.get("name") or product.get("name")
                print(f"Checking {sp.get('storeName')} | {label} ...", flush=True)

                new_price, availability = get_price_and_availability(
                    session, url, store_name=store_name, label=label
                )
                old_price = sp.get("price")

                # Always record the latest availability
                sp["availability"] = availability

                if new_price is None:
                    print(f"FAILED     {sp.get('storeName')} | {label} -> could not read price "
                          f"[availability: {availability}] ({url})")
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
                    print(f"UPDATED    {sp.get('storeName')} | {label} : {old_price} -> {new_price} "
                          f"[availability: {availability}]")
                    sp["price"] = new_price
                    updated += 1

                time.sleep(args.delay)
    finally:
        close_selenium_driver()

    with open(args.json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Availability summary
    avail_stats = {}
    for product in data["products"]:
        for sp in product["storePrices"]:
            a = sp.get("availability", "unknown")
            avail_stats[a] = avail_stats.get(a, 0) + 1

    print(
        f"\nDone. {updated} updated, {unchanged} unchanged, "
        f"{suspicious} suspicious (skipped), {failed} failed."
    )
    print(
        f"Availability summary: "
        f"{avail_stats.get('available', 0)} available | "
        f"{avail_stats.get('not available', 0)} not available | "
        f"{avail_stats.get('unknown', 0)} unknown."
    )
    if suspicious:
        print(
            f"{suspicious} price(s) changed by more than {args.max_change_pct:.0f}% and were "
            f"left untouched — review those URLs manually, the scraper likely grabbed the "
            f"wrong element on the page."
        )


if __name__ == "__main__":
    main()
