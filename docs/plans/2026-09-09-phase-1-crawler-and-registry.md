# Phase 1: Crawler Library and Domain Registry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adding a product URL and confirming its price works end to end from a command-line harness, with the confirmed selector persisted per shop in Postgres.

**Architecture:** The existing single-file CLI becomes a `pricewatcher` package. Pure functions (parsing, extraction, ranking, canonicalization) are separated from I/O (HTTP, Playwright, Postgres) so the price logic is testable offline against saved real pages. A thin CLI harness wires them together; no web layer and no auth in this phase.

**Tech Stack:** Python 3.12, `requests`, `beautifulsoup4`, `lxml`, `playwright`, SQLAlchemy 2.0, Alembic, `psycopg[binary]`, `pytest`, Docker Compose for Postgres.

**Spec:** `docs/specs/2026-09-09-price-watcher-saas-design.md` — read §4 (data model), §5 (adding a product), §6 (ranking) and §10 (evidence) before starting. §10 is where the expected numbers in the tests come from.

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include these.

- **Politeness:** at least **5 seconds** between requests to the same host; at most **4 concurrent fetches** across all hosts; `robots.txt` honoured per domain.
- **Never silently fall back.** A shop with a pinned `price_source` that stops matching reports failure; it does not substitute a number from another source.
- **`price_source`** is one of `'selector'`, `'jsonld'`, `'meta'`.
- **`shops.distrusted`** is a JSON array of source identifier strings, e.g. `["product:price:amount"]`.
- **Currency and decimal style are per shop**, never global. `decimal_style` is `'comma'` or `'dot'`.
- **Postgres requires the `citext` extension** (used by `users.email`).
- **Default timezone** is `'Europe/Zagreb'`.
- **Trap rule:** if a bulk/tiered price block is present in the DOM, every `product:*` meta price is rejected — *even when that block renders no visible rows*.

### Phase 1 boundary

The migration creates all six tables from spec §4, but Phase 1 only *exercises* `shops`, `products`, `price_points` and `checks`. `users` and `watches` exist in the schema and are used from Phase 2 onward, when auth arrives. Do not build user handling in this phase.

---

## File Structure

| Path | Responsibility |
|---|---|
| `pricewatcher/parsing.py` | `parse_price` only. Ported verbatim from `main.py`. |
| `pricewatcher/canonical.py` | URL canonicalization and domain extraction. |
| `pricewatcher/extract.py` | The three extraction strategies + candidate collection. |
| `pricewatcher/ranking.py` | Bulk-block detection, candidate ranking, distrusted-source list. |
| `pricewatcher/platforms.py` | Platform fingerprinting (Shopware 6 for now). |
| `pricewatcher/fetch.py` | HTTP fetching with per-host politeness; Playwright rendering. |
| `pricewatcher/db/models.py` | SQLAlchemy models for the six tables. |
| `pricewatcher/db/session.py` | Engine and session factory. |
| `pricewatcher/registry.py` | Shop lookup, confirmation, flagging. |
| `pricewatcher/products.py` | `add_product` / `confirm_and_add` orchestration. |
| `pricewatcher/cli.py` | Test harness: `debug`, `add`, `confirm`, `check`. |
| `tests/fixtures/*.html` | Saved real product pages, so trap tests run offline. |
| `docker-compose.yml` | Postgres for local dev and tests. |
| `alembic/` | Migrations. |

`main.py` stays untouched and working throughout this phase. It is deleted in Phase 3, once the service replaces it.

---

## Task 1: Package scaffolding and `parse_price`

**Files:**
- Create: `pyproject.toml`
- Create: `pricewatcher/__init__.py`
- Create: `pricewatcher/parsing.py`
- Create: `tests/__init__.py`
- Test: `tests/test_parsing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_price(raw: str | float | None) -> float | None`

- [ ] **Step 1: Write the failing test**

`tests/test_parsing.py`:

```python
import pytest

from pricewatcher.parsing import parse_price


@pytest.mark.parametrize("raw,expected", [
    ("248,90 €", 248.90),
    ("€1.234,56", 1234.56),
    ("$1,234.56", 1234.56),
    ("1.234", 1234.0),
    ("79,90", 79.90),
    ("1 299,00 kn", 1299.00),
    ("Preporučena cijena: 354,00 €", 354.00),
    ("197,90 €", 197.90),
    ("275,90 €", 275.90),
])
def test_parses_eu_and_us_formats(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "no digits here", "€"])
def test_returns_none_when_no_number(raw):
    assert parse_price(raw) is None


def test_mixed_content_container_is_rejected():
    # A container holding both price and RRP must not yield a plausible number.
    assert parse_price("248,90 €Preporučena cijena:354,00 €") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parsing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher'`

- [ ] **Step 3: Write minimal implementation**

`pyproject.toml`:

```toml
[project]
name = "pricewatcher"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "requests>=2.31",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "SQLAlchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.1",
    "playwright>=1.44",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["pricewatcher*"]
```

`pricewatcher/__init__.py`: empty file.

`pricewatcher/parsing.py` — copied from `main.py` unchanged except for the type hint:

```python
"""Price string parsing. Ported verbatim from the CLI; do not 'improve' it
without adding a failing test first — every branch here exists for a real
observed format."""
import re


def parse_price(raw: str | float | None) -> float | None:
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
        if re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_dot:
        if re.search(r"\.\d{3}$", cleaned) and cleaned.count(".") == 1:
            cleaned = cleaned.replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pip install -e ".[dev]"` then `pytest tests/test_parsing.py -v`
Expected: PASS — 15 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml pricewatcher/ tests/
git commit -m "feat: package scaffolding and parse_price with verified format cases"
```

---

## Task 2: URL canonicalization

**Files:**
- Create: `pricewatcher/canonical.py`
- Test: `tests/test_canonical.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `canonicalize_url(url: str) -> str`, `domain_of(url: str) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_canonical.py`:

```python
import pytest

from pricewatcher.canonical import canonicalize_url, domain_of

HEAD = "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769"


def test_strips_tracking_params_so_one_product_is_one_row():
    dirty = HEAD + "?utm_source=newsletter&gclid=abc123&fbclid=z"
    assert canonicalize_url(dirty) == HEAD


def test_drops_fragment_and_lowercases_host():
    assert canonicalize_url("https://Tennis-Zone.HR/a/b#reviews") == \
        "https://tennis-zone.hr/a/b"


def test_strips_www():
    assert canonicalize_url("https://www.example.com/p/1") == \
        "https://example.com/p/1"


def test_keeps_meaningful_params_sorted():
    assert canonicalize_url("https://x.com/p?variant=blue&size=3") == \
        "https://x.com/p?size=3&variant=blue"


def test_removes_trailing_slash_but_keeps_root():
    assert canonicalize_url("https://x.com/p/1/") == "https://x.com/p/1"
    assert canonicalize_url("https://x.com/") == "https://x.com/"


def test_upgrades_http_to_https():
    assert canonicalize_url("http://x.com/p") == "https://x.com/p"


def test_domain_of_matches_canonical_host():
    assert domain_of(HEAD) == "tennis-zone.hr"
    assert domain_of("https://www.Example.com/x") == "example.com"


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://x.com/p"])
def test_rejects_unusable_urls(bad):
    with pytest.raises(ValueError):
        canonicalize_url(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_canonical.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.canonical'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/canonical.py`:

```python
"""URL canonicalization.

This is load-bearing, not cosmetic: without it the same product carrying a
tracking parameter becomes two rows, and the one-fetch-per-URL guarantee
breaks. Stripping query parameters also keeps requests off the filtered paths
shops commonly disallow in robots.txt.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"gclid", "fbclid", "msclkid", "ref", "source", "mc_cid", "mc_eid"}


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_KEYS or k.startswith(TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https", ""):
        raise ValueError(f"unsupported scheme: {parts.scheme!r}")
    if not parts.netloc:
        raise ValueError(f"not an absolute URL: {url!r}")

    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(k)]
    query = urlencode(sorted(kept))

    return urlunsplit(("https", host, path, query, ""))


def domain_of(url: str) -> str:
    return urlsplit(canonicalize_url(url)).netloc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_canonical.py -v`
Expected: PASS — 11 passed

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/canonical.py tests/test_canonical.py
git commit -m "feat: URL canonicalization so one product is one row"
```

---

## Task 3: Capture real pages as test fixtures

Offline fixtures make the trap tests deterministic. Without them every test run hits a live shop, which is both slow and rude.

**Files:**
- Create: `scripts/capture_fixtures.py`
- Create: `tests/fixtures/tennis_zone_head.html` (generated)
- Create: `tests/fixtures/tennis_zone_adidas.html` (generated)
- Create: `tests/fixtures/no_bulk_block.html` (hand-written)
- Test: `tests/test_fixtures_present.py`

**Interfaces:**
- Consumes: nothing.
- Produces: fixture files, and `tests/conftest.py` helper `load_fixture(name: str) -> str` (created in Step 3).

- [ ] **Step 1: Write the failing test**

`tests/test_fixtures_present.py`:

```python
from tests.conftest import load_fixture


def test_head_fixture_has_the_four_price_trap():
    html = load_fixture("tennis_zone_head.html")
    assert "product-main-price-display" in html
    assert "223.90" in html or "223,90" in html   # the bulk price
    assert "248,90" in html                        # the real price


def test_adidas_fixture_has_header_only_bulk_table():
    html = load_fixture("tennis_zone_adidas.html")
    assert "product-block-prices" in html
    assert "275,90" in html


def test_no_bulk_fixture_has_no_bulk_block():
    html = load_fixture("no_bulk_block.html")
    assert "product-block-prices" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fixtures_present.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.conftest'` or `FileNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`tests/conftest.py`:

```python
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
```

`scripts/capture_fixtures.py`:

```python
"""Re-capture live pages as test fixtures. Run manually, rarely.

Rate-limited to one request per 5 seconds per the politeness constraint.
"""
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = {
    "tennis_zone_head.html":
        "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769",
    "tennis_zone_adidas.html":
        "https://tennis-zone.hr/padel/reketi/adidas/adidas-metalbone-2026/ST107802",
}

OUT.mkdir(parents=True, exist_ok=True)
for i, (name, url) in enumerate(PAGES.items()):
    if i:
        time.sleep(5)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    (OUT / name).write_text(r.text, encoding="utf-8")
    print(f"wrote {name}  ({len(r.text)} chars)  HTTP {r.status_code}")
```

`tests/fixtures/no_bulk_block.html` — hand-written, the third evidence case
(no bulk block, so the meta price is trustworthy):

```html
<!DOCTYPE html>
<html lang="hr-HR"><head>
<meta property="product:price:amount" content="197.90">
<meta property="product:original_price:amount" content="295.00">
</head><body>
<div class="product-detail-price-container">
  <div class="product-main-price-display">
    <p class="product-detail-price">197,90 €</p>
  </div>
</div>
</body></html>
```

- [ ] **Step 4: Generate fixtures and run tests**

Run: `python scripts/capture_fixtures.py` then `pytest tests/test_fixtures_present.py -v`
Expected: script prints two `wrote ...` lines with HTTP 200; tests PASS — 3 passed

If the capture returns HTTP 403, you are on an IP the shop refuses — see spec §9. Do not add headers to work around it; note it and continue with the hand-written fixture only, marking the two live tests `pytest.mark.skip`.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_fixtures.py tests/conftest.py tests/fixtures/ tests/test_fixtures_present.py
git commit -m "test: capture real product pages as offline fixtures"
```

---

## Task 4: Extraction strategies

**Files:**
- Create: `pricewatcher/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `parse_price` from Task 1, `load_fixture` from Task 3.
- Produces:
  - `soup_of(html: str) -> BeautifulSoup`
  - `from_selector(soup, selector: str) -> float | None`
  - `from_jsonld(soup) -> float | None`
  - `from_meta(soup, meta_property: str = "product:price:amount") -> float | None`
  - `Candidate` dataclass with fields `source: str`, `kind: str`, `selector: str | None`, `raw_text: str`, `price: float | None`, `match_count: int`
  - `collect_candidates(html: str) -> list[Candidate]`

- [ ] **Step 1: Write the failing test**

`tests/test_extract.py`:

```python
from pricewatcher.extract import (
    Candidate, collect_candidates, from_jsonld, from_meta, from_selector, soup_of,
)
from tests.conftest import load_fixture


def test_selector_finds_the_real_single_unit_price():
    soup = soup_of(load_fixture("tennis_zone_head.html"))
    assert from_selector(soup, "div.product-main-price-display") == 248.90


def test_meta_reports_the_wrong_number_on_the_head_page():
    soup = soup_of(load_fixture("tennis_zone_head.html"))
    assert from_meta(soup, "product:price:amount") == 223.90
    assert from_meta(soup, "product:sale_price:amount") == 259.40
    assert from_meta(soup, "product:original_price:amount") == 354.00


def test_head_page_has_no_jsonld_price_at_all():
    soup = soup_of(load_fixture("tennis_zone_head.html"))
    assert from_jsonld(soup) is None


def test_missing_selector_returns_none_never_guesses():
    soup = soup_of(load_fixture("tennis_zone_head.html"))
    assert from_selector(soup, "div.class-that-does-not-exist") is None


def test_collect_candidates_records_match_count_for_uniqueness():
    cands = collect_candidates(load_fixture("tennis_zone_head.html"))
    by_sel = {c.selector: c for c in cands if c.kind == "selector"}
    assert by_sel["div.product-main-price-display"].match_count == 1
    # the sticky-header duplicate
    assert by_sel["p.product-detail-price"].match_count == 2


def test_collect_candidates_includes_meta_and_marks_kind():
    cands = collect_candidates(load_fixture("tennis_zone_head.html"))
    metas = {c.source: c.price for c in cands if c.kind == "meta"}
    assert metas["product:price:amount"] == 223.90
    assert all(isinstance(c, Candidate) for c in cands)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.extract'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/extract.py`:

```python
"""The three extraction strategies, plus candidate collection.

Ported from the CLI's fetch_price/debug_url. The candidate collector is what
Layer 1 discovery ranks; it must report match_count, because uniqueness in the
DOM is a ranking signal.
"""
import json
from dataclasses import dataclass

from bs4 import BeautifulSoup

from pricewatcher.parsing import parse_price

META_PROPERTIES = (
    "product:price:amount",
    "product:sale_price:amount",
    "product:original_price:amount",
    "og:price:amount",
)


@dataclass(frozen=True)
class Candidate:
    source: str
    kind: str            # 'selector' | 'jsonld' | 'meta'
    selector: str | None
    raw_text: str
    price: float | None
    match_count: int


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


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


def from_jsonld(soup) -> float | None:
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


def from_meta(soup, meta_property: str = "product:price:amount") -> float | None:
    for attrs in ({"property": meta_property}, {"name": meta_property}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return parse_price(tag["content"])
    return None


def from_selector(soup, selector: str) -> float | None:
    if not selector:
        return None
    el = soup.select_one(selector)
    if not el:
        return None
    return parse_price(el.get_text())


def _text_of(el) -> str:
    return " ".join(el.get_text(strip=True).split())


def collect_candidates(html: str) -> list[Candidate]:
    soup = soup_of(html)
    out: list[Candidate] = []

    jsonld = from_jsonld(soup)
    if jsonld is not None:
        out.append(Candidate("jsonld", "jsonld", None, str(jsonld), jsonld, 1))

    for prop in META_PROPERTIES:
        price = from_meta(soup, prop)
        if price is not None:
            out.append(Candidate(prop, "meta", None, str(price), price, 1))

    seen: set[str] = set()
    for el in soup.select('[class*="price"]'):
        classes = el.get("class") or []
        if not classes:
            continue
        selector = f"{el.name}.{'.'.join(classes)}"
        if selector in seen:
            continue
        seen.add(selector)
        text = _text_of(el)
        if not text or len(text) > 60:
            continue
        out.append(Candidate(
            source=f"selector:{selector}",
            kind="selector",
            selector=selector,
            raw_text=text,
            price=parse_price(text),
            match_count=len(soup.select(selector)),
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extract.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/extract.py tests/test_extract.py
git commit -m "feat: extraction strategies and candidate collection"
```

---

## Task 5: Platform fingerprinting

**Files:**
- Create: `pricewatcher/platforms.py`
- Test: `tests/test_platforms.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `detect_platform(html: str, robots_txt: str = "") -> str | None`

- [ ] **Step 1: Write the failing test**

`tests/test_platforms.py`:

```python
from pricewatcher.platforms import detect_platform
from tests.conftest import load_fixture


def test_detects_shopware_from_storefront_markers():
    assert detect_platform(load_fixture("tennis_zone_head.html")) == "shopware6"


def test_detects_shopware_from_robots_txt():
    robots = "# Robots.txt for Shopware 6 E-commerce Store\nUser-agent: *\nAllow: /"
    assert detect_platform("<html></html>", robots) == "shopware6"


def test_returns_none_for_unknown_platform():
    assert detect_platform("<html><body>plain</body></html>") is None


def test_detection_is_case_insensitive():
    assert detect_platform("<script>window.ACTIVENAVIGATIONID=1</script>") == "shopware6"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_platforms.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.platforms'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/platforms.py`:

```python
"""Platform fingerprinting.

Note what this is NOT for: platform does not imply a selector. That hypothesis
was tested and refuted (spec §10) — Shopware themes restructure price markup
freely. Platform carries trap rules, such as which meta fields lie.
"""

FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "shopware6": (
        "/bundles/storefront/",
        "window.activenavigationid",
        "data-cms-page-id",
        "shopware",
    ),
}


def detect_platform(html: str, robots_txt: str = "") -> str | None:
    haystack = f"{html}\n{robots_txt}".lower()
    for platform, markers in FINGERPRINTS.items():
        if any(m.lower() in haystack for m in markers):
            return platform
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_platforms.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/platforms.py tests/test_platforms.py
git commit -m "feat: platform fingerprinting for trap rules"
```

---

## Task 6: Candidate ranking and the trap rule

This is the heart of Layer 1. All expected values come from spec §10.

**Files:**
- Create: `pricewatcher/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: `Candidate`, `collect_candidates`, `soup_of` from Task 4.
- Produces:
  - `has_bulk_price_block(html: str) -> bool`
  - `distrusted_sources(html: str) -> list[str]`
  - `rank_candidates(html: str) -> list[Candidate]` — best first
  - `BULK_SELECTORS: tuple[str, ...]`
  - `CONDITIONAL_CLASSES: frozenset[str]`

- [ ] **Step 1: Write the failing test**

`tests/test_ranking.py`:

```python
from pricewatcher.ranking import (
    distrusted_sources, has_bulk_price_block, rank_candidates,
)
from tests.conftest import load_fixture

HEAD = "tennis_zone_head.html"
ADIDAS = "tennis_zone_adidas.html"
NO_BULK = "no_bulk_block.html"


def test_bulk_block_detected_when_it_has_rows():
    assert has_bulk_price_block(load_fixture(HEAD)) is True


def test_bulk_block_detected_even_when_it_renders_no_rows():
    # The Adidas page renders the table header-only, yet the meta price is
    # still a tier price (248.40 against a real 275.90). Presence is the signal.
    assert has_bulk_price_block(load_fixture(ADIDAS)) is True


def test_no_bulk_block_when_absent():
    assert has_bulk_price_block(load_fixture(NO_BULK)) is False


def test_meta_sources_are_distrusted_when_a_bulk_block_exists():
    d = distrusted_sources(load_fixture(HEAD))
    assert "product:price:amount" in d
    assert "product:original_price:amount" in d


def test_meta_sources_are_trusted_when_no_bulk_block_exists():
    assert distrusted_sources(load_fixture(NO_BULK)) == []


def test_top_candidate_on_head_page_is_the_real_price():
    best = rank_candidates(load_fixture(HEAD))[0]
    assert best.price == 248.90
    assert best.selector == "div.product-main-price-display"


def test_top_candidate_on_adidas_page_is_the_real_price():
    best = rank_candidates(load_fixture(ADIDAS))[0]
    assert best.price == 275.90


def test_bulk_price_never_ranks_first():
    ranked = rank_candidates(load_fixture(HEAD))
    assert 223.90 not in [c.price for c in ranked[:3]]


def test_duplicated_selector_ranks_below_unique_one():
    ranked = rank_candidates(load_fixture(HEAD))
    order = [c.selector for c in ranked if c.kind == "selector"]
    assert order.index("div.product-main-price-display") < \
        order.index("p.product-detail-price")


def test_conditional_class_selectors_are_demoted():
    ranked = rank_candidates(load_fixture(HEAD))
    conditional = [c for c in ranked if c.selector
                   and "with-list-price" in c.selector]
    assert all(ranked.index(c) > 0 for c in conditional)


def test_unparseable_containers_are_dropped():
    ranked = rank_candidates(load_fixture(HEAD))
    assert all(c.price is not None for c in ranked)


def test_meta_price_wins_when_no_bulk_block_and_no_selector():
    html = """<html><head>
    <meta property="product:price:amount" content="197.90"></head>
    <body></body></html>"""
    ranked = rank_candidates(html)
    assert ranked[0].price == 197.90
    assert ranked[0].kind == "meta"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ranking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.ranking'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/ranking.py`:

```python
"""Candidate ranking.

Every rule here comes from measured behaviour on real pages (spec §10), not
from first principles. Do not add a rule without a fixture that demonstrates
it, and do not relax one without checking the three evidence products still
resolve correctly.
"""
from pricewatcher.extract import Candidate, collect_candidates, soup_of

BULK_SELECTORS: tuple[str, ...] = (
    "div.product-block-prices",
    "table.product-block-prices-grid",
    "[class*='block-prices']",
)

CONDITIONAL_CLASSES: frozenset[str] = frozenset({"with-list-price"})

# Meta sources that carry a tier price when tiered pricing exists at all.
TIER_SENSITIVE_META: tuple[str, ...] = (
    "product:price:amount",
    "product:sale_price:amount",
    "product:original_price:amount",
    "og:price:amount",
)


def has_bulk_price_block(html: str) -> bool:
    """True if a quantity/tier price block exists, rows or no rows."""
    soup = soup_of(html)
    return any(soup.select_one(sel) for sel in BULK_SELECTORS)


def distrusted_sources(html: str) -> list[str]:
    """Meta sources that must be rejected on this page."""
    if not has_bulk_price_block(html):
        return []
    soup = soup_of(html)
    present = []
    for prop in TIER_SENSITIVE_META:
        if soup.find("meta", attrs={"property": prop}) or \
           soup.find("meta", attrs={"name": prop}):
            present.append(prop)
    return present


def _score(c: Candidate, distrusted: set[str]) -> tuple:
    """Sort key; lower is better. Tuple ordering encodes rule precedence."""
    untrusted = 1 if c.source in distrusted else 0
    not_unique = 0 if c.match_count == 1 else 1
    conditional = 1 if c.selector and any(
        cls in c.selector for cls in CONDITIONAL_CLASSES) else 0
    kind_rank = {"selector": 0, "jsonld": 1, "meta": 2}[c.kind]
    return (untrusted, kind_rank, not_unique, conditional, len(c.raw_text))


def rank_candidates(html: str) -> list[Candidate]:
    distrusted = set(distrusted_sources(html))
    usable = [c for c in collect_candidates(html) if c.price is not None]
    inside_bulk = _bulk_descendant_selectors(html)
    usable = [c for c in usable if c.selector not in inside_bulk]
    return sorted(usable, key=lambda c: _score(c, distrusted))


def _bulk_descendant_selectors(html: str) -> set[str]:
    """Selectors for elements living inside a bulk-price block."""
    soup = soup_of(html)
    out: set[str] = set()
    for sel in BULK_SELECTORS:
        for block in soup.select(sel):
            for el in block.find_all(True):
                classes = el.get("class") or []
                if classes:
                    out.add(f"{el.name}.{'.'.join(classes)}")
            classes = block.get("class") or []
            if classes:
                out.add(f"{block.name}.{'.'.join(classes)}")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS — 12 passed

If `test_top_candidate_on_head_page_is_the_real_price` fails because a
different unique selector outranks it, read the actual ranked list before
changing the scoring — the fixture is ground truth and the expected answer is
248.90, not whatever the code currently prefers.

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/ranking.py tests/test_ranking.py
git commit -m "feat: candidate ranking with the tiered-price trap rule"
```

---

## Task 7: Postgres, models and the initial migration

**Files:**
- Create: `docker-compose.yml`
- Create: `pricewatcher/db/__init__.py`
- Create: `pricewatcher/db/models.py`
- Create: `pricewatcher/db/session.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Base`, and models `User`, `Shop`, `Product`, `Watch`, `PricePoint`, `Check`
  - `make_engine(url: str | None = None)`, `session_scope()` context manager
  - pytest fixture `session` (defined in `tests/conftest.py`)

- [ ] **Step 1: Write the failing test**

Append to `tests/conftest.py`:

```python
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from pricewatcher.db.models import Base

TEST_DB_URL = os.getenv(
    "PRICEWATCHER_TEST_DB",
    "postgresql+psycopg://pricewatcher:pricewatcher@localhost:5433/pricewatcher_test",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, future=True)
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    """A session rolled back after each test, so tests never share state."""
    conn = engine.connect()
    tx = conn.begin()
    s = sessionmaker(bind=conn, future=True)()
    try:
        yield s
    finally:
        s.close()
        tx.rollback()
        conn.close()
```

`tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from pricewatcher.db.models import Product, Shop


def _shop(session, domain="tennis-zone.hr"):
    shop = Shop(
        domain=domain, platform="shopware6",
        selector="div.product-main-price-display", price_source="selector",
        currency="EUR", decimal_style="comma", needs_render=False,
        distrusted=["product:price:amount"], status="trusted",
        confirmed_on="https://tennis-zone.hr/x",
    )
    session.add(shop)
    session.flush()
    return shop


def test_shop_round_trips_with_jsonb_distrusted(session):
    _shop(session)
    got = session.get(Shop, "tennis-zone.hr")
    assert got.distrusted == ["product:price:amount"]
    assert got.status == "trusted"


def test_shop_defaults_are_sane(session):
    session.add(Shop(domain="x.com", currency="EUR", decimal_style="dot"))
    session.flush()
    got = session.get(Shop, "x.com")
    assert got.price_source == "selector"
    assert got.needs_render is False
    assert got.distrusted == []
    assert got.status == "trusted"
    assert got.verified_count == 1


def test_canonical_url_is_unique_so_one_url_is_one_product(session):
    _shop(session)
    url = "https://tennis-zone.hr/p/1"
    session.add(Product(canonical_url=url, domain="tennis-zone.hr"))
    session.flush()
    session.add(Product(canonical_url=url, domain="tennis-zone.hr"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_product_requires_a_known_shop(session):
    session.add(Product(canonical_url="https://nope.com/p", domain="nope.com"))
    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d db` then `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.db'`

- [ ] **Step 3: Write minimal implementation**

`docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: pricewatcher
      POSTGRES_PASSWORD: pricewatcher
      POSTGRES_DB: pricewatcher_test
    ports:
      - "5433:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```

`pricewatcher/db/__init__.py`: empty file.

`pricewatcher/db/models.py`:

```python
"""SQLAlchemy models for spec §4.

Products are global; watches are per-user. That is the decision that makes
one-fetch-per-URL a property of the schema rather than a rule to remember.
users and watches are created here but not exercised until Phase 2.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CHAR, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    cognito_sub: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    alias: Mapped[str | None] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(
        String, nullable=False, server_default="Europe/Zagreb")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class Shop(Base):
    __tablename__ = "shops"
    domain: Mapped[str] = mapped_column(String, primary_key=True)
    platform: Mapped[str | None] = mapped_column(String)
    selector: Mapped[str | None] = mapped_column(String)
    price_source: Mapped[str] = mapped_column(
        String, nullable=False, server_default="selector", default="selector")
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    decimal_style: Mapped[str] = mapped_column(String, nullable=False)
    needs_render: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False)
    distrusted: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="trusted", default="trusted")
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_on: Mapped[str | None] = mapped_column(String)
    verified_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(
        String, ForeignKey("shops.domain"), nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class Watch(Base):
    __tablename__ = "watches"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    label: Mapped[str | None] = mapped_column(String)
    last_reported_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    last_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True)


class PricePoint(Base):
    __tablename__ = "price_points"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class Check(Base):
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String)

    __table_args__ = (UniqueConstraint("id", name="uq_checks_id"),)
```

`pricewatcher/db/session.py`:

```python
import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DEFAULT_URL = "postgresql+psycopg://pricewatcher:pricewatcher@localhost:5433/pricewatcher"


def make_engine(url: str | None = None):
    return create_engine(url or os.getenv("PRICEWATCHER_DB", DEFAULT_URL), future=True)


@contextmanager
def session_scope(url: str | None = None):
    s = sessionmaker(bind=make_engine(url), future=True)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
```

`alembic.ini`:

```ini
[alembic]
script_location = alembic
sqlalchemy.url = postgresql+psycopg://pricewatcher:pricewatcher@localhost:5433/pricewatcher
```

`alembic/env.py`:

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from pricewatcher.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
```

`alembic/versions/0001_initial.py`:

```python
"""initial schema

Revision ID: 0001
"""
from alembic import op

from pricewatcher.db.models import Base

revision = "0001"
down_revision = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    Base.metadata.create_all(op.get_bind())


def downgrade():
    Base.metadata.drop_all(op.get_bind())
    op.execute("DROP EXTENSION IF EXISTS citext")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose up -d db` then `pytest tests/test_models.py -v`
Expected: PASS — 4 passed

Then verify the migration path independently: `alembic upgrade head`
Expected: exit 0, and `docker compose exec db psql -U pricewatcher -d pricewatcher -c "\dt"` lists all six tables.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml alembic.ini alembic/ pricewatcher/db/ tests/conftest.py tests/test_models.py
git commit -m "feat: Postgres schema, models and initial migration"
```

---

## Task 8: The shop registry

**Files:**
- Create: `pricewatcher/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: models from Task 7.
- Produces:
  - `get_shop(session, domain: str) -> Shop | None`
  - `confirm_shop(session, *, domain: str, selector: str, currency: str, decimal_style: str, needs_render: bool, platform: str | None, distrusted: list[str], confirmed_on: str, confirmed_by=None) -> Shop`
  - `flag_shop(session, domain: str, reason: str) -> Shop`
  - `ConflictingConfirmation` exception

- [ ] **Step 1: Write the failing test**

`tests/test_registry.py`:

```python
import pytest

from pricewatcher.db.models import Shop
from pricewatcher.registry import (
    ConflictingConfirmation, confirm_shop, flag_shop, get_shop,
)

ARGS = dict(
    domain="tennis-zone.hr", selector="div.product-main-price-display",
    currency="EUR", decimal_style="comma", needs_render=False,
    platform="shopware6", distrusted=["product:price:amount"],
    confirmed_on="https://tennis-zone.hr/p/1",
)


def test_confirming_a_new_shop_creates_a_trusted_entry(session):
    shop = confirm_shop(session, **ARGS)
    assert shop.status == "trusted"
    assert shop.selector == "div.product-main-price-display"
    assert shop.verified_count == 1
    assert shop.confirmed_at is not None


def test_get_shop_returns_none_for_unknown_domain(session):
    assert get_shop(session, "nobody.example") is None


def test_reconfirming_the_same_selector_increments_evidence(session):
    confirm_shop(session, **ARGS)
    shop = confirm_shop(session, **{**ARGS, "confirmed_on": ".../p/2"})
    assert shop.verified_count == 2
    assert shop.status == "trusted"


def test_a_disagreeing_confirmation_flags_and_does_not_overwrite(session):
    confirm_shop(session, **ARGS)
    with pytest.raises(ConflictingConfirmation):
        confirm_shop(session, **{**ARGS, "selector": "div.something-else"})
    shop = get_shop(session, "tennis-zone.hr")
    assert shop.selector == "div.product-main-price-display"   # unchanged
    assert shop.status == "flagged"


def test_flagging_records_status_without_touching_the_selector(session):
    confirm_shop(session, **ARGS)
    shop = flag_shop(session, "tennis-zone.hr", "selector stopped parsing")
    assert shop.status == "flagged"
    assert shop.selector == "div.product-main-price-display"


def test_flagging_an_unknown_domain_raises(session):
    with pytest.raises(LookupError):
        flag_shop(session, "nobody.example", "n/a")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.registry'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/registry.py`:

```python
"""The shared shop registry.

One user's confirmation decides correctness for everyone watching that shop,
which is what makes the second user free. The guard is that a *disagreeing*
confirmation flags the entry for review instead of overwriting it.
"""
from datetime import datetime, timezone

from pricewatcher.db.models import Shop


class ConflictingConfirmation(Exception):
    """A confirmation disagreed with the stored selector."""


def get_shop(session, domain: str) -> Shop | None:
    return session.get(Shop, domain)


def confirm_shop(session, *, domain: str, selector: str, currency: str,
                 decimal_style: str, needs_render: bool, platform: str | None,
                 distrusted: list[str], confirmed_on: str,
                 confirmed_by=None) -> Shop:
    now = datetime.now(timezone.utc)
    shop = get_shop(session, domain)

    if shop is None:
        shop = Shop(
            domain=domain, platform=platform, selector=selector,
            price_source="selector", currency=currency,
            decimal_style=decimal_style, needs_render=needs_render,
            distrusted=distrusted, status="trusted", confirmed_by=confirmed_by,
            confirmed_at=now, confirmed_on=confirmed_on, verified_count=1,
        )
        session.add(shop)
        session.flush()
        return shop

    if shop.selector != selector:
        shop.status = "flagged"
        session.flush()
        raise ConflictingConfirmation(
            f"{domain}: stored {shop.selector!r} but confirmed {selector!r}; "
            "entry flagged for review, not overwritten"
        )

    shop.verified_count += 1
    shop.status = "trusted"
    shop.confirmed_at = now
    shop.confirmed_on = confirmed_on
    shop.needs_render = needs_render
    shop.distrusted = distrusted
    session.flush()
    return shop


def flag_shop(session, domain: str, reason: str) -> Shop:
    shop = get_shop(session, domain)
    if shop is None:
        raise LookupError(f"unknown domain: {domain}")
    shop.status = "flagged"
    session.flush()
    return shop
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_registry.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/registry.py tests/test_registry.py
git commit -m "feat: shared shop registry with conflict guard"
```

---

## Task 9: Fetching, with politeness

**Files:**
- Create: `pricewatcher/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FetchResult` dataclass: `status: int`, `html: str`, `url: str`, `blocked: bool`
  - `Fetcher(min_host_gap: float = 5.0, timeout: int = 20, session=None)` with `.get(url) -> FetchResult`
  - `render(url: str, timeout_ms: int = 20000) -> str`
  - `MIN_HOST_GAP_SECONDS = 5.0`, `MAX_CONCURRENT_FETCHES = 4`

- [ ] **Step 1: Write the failing test**

`tests/test_fetch.py`:

```python
import pytest

from pricewatcher.fetch import (
    MAX_CONCURRENT_FETCHES, MIN_HOST_GAP_SECONDS, FetchResult, Fetcher,
)


class FakeResponse:
    def __init__(self, status=200, text="<html></html>"):
        self.status_code = status
        self.text = text


class FakeSession:
    """Records calls and returns queued responses; no network."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses.pop(0) if self.responses else FakeResponse()


def test_politeness_constants_match_the_spec():
    assert MIN_HOST_GAP_SECONDS == 5.0
    assert MAX_CONCURRENT_FETCHES == 4


def test_get_returns_html_and_status():
    fake = FakeSession([FakeResponse(200, "<html>hi</html>")])
    r = Fetcher(session=fake).get("https://x.com/p")
    assert isinstance(r, FetchResult)
    assert (r.status, r.html, r.blocked) == (200, "<html>hi</html>", False)


def test_403_is_reported_as_blocked_not_raised():
    fake = FakeSession([FakeResponse(403, "denied")])
    r = Fetcher(session=fake).get("https://x.com/p")
    assert r.status == 403
    assert r.blocked is True


def test_second_request_to_same_host_waits_for_the_gap(monkeypatch):
    slept = []
    monkeypatch.setattr("pricewatcher.fetch.time.sleep", slept.append)
    monkeypatch.setattr("pricewatcher.fetch.time.monotonic", lambda: 0.0)

    f = Fetcher(min_host_gap=5.0, session=FakeSession(
        [FakeResponse(), FakeResponse()]))
    f.get("https://x.com/a")
    f.get("https://x.com/b")
    assert slept and slept[0] == pytest.approx(5.0)


def test_different_hosts_do_not_wait_on_each_other(monkeypatch):
    slept = []
    monkeypatch.setattr("pricewatcher.fetch.time.sleep", slept.append)
    monkeypatch.setattr("pricewatcher.fetch.time.monotonic", lambda: 0.0)

    f = Fetcher(min_host_gap=5.0, session=FakeSession(
        [FakeResponse(), FakeResponse()]))
    f.get("https://a.com/1")
    f.get("https://b.com/1")
    assert slept == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.fetch'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/fetch.py`:

```python
"""HTTP fetching with per-host politeness, plus Playwright rendering.

The per-host gap is the constraint that keeps this service welcome. It is
enforced here rather than at the call site so no caller can forget it.
"""
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

MIN_HOST_GAP_SECONDS = 5.0
MAX_CONCURRENT_FETCHES = 4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "hr,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


@dataclass(frozen=True)
class FetchResult:
    status: int
    html: str
    url: str
    blocked: bool


class Fetcher:
    def __init__(self, min_host_gap: float = MIN_HOST_GAP_SECONDS,
                 timeout: int = 20, session=None):
        self.min_host_gap = min_host_gap
        self.timeout = timeout
        self._session = session or requests.Session()
        self._last_hit: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_hit.get(host)
        now = time.monotonic()
        if last is not None:
            remaining = self.min_host_gap - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str) -> FetchResult:
        host = urlsplit(url).netloc.lower()
        self._wait_for_host(host)
        resp = self._session.get(url, headers=HEADERS, timeout=self.timeout)
        return FetchResult(
            status=resp.status_code,
            html=resp.text,
            url=url,
            blocked=resp.status_code in (401, 403, 429),
        )


def render(url: str, timeout_ms: int = 20000) -> str:
    """Return HTML after JavaScript has run. Used for discovery only."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            return page.content()
        finally:
            browser.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fetch.py -v`
Expected: PASS — 5 passed

Then install the browser once so Task 10 can render: `playwright install chromium`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/fetch.py tests/test_fetch.py
git commit -m "feat: fetcher with enforced per-host politeness"
```

---

## Task 10: Price resolution with render escalation

Static HTML first; a rendered DOM only when static fails. This is where spec §5
"Render escalation" lives, and it is what makes `needs_render` an observation
rather than a guess.

**Files:**
- Create: `pricewatcher/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `from_selector`, `soup_of` (Task 4); `rank_candidates` (Task 6); `FetchResult`, `render` (Task 9).
- Produces:
  - `Resolution` dataclass: `price: float | None`, `used_render: bool`, `html: str`, `status: str` (`'ok'` | `'no_price'` | `'blocked'`), `http_status: int | None`
  - `resolve_price(fetcher, url: str, selector: str | None, *, allow_render: bool = True, renderer=render) -> Resolution`

- [ ] **Step 1: Write the failing test**

`tests/test_resolve.py`:

```python
from pricewatcher.fetch import FetchResult
from pricewatcher.resolve import Resolution, resolve_price
from tests.conftest import load_fixture

URL = "https://tennis-zone.hr/p/1"
SELECTOR = "div.product-main-price-display"
JS_ONLY = "<html><body><div id='app'></div></body></html>"


class StubFetcher:
    def __init__(self, html, status=200):
        self.html, self.status = html, status
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return FetchResult(self.status, self.html, url,
                           self.status in (401, 403, 429))


def _renderer(html):
    calls = []

    def render(url, timeout_ms=20000):
        calls.append(url)
        return html

    render.calls = calls
    return render


def test_static_hit_never_launches_a_browser():
    r = _renderer("<html>should not be used</html>")
    res = resolve_price(StubFetcher(load_fixture("tennis_zone_head.html")),
                        URL, SELECTOR, renderer=r)
    assert isinstance(res, Resolution)
    assert (res.price, res.used_render, res.status) == (248.90, False, "ok")
    assert r.calls == []


def test_escalates_to_render_when_selector_missing_statically():
    r = _renderer(load_fixture("tennis_zone_head.html"))
    res = resolve_price(StubFetcher(JS_ONLY), URL, SELECTOR, renderer=r)
    assert res.price == 248.90
    assert res.used_render is True
    assert res.status == "ok"
    assert r.calls == [URL]


def test_no_price_when_both_static_and_rendered_fail():
    r = _renderer(JS_ONLY)
    res = resolve_price(StubFetcher(JS_ONLY), URL, SELECTOR, renderer=r)
    assert res.price is None
    assert res.status == "no_price"
    assert res.used_render is True


def test_renders_at_most_once_per_call():
    r = _renderer(JS_ONLY)
    resolve_price(StubFetcher(JS_ONLY), URL, SELECTOR, renderer=r)
    assert len(r.calls) == 1


def test_allow_render_false_never_renders():
    r = _renderer(load_fixture("tennis_zone_head.html"))
    res = resolve_price(StubFetcher(JS_ONLY), URL, SELECTOR,
                        allow_render=False, renderer=r)
    assert res.price is None
    assert res.used_render is False
    assert r.calls == []


def test_blocked_fetch_does_not_escalate():
    r = _renderer(load_fixture("tennis_zone_head.html"))
    res = resolve_price(StubFetcher("denied", status=403), URL, SELECTOR,
                        renderer=r)
    assert res.status == "blocked"
    assert res.http_status == 403
    assert r.calls == []


def test_discovery_mode_succeeds_when_static_has_any_candidate():
    r = _renderer(JS_ONLY)
    res = resolve_price(StubFetcher(load_fixture("tennis_zone_head.html")),
                        URL, None, renderer=r)
    assert res.status == "ok"
    assert res.used_render is False
    assert r.calls == []


def test_discovery_mode_escalates_when_static_has_no_candidates():
    r = _renderer(load_fixture("tennis_zone_head.html"))
    res = resolve_price(StubFetcher(JS_ONLY), URL, None, renderer=r)
    assert res.status == "ok"
    assert res.used_render is True


def test_returns_the_html_actually_used():
    """Callers rank candidates and detect the platform on this HTML, so it must
    be the rendered DOM whenever rendering happened."""
    r = _renderer(load_fixture("tennis_zone_head.html"))
    res = resolve_price(StubFetcher(JS_ONLY), URL, SELECTOR, renderer=r)
    assert "product-main-price-display" in res.html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_resolve.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.resolve'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/resolve.py`:

```python
"""Static HTML first, rendered DOM only on failure (spec section 5).

The escalation is what separates two failures that look identical: a shop that
moved to client-side rendering (static fails, rendered works - self-healing,
needs no human) from a shop that redesigned (both fail - genuine rot, needs
re-confirmation). Rendering is attempted at most once per call.
"""
from dataclasses import dataclass

from pricewatcher.extract import from_selector, soup_of
from pricewatcher.fetch import render as default_render
from pricewatcher.ranking import rank_candidates


@dataclass(frozen=True)
class Resolution:
    price: float | None
    used_render: bool
    html: str
    status: str                 # 'ok' | 'no_price' | 'blocked'
    http_status: int | None


def _attempt(html: str, selector: str | None) -> float | None:
    """Price if this HTML satisfies what we are looking for, else None."""
    if selector:
        return from_selector(soup_of(html), selector)
    # Discovery mode: any usable candidate counts as success.
    candidates = rank_candidates(html)
    return candidates[0].price if candidates else None


def resolve_price(fetcher, url: str, selector: str | None, *,
                  allow_render: bool = True,
                  renderer=default_render) -> Resolution:
    result = fetcher.get(url)
    if result.blocked:
        return Resolution(None, False, "", "blocked", result.status)

    price = _attempt(result.html, selector)
    if price is not None:
        return Resolution(price, False, result.html, "ok", result.status)

    if not allow_render:
        return Resolution(None, False, result.html, "no_price", result.status)

    rendered = renderer(url)
    price = _attempt(rendered, selector)
    status = "ok" if price is not None else "no_price"
    return Resolution(price, True, rendered, status, result.status)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/resolve.py tests/test_resolve.py
git commit -m "feat: static-first price resolution with render escalation"
```

---

## Task 11: Add-product orchestration

**Files:**
- Create: `pricewatcher/products.py`
- Test: `tests/test_products.py`

**Interfaces:**
- Consumes: `canonicalize_url`, `domain_of` (Task 2); `Candidate`, `from_selector`, `soup_of` (Task 4); `detect_platform` (Task 5); `rank_candidates`, `distrusted_sources` (Task 6); models (Task 7); `get_shop`, `confirm_shop`, `flag_shop` (Task 8); `FetchResult` (Task 9); `resolve_price`, `Resolution` (Task 10).
- **Uses `resolve_price` for every fetch**, never `fetcher.get` directly, so escalation and `needs_render` are handled in one place. Pass `allow_render=False` when the shop is already `flagged`, per the spec's guard against launching a browser for a permanently dead product.
- Produces:
  - `AddResult` dataclass: `status: str` (`'added'` | `'needs_confirmation'` | `'blocked'`), `product_id: uuid.UUID | None`, `price: float | None`, `candidates: list[Candidate]`, `domain: str`, `canonical_url: str`
  - `add_product(session, fetcher, url: str) -> AddResult`
  - `confirm_and_add(session, fetcher, url: str, selector: str, currency: str = "EUR") -> AddResult`

- [ ] **Step 1: Write the failing test**

`tests/test_products.py`:

```python
from pricewatcher.db.models import Check, PricePoint, Product
from pricewatcher.fetch import FetchResult
from pricewatcher.products import add_product, confirm_and_add
from pricewatcher.registry import confirm_shop, get_shop
from tests.conftest import load_fixture

HEAD_URL = "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769"


class StubFetcher:
    def __init__(self, html, status=200):
        self.html, self.status = html, status
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return FetchResult(self.status, self.html, url,
                           self.status in (401, 403, 429))


def _never_render(url, timeout_ms=20000):
    raise AssertionError("should not have needed a browser")


def _confirm_tennis_zone(session):
    return confirm_shop(
        session, domain="tennis-zone.hr",
        selector="div.product-main-price-display", currency="EUR",
        decimal_style="comma", needs_render=False, platform="shopware6",
        distrusted=["product:price:amount"], confirmed_on=HEAD_URL,
    )


def test_unknown_shop_asks_for_confirmation_and_stores_nothing(session):
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    r = add_product(session, f, HEAD_URL)
    assert r.status == "needs_confirmation"
    assert r.product_id is None
    assert r.candidates[0].price == 248.90
    assert get_shop(session, "tennis-zone.hr") is None


def test_confirming_writes_the_shop_and_the_product(session):
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    r = confirm_and_add(session, f, HEAD_URL, "div.product-main-price-display")
    assert r.status == "added"
    assert r.price == 248.90
    shop = get_shop(session, "tennis-zone.hr")
    assert shop.selector == "div.product-main-price-display"
    assert shop.platform == "shopware6"
    assert "product:price:amount" in shop.distrusted


def test_known_shop_adds_without_confirmation(session):
    _confirm_tennis_zone(session)
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    r = add_product(session, f, HEAD_URL)
    assert r.status == "added"
    assert r.price == 248.90
    assert r.candidates == []


def test_adding_records_a_price_point_and_a_check(session):
    _confirm_tennis_zone(session)
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    add_product(session, f, HEAD_URL)
    assert session.query(PricePoint).count() == 1
    assert session.query(Check).one().status == "ok"


def test_same_url_with_tracking_params_is_one_product(session):
    _confirm_tennis_zone(session)
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    add_product(session, f, HEAD_URL)
    add_product(session, f, HEAD_URL + "?utm_source=x")
    assert session.query(Product).count() == 1


def test_broken_selector_on_known_shop_flags_and_asks_again(session):
    _confirm_tennis_zone(session)
    f = StubFetcher("<html><body>redesigned, no price here</body></html>")
    r = add_product(session, f, HEAD_URL)
    assert r.status == "needs_confirmation"
    assert get_shop(session, "tennis-zone.hr").status == "flagged"
    assert session.query(Check).one().status == "no_price"


def test_blocked_fetch_reports_blocked_and_records_the_check(session):
    _confirm_tennis_zone(session)
    f = StubFetcher("denied", status=403)
    r = add_product(session, f, HEAD_URL)
    assert r.status == "blocked"
    assert session.query(Check).one().status == "blocked"


def test_confirming_records_needs_render_false_for_a_static_shop(session):
    f = StubFetcher(load_fixture("tennis_zone_head.html"))
    confirm_and_add(session, f, HEAD_URL, "div.product-main-price-display",
                    renderer=_never_render)
    assert get_shop(session, "tennis-zone.hr").needs_render is False


def test_confirming_records_needs_render_true_for_a_js_shop(session):
    f = StubFetcher("<html><body><div id='app'></div></body></html>")
    confirm_and_add(session, f, HEAD_URL, "div.product-main-price-display",
                    renderer=lambda url, timeout_ms=20000:
                        load_fixture("tennis_zone_head.html"))
    assert get_shop(session, "tennis-zone.hr").needs_render is True


def test_shop_moving_to_client_side_rendering_self_heals(session):
    """Static fails, rendered works with the same selector: not rot."""
    _confirm_tennis_zone(session)
    f = StubFetcher("<html><body><div id='app'></div></body></html>")
    r = add_product(session, f, HEAD_URL,
                    renderer=lambda url, timeout_ms=20000:
                        load_fixture("tennis_zone_head.html"))
    assert r.status == "added"
    assert r.price == 248.90
    shop = get_shop(session, "tennis-zone.hr")
    assert shop.needs_render is True
    assert shop.status == "trusted"        # not flagged - nothing is broken


def test_a_flagged_shop_does_not_launch_a_browser(session):
    from pricewatcher.registry import flag_shop
    _confirm_tennis_zone(session)
    flag_shop(session, "tennis-zone.hr", "already known broken")
    f = StubFetcher("<html><body>nothing</body></html>")
    calls = []

    def counting_renderer(url, timeout_ms=20000):
        calls.append(url)
        return ""

    add_product(session, f, HEAD_URL, renderer=counting_renderer)
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_products.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.products'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/products.py`:

```python
"""Add-product orchestration — spec §5.

Note what never happens here: guessing. A known shop whose selector stops
parsing is flagged and routed back to confirmation; it is never quietly
resolved from a different source.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pricewatcher.canonical import canonicalize_url, domain_of
from pricewatcher.db.models import Check, PricePoint, Product
from pricewatcher.extract import Candidate, from_selector, soup_of
from pricewatcher.platforms import detect_platform
from pricewatcher.ranking import distrusted_sources, rank_candidates
from pricewatcher.registry import confirm_shop, flag_shop, get_shop
from pricewatcher.resolve import resolve_price
from pricewatcher.fetch import render as default_render


@dataclass
class AddResult:
    status: str                     # 'added' | 'needs_confirmation' | 'blocked'
    domain: str
    canonical_url: str
    product_id: uuid.UUID | None = None
    price: float | None = None
    candidates: list[Candidate] = field(default_factory=list)


def _record_check(session, product_id, status, http_status=None, error=None):
    session.add(Check(
        product_id=product_id, status=status, http_status=http_status,
        error=error, finished_at=datetime.now(timezone.utc),
    ))
    session.flush()


def _upsert_product(session, canonical_url: str, domain: str) -> Product:
    existing = session.query(Product).filter_by(canonical_url=canonical_url).one_or_none()
    if existing:
        return existing
    product = Product(canonical_url=canonical_url, domain=domain)
    session.add(product)
    session.flush()
    return product


def add_product(session, fetcher, url: str, *,
                renderer=default_render) -> AddResult:
    canonical = canonicalize_url(url)
    domain = domain_of(canonical)
    shop = get_shop(session, domain)

    trusted = shop is not None and shop.status == "trusted" and shop.selector
    selector = shop.selector if trusted else None
    # Never launch a browser for a shop already known to be broken.
    allow_render = not (shop is not None and shop.status == "flagged")

    res = resolve_price(fetcher, canonical, selector,
                        allow_render=allow_render, renderer=renderer)

    if res.status == "blocked":
        product = session.query(Product).filter_by(
            canonical_url=canonical).one_or_none()
        if shop is not None and product is None:
            product = _upsert_product(session, canonical, domain)
        if product is not None:
            _record_check(session, product.id, "blocked", res.http_status)
        return AddResult("blocked", domain, canonical,
                         product_id=product.id if product else None)

    if not trusted:
        return AddResult("needs_confirmation", domain, canonical,
                         candidates=rank_candidates(res.html))

    product = _upsert_product(session, canonical, domain)

    if res.price is None:
        flag_shop(session, domain, "stored selector stopped parsing")
        _record_check(session, product.id, "no_price", res.http_status)
        return AddResult("needs_confirmation", domain, canonical,
                         product_id=product.id,
                         candidates=rank_candidates(res.html))

    # Static failed but the rendered DOM resolved: the shop moved to
    # client-side rendering. Self-heal instead of asking a human.
    if res.used_render and not shop.needs_render:
        shop.needs_render = True
        session.flush()

    session.add(PricePoint(product_id=product.id, price=res.price,
                           currency=shop.currency, source=shop.price_source))
    _record_check(session, product.id, "ok", res.http_status)
    return AddResult("added", domain, canonical,
                     product_id=product.id, price=res.price)


def confirm_and_add(session, fetcher, url: str, selector: str,
                    currency: str = "EUR", *,
                    renderer=default_render) -> AddResult:
    canonical = canonicalize_url(url)
    domain = domain_of(canonical)

    res = resolve_price(fetcher, canonical, selector, renderer=renderer)
    if res.status == "blocked":
        return AddResult("blocked", domain, canonical)
    if res.price is None:
        raise ValueError(
            f"selector {selector!r} does not parse to a price on {canonical}")

    decimal_style = "comma" if "," in _price_text(res.html, selector) else "dot"

    confirm_shop(
        session, domain=domain, selector=selector, currency=currency,
        decimal_style=decimal_style,
        # Observed, not assumed: true exactly when rendering was required.
        needs_render=res.used_render,
        platform=detect_platform(res.html),
        distrusted=distrusted_sources(res.html), confirmed_on=canonical,
    )

    product = _upsert_product(session, canonical, domain)
    session.add(PricePoint(product_id=product.id, price=res.price,
                           currency=currency, source="selector"))
    _record_check(session, product.id, "ok", res.http_status)
    return AddResult("added", domain, canonical,
                     product_id=product.id, price=res.price)


def _price_text(html: str, selector: str) -> str:
    el = soup_of(html).select_one(selector)
    return " ".join(el.get_text(strip=True).split()) if el else ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_products.py -v`
Expected: PASS — 12 passed

Then run the whole suite: `pytest -v`
Expected: PASS — all tests from Tasks 1-10, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/products.py tests/test_products.py
git commit -m "feat: add-product orchestration with flag-not-guess on selector rot"
```

---

## Task 12: CLI harness

This is the Phase 1 done-criterion: adding a URL and confirming a price, end to end, against a real shop and a real database.

**Files:**
- Create: `pricewatcher/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None) -> int`, and subcommands `debug`, `add`, `confirm`, `check`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
from pricewatcher.cli import build_parser, format_candidates
from pricewatcher.ranking import rank_candidates
from tests.conftest import load_fixture


def test_parser_exposes_the_four_subcommands():
    p = build_parser()
    for cmd in ("debug", "add", "confirm", "check"):
        assert p.parse_args([cmd, "https://x.com/p"]).command == cmd


def test_confirm_requires_a_selector():
    args = build_parser().parse_args(
        ["confirm", "https://x.com/p", "--selector", "div.p"])
    assert args.selector == "div.p"


def test_format_candidates_shows_price_and_selector():
    out = format_candidates(rank_candidates(load_fixture("tennis_zone_head.html")))
    assert "248.90" in out
    assert "div.product-main-price-display" in out


def test_format_candidates_numbers_rows_for_selection():
    out = format_candidates(rank_candidates(load_fixture("tennis_zone_head.html")))
    assert out.splitlines()[0].strip().startswith("1)")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pricewatcher.cli'`

- [ ] **Step 3: Write minimal implementation**

`pricewatcher/cli.py`:

```python
"""Phase 1 harness. Replaced by the web layer in Phase 2; kept for diagnosis.

    python -m pricewatcher.cli debug   <url>
    python -m pricewatcher.cli add     <url>
    python -m pricewatcher.cli confirm <url> --selector "div.foo"
    python -m pricewatcher.cli check   <url>
"""
import argparse
import sys

from pricewatcher.db.session import session_scope
from pricewatcher.extract import Candidate
from pricewatcher.fetch import Fetcher, render
from pricewatcher.products import add_product, confirm_and_add
from pricewatcher.ranking import rank_candidates


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pricewatcher")
    sub = p.add_subparsers(dest="command", required=True)
    for cmd in ("debug", "add", "check"):
        s = sub.add_parser(cmd)
        s.add_argument("url")
        s.add_argument("--render", action="store_true",
                       help="load the page with a browser first")
    c = sub.add_parser("confirm")
    c.add_argument("url")
    c.add_argument("--selector", required=True)
    c.add_argument("--currency", default="EUR")
    c.add_argument("--render", action="store_true")
    return p


def format_candidates(candidates: list[Candidate]) -> str:
    lines = []
    for i, c in enumerate(candidates, start=1):
        target = c.selector or c.source
        lines.append(f"  {i}) {c.price:>10.2f}  {target}"
                     f"   ({c.kind}, {c.match_count} match)")
    return "\n".join(lines)


def _html(url: str, use_render: bool) -> str:
    if use_render:
        return render(url)
    return Fetcher().get(url).html


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "debug":
        cands = rank_candidates(_html(args.url, args.render))
        if not cands:
            print("No price candidates found. The page may need --render.")
            return 1
        print(f"\nCandidates for {args.url}\n")
        print(format_candidates(cands))
        print("\nConfirm with:  python -m pricewatcher.cli confirm "
              f'"{args.url}" --selector "<selector>"\n')
        return 0

    with session_scope() as session:
        fetcher = Fetcher()
        if args.command == "confirm":
            r = confirm_and_add(session, fetcher, args.url,
                                args.selector, args.currency)
        else:
            r = add_product(session, fetcher, args.url)

        if r.status == "added":
            print(f"{r.status}: {r.canonical_url} at {r.price:.2f}")
            return 0
        if r.status == "blocked":
            print(f"blocked: {r.domain} refused the request (see spec section 9)")
            return 1
        print(f"needs confirmation for {r.domain}:\n")
        print(format_candidates(r.candidates))
        print("\nConfirm with:  python -m pricewatcher.cli confirm "
              f'"{args.url}" --selector "<selector>"\n')
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, then the real end-to-end check**

Run: `pytest tests/test_cli.py -v`
Expected: PASS — 4 passed

Then, with `docker compose up -d db` and `alembic upgrade head` done, run the
actual acceptance check against the live shop:

```bash
python -m pricewatcher.cli debug "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769"
```
Expected: candidate `1)` is `248.90  div.product-main-price-display`.

```bash
python -m pricewatcher.cli confirm "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769" --selector "div.product-main-price-display"
python -m pricewatcher.cli add "https://tennis-zone.hr/padel/reketi/adidas/adidas-metalbone-2026/ST107802"
```
Expected: the first prints `added: ... at 248.90`; the second prints
`added: ... at 275.90` **without asking for confirmation** — that is the core
bet of Layer 1 demonstrated end to end.

- [ ] **Step 5: Commit**

```bash
git add pricewatcher/cli.py tests/test_cli.py
git commit -m "feat: Phase 1 CLI harness"
```

---

## Self-Review

**Spec coverage.** §4 data model → Task 7. §4 canonicalization → Task 2. §5 add flow, all three branches → Task 11. §5 discovery/platform → Tasks 4, 5. §5 render escalation → Task 10. §6 ranking and the trap rule → Task 6. §7 politeness limits → Task 9. §7 escalation on polling → Task 10. §9 blocked-IP handling → Task 9 (`blocked` flag) and Tasks 10-11 (`blocked` status), with the manual check in Task 3 Step 4. §9 registry conflict guard → Task 8. §9 selector rot → Task 11. §10 evidence numbers → asserted in Tasks 4 and 6.

**Deliberately not covered here**, and correct to defer: `users`/`watches` behaviour (Phase 2), the scheduler and notifications (Phase 3), "hit me" cache window and quotas (Phase 4), Telegram (Phase 5), and the `robots.txt` fetch-and-obey step, which belongs with the scheduler that does repeat fetching rather than with a one-shot add. Phase 3's plan must open with it.

**Previously recorded gap, now closed.** An earlier version of this plan noted that `confirm_and_add` could only ever write `needs_render=False`, and deferred the fix to Phase 2. The render-escalation amendment (spec §13, 2026-09-10) resolves it inside Phase 1: `Resolution.used_render` records whether rendering was actually required, and Task 11 writes that value straight into the registry. `needs_render` is now observed, never assumed.

**Type consistency.** `Candidate` fields are used identically in Tasks 4, 6, 10, 11 and 12. `FetchResult` is constructed in Task 9 and stubbed with the same four fields in Tasks 10 and 11. `Resolution.status` and `AddResult.status` share the literals `'ok'`/`'blocked'` and `'no_price'`/`'needs_confirmation'` respectively — they are deliberately different vocabularies, one describing a fetch and one describing a user-facing outcome. `confirm_shop` is called with the full keyword set in Tasks 8 and 11.

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, reviewed between tasks.
2. **Inline Execution** — tasks executed in this session with checkpoints.
