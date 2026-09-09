#!/usr/bin/env python3
"""
Price Watcher — checks product URLs for price drops and emails you when one drops.

Usage:
    python main.py                      # check all products once
    python main.py --dry-run            # check prices, never send email
    python main.py --debug-url <URL>    # dump every price candidate found on a page

Secrets live in .env (never committed). Products live in config.json.
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent

# Windows consoles default to cp1252, which cannot print Croatian text or "€"
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("price_watcher")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20


# --------------------------------------------------------------------------
# .env loading (no external dependency)
# --------------------------------------------------------------------------

def load_dotenv(path: Path):
    """Minimal .env parser: KEY=value lines, # comments, optional quotes."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables win over .env
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Price parsing
# --------------------------------------------------------------------------

def parse_price(raw):
    """Convert a messy price string to float. Handles EU and US separators."""
    if raw is None:
        return None
    text = str(raw).strip()
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None

    has_comma = "," in cleaned
    has_dot = "." in cleaned

    if has_comma and has_dot:
        # Whichever separator comes last is the decimal one
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        # ",dd" at the end = decimal; otherwise thousands separator
        if re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_dot:
        # A lone dot with 3 trailing digits is a thousands separator (1.234)
        if re.search(r"\.\d{3}$", cleaned) and cleaned.count(".") == 1:
            cleaned = cleaned.replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Price extraction strategies
# --------------------------------------------------------------------------

def _walk_for_price(obj):
    if isinstance(obj, dict):
        if "price" in obj and obj["price"] not in (None, ""):
            return obj["price"]
        for key in ("offers", "@graph"):
            if key in obj:
                found = _walk_for_price(obj[key])
                if found is not None:
                    return found
        for v in obj.values():
            found = _walk_for_price(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_for_price(item)
            if found is not None:
                return found
    return None


def from_jsonld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        found = _walk_for_price(data)
        if found is not None:
            return parse_price(found)
    return None


def from_meta(soup, meta_property="product:price:amount"):
    tag = soup.find("meta", attrs={"property": meta_property})
    if tag and tag.get("content"):
        return parse_price(tag["content"])
    tag = soup.find("meta", attrs={"name": meta_property})
    if tag and tag.get("content"):
        return parse_price(tag["content"])
    return None


def from_selector(soup, selector):
    if not selector:
        return None
    el = soup.select_one(selector)
    if not el:
        return None
    return parse_price(el.get_text())


def get_soup(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "hr,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def fetch_price(product):
    """
    Resolve a product's current price.

    Honors an explicit per-product 'price_source' to avoid picking up the
    wrong number on sites that publish several prices (RRP, bulk, unit).
    Order when unset: selector -> jsonld -> meta.
    """
    url = product["url"]
    selector = product.get("selector")
    source = product.get("price_source")
    soup = get_soup(url)

    if source == "selector":
        return from_selector(soup, selector), "selector"
    if source == "jsonld":
        return from_jsonld(soup), "jsonld"
    if source == "meta":
        return from_meta(soup, product.get("meta_property", "product:price:amount")), "meta"

    price = from_selector(soup, selector)
    if price is not None:
        return price, "selector"
    price = from_jsonld(soup)
    if price is not None:
        return price, "jsonld"
    price = from_meta(soup)
    if price is not None:
        return price, "meta"
    return None, None


def debug_url(url):
    """Print every price candidate a page exposes, so you can pick the right one."""
    soup = get_soup(url)
    print(f"\nPrice candidates for:\n  {url}\n")

    print(f"  JSON-LD                        : {from_jsonld(soup)}")
    for prop in (
        "product:price:amount",
        "product:sale_price:amount",
        "product:original_price:amount",
        "og:price:amount",
    ):
        print(f"  meta[{prop}]".ljust(33) + f": {from_meta(soup, prop)}")

    print("\n  Elements whose class mentions 'price':")
    seen = set()
    for el in soup.select("[class*=price i]"):
        text = " ".join(el.get_text(strip=True).split())
        if not text or len(text) > 60:
            continue
        classes = ".".join(el.get("class") or [])
        key = (classes, text)
        if key in seen:
            continue
        seen.add(key)
        print(f"    {el.name}.{classes}".ljust(52) + f" -> {text!r}  (parsed: {parse_price(text)})")

    print(
        "\nPick the one matching the price you actually want, then set "
        "'selector' and 'price_source': 'selector' in config.json.\n"
    )


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------

def send_email(subject, body):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    # Google shows App Passwords in four spaced groups; SMTP AUTH needs the bare 16 chars
    password = (os.getenv("SMTP_PASSWORD") or "").replace(" ", "") or None
    recipient = os.getenv("ALERT_TO") or user

    if not user or not password:
        log.error("SMTP_USER / SMTP_PASSWORD not set — check your .env file.")
        return False

    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = recipient

        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, [recipient], msg.as_string())
        log.info("Email sent to %s", recipient)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "SMTP login rejected. For Gmail you need an App Password "
            "(not your normal password), with 2FA enabled on the account."
        )
    except Exception as e:
        log.error("Email failed: %s", e)
    return False


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_subject(changes, failures, total):
    """Subject that says what happened, readable in a phone notification."""
    prefix = "[!] " if failures else ""
    if not changes:
        return f"{prefix}Price watcher: no changes ({total} product{'s' if total != 1 else ''})"
    if len(changes) == 1:
        name, direction, price = changes[0]
        return f"{prefix}Price watcher: {name} {direction} to {price:.2f} EUR"
    return f"{prefix}Price watcher: {len(changes)} price changes"


def main():
    parser = argparse.ArgumentParser(
        description="Check product prices and email a report on every run."
    )
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--history", default=str(BASE_DIR / "history.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the report instead of emailing; leaves history untouched")
    parser.add_argument("--debug-url", help="Dump all price candidates for a URL and exit")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    if args.debug_url:
        debug_url(args.debug_url)
        return

    config = load_json(args.config, None)
    if config is None:
        log.error("Config not found: %s", args.config)
        sys.exit(1)

    history = load_json(args.history, {})
    products = config.get("products", [])

    blocks = []   # one report block per product
    pending = {}  # url -> history entry, written only once the report is delivered
    changes = []  # (name, direction, price), drives the subject line
    failures = 0

    for product in products:
        name = product.get("name", product["url"])
        url = product["url"]

        log.info("Checking: %s", name)
        try:
            price, source = fetch_price(product)
        except requests.RequestException as e:
            log.error("  Request failed: %s", e)
            blocks.append(f"{name}\n  CHECK FAILED: {e}\n  {url}")
            failures += 1
            continue

        if price is None:
            log.warning('  No price found. Run:  python main.py --debug-url "%s"', url)
            blocks.append(
                f"{name}\n  NO PRICE FOUND - the selector has probably broken.\n"
                f'  Diagnose with: python main.py --debug-url "{url}"\n  {url}'
            )
            failures += 1
            continue

        previous = history.get(url, {}).get("price")
        log.info("  %.2f  (via %s)%s", price, source,
                 "" if previous is None else f"   previous: {previous:.2f}")

        if previous is None:
            note = f"added to the price watcher, current price {price:.2f} EUR"
        elif price == previous:
            note = f"unchanged since the last check, still {price:.2f} EUR"
        else:
            delta = price - previous
            pct = delta / previous * 100 if previous else 0.0
            direction = "rose" if delta > 0 else "dropped"
            note = (f"{direction} from {previous:.2f} to {price:.2f} EUR"
                    f" ({delta:+.2f}, {pct:+.1f}%)")
            changes.append((name, direction, price))
            log.info("  %s", note)

        blocks.append(f"{name}\n  {note}\n  {url}")
        pending[url] = {
            "name": name,
            "price": price,
            "source": source,
            "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    subject = build_subject(changes, failures, len(products))
    body = "\n\n".join(blocks)

    delivered = True
    if not products:
        log.warning("No products in config - nothing to check.")
    elif args.dry_run:
        print(f"\n--- report that would be emailed ---\nSubject: {subject}\n\n{body}\n")
    else:
        delivered = send_email(subject, body)
        if not delivered:
            failures += 1
            log.error("Report undelivered - keeping the old prices so changes retry next run.")

    # Advance baselines only once the report is actually out, or a change is lost
    if delivered and not args.dry_run:
        history.update(pending)
    save_json(args.history, history)

    if failures:
        log.error("%d issue(s) this run - see above.", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
