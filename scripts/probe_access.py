#!/usr/bin/env python3
"""Phase 0 probe: is this vantage point served by the target shops?

Standard library only, so it runs on a fresh cloud box with nothing
installed. Sends one honest browser-like request per shop and classifies
the response. It does not try to get around a block - if the answer is
403, the answer is 403.

Usage:
    python3 probe_access.py                 # probe the default shops
    python3 probe_access.py --label hetzner # tag the output with where it ran

Exit code: 0 if every shop is served, 1 if any is not, 2 on a probe fault.
"""
import argparse
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HOST_GAP_SECONDS = 5.0
TIMEOUT_SECONDS = 25

CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-challenge",
    "just a moment",
    "checking your browser",
    "captcha",
    "cf-browser-verification",
)

# (url, marker that proves we got the real product page)
DEFAULT_TARGETS: tuple[tuple[str, str], ...] = (
    ("https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769",
     "product-main-price-display"),
    ("https://tennis-zone.hr/padel/reketi/adidas/adidas-metalbone-2026/ST107802",
     "product-main-price-display"),
)


def classify(status: int, body: str, expect_marker: str) -> str:
    """Turn a response into a verdict. A 200 is not automatically success."""
    if status in (401, 403, 429):
        return "blocked"
    if status >= 500:
        return "error"
    if status != 200:
        return "error"
    # The price element is the shop's own DOM; an interstitial never carries
    # it. Check it first: real pages legitimately mention "captcha" in their
    # own scripts (observed on tennis-zone.hr, a Shopware 6 shop).
    if expect_marker in body:
        return "served"
    lowered = body.lower()
    if any(m.lower() in lowered for m in CHALLENGE_MARKERS):
        return "challenge"
    return "not_product_page"


def fetch(url: str) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "hr,en;q=0.9",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ctx) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)


def public_ip() -> str:
    """Best-effort: what IP does the outside world see us as?"""
    try:
        req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode().strip()
    except Exception:
        return "unknown"


def probe(targets, label: str) -> list[dict]:
    results = []
    ip = public_ip()
    last_hit: dict[str, float] = {}
    for url, marker in targets:
        host = url.split("/")[2]
        if host in last_hit:
            wait = HOST_GAP_SECONDS - (time.monotonic() - last_hit[host])
            if wait > 0:
                time.sleep(wait)
        record = {
            "label": label,
            "ip": ip,
            "url": url,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            status, body, headers = fetch(url)
            last_hit[host] = time.monotonic()
            record.update({
                "status": status,
                "verdict": classify(status, body, marker),
                "server": headers.get("Server") or headers.get("server"),
                "cf_ray": headers.get("CF-RAY") or headers.get("cf-ray"),
                "cf_mitigated": headers.get("cf-mitigated"),
                "body_len": len(body),
            })
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last_hit[host] = time.monotonic()
            record.update({"status": None, "verdict": "error", "error": str(e)})
        results.append(record)
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--label", default=socket.gethostname(),
                   help="where this ran, e.g. home, hetzner-fsn1, fly-ams")
    p.add_argument("--url", action="append", metavar="URL",
                   help="probe this URL instead of the defaults (repeatable); "
                        "marker defaults to product-main-price-display")
    args = p.parse_args(argv)

    targets = DEFAULT_TARGETS
    if args.url:
        targets = tuple((u, "product-main-price-display") for u in args.url)

    try:
        results = probe(targets, args.label)
    except Exception as e:
        print(json.dumps({"fault": str(e)}), file=sys.stderr)
        return 2

    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    verdicts = {r["verdict"] for r in results}
    print(f"\n{args.label}: {'ALL SERVED' if verdicts == {'served'} else 'NOT SERVED: ' + ', '.join(sorted(verdicts - {'served'}))}",
          file=sys.stderr)
    return 0 if verdicts == {"served"} else 1


if __name__ == "__main__":
    sys.exit(main())
