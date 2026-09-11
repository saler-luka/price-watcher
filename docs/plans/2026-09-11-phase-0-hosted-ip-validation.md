# Phase 0: Hosted-IP Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Know, with recorded evidence, whether a datacenter IP is served by the target shops — and make the hosting decision that gates Phase 1.

**Architecture:** A single-file probe script with no project dependencies runs the same request from three vantage points (the owner's home connection as control, then one or two cloud hosts) and prints a machine-readable verdict per shop. The verdicts are recorded in a dated note, the decision is recorded in a spec amendment, and the handoff is updated. Nothing here becomes part of the service.

**Tech Stack:** Python 3.12 standard library only (`urllib`, `json`, `ssl`). No `requests`, no venv — the script must run on a fresh cloud box with nothing installed beyond Python.

**Spec:** `docs/specs/2026-09-09-price-watcher-saas-design.md` — §9 "Blocked before we start" states the question, §11 lists Phase 0 as the gate, §9 "Graceful degradation, not evasion" bounds what a negative result may lead to.

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include these.

- **Politeness:** at least **5 seconds** between requests to the same host. The probe makes very few requests, but the constraint still applies.
- **No attempt to defeat bot protection.** The probe sends one honest browser-like User-Agent and nothing else. It does not rotate agents, does not add a `Referer`, does not use a proxy. If the answer is 403, the answer is 403.
- **Do not build on assumption.** Phase 1 does not start until the finding in Task 5 is committed.

### What "served" means

A shop counts as **served** from a vantage point only if all three hold:

1. HTTP status is 200
2. the response body contains the product's price selector (`product-main-price-display` for tennis-zone.hr), proving it is the real page and not a challenge or interstitial
3. the body does **not** contain a challenge marker (`cf-challenge`, `Just a moment`, `Checking your browser`, `captcha`)

A 200 with a Cloudflare challenge page is a **block**, not a success. This is the mistake the probe exists to prevent.

> **Execution note (2026-09-11):** condition 3 is evaluated only when condition 2
> fails. The control run showed the real page contains the bare string
> `captcha` (Shopware feature flags + `captcha.js`), so checking markers first
> misclassified a genuine page. The price element is the shop's own DOM and an
> interstitial never carries it, so its presence is decisive. Details in
> `docs/notes/2026-09-11-hosted-ip-findings.md`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/probe_access.py` | The probe. Stdlib only, prints one JSON line per shop, exit code encodes the verdict. |
| `tests/test_probe_access.py` | Tests the verdict logic offline. The tool that produces the decision must itself be trusted. |
| `docs/notes/2026-09-11-hosted-ip-findings.md` | The recorded measurement: raw output from every vantage point. |
| `docs/specs/2026-09-09-price-watcher-saas-design.md` | §13 gains the hosting decision. |
| `HANDOFF.md` | Status table updated; Phase 0 marked done; next action becomes Phase 1 Task 1. |

Note: `tests/` does not exist yet — Phase 1 Task 1 creates it with `pytest`. Phase 0 runs first, so this plan creates a minimal `tests/` with a stdlib-only test runnable via `python -m unittest`, and Phase 1 Task 1 adopts it into pytest unchanged.

---

## Task 1: The probe's verdict logic

The verdict is the whole point. Test it before wiring it to the network.

**Files:**
- Create: `scripts/__init__.py` (empty, so the test can import the script)
- Create: `scripts/probe_access.py`
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_probe_access.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `classify(status: int, body: str, expect_marker: str) -> str` returning one of `'served'`, `'blocked'`, `'challenge'`, `'not_product_page'`, `'error'`
  - `CHALLENGE_MARKERS: tuple[str, ...]`

- [x] **Step 1: Write the failing test**

`tests/test_probe_access.py`:

```python
import unittest

from scripts.probe_access import CHALLENGE_MARKERS, classify

MARKER = "product-main-price-display"
REAL_PAGE = f'<html><body><div class="{MARKER}">248,90 €</div></body></html>'


class ClassifyTests(unittest.TestCase):

    def test_200_with_the_price_element_is_served(self):
        self.assertEqual(classify(200, REAL_PAGE, MARKER), "served")

    def test_403_is_blocked(self):
        self.assertEqual(classify(403, "<html>Forbidden</html>", MARKER), "blocked")

    def test_429_is_blocked(self):
        self.assertEqual(classify(429, "", MARKER), "blocked")

    def test_200_cloudflare_challenge_is_a_block_not_a_success(self):
        """The trap this probe exists to catch: a 200 that is not the page."""
        body = "<html><title>Just a moment...</title><div id='cf-challenge'></div></html>"
        self.assertEqual(classify(200, body, MARKER), "challenge")

    def test_200_without_the_price_element_is_not_the_product_page(self):
        body = "<html><body>Welcome to our shop</body></html>"
        self.assertEqual(classify(200, body, MARKER), "not_product_page")

    def test_5xx_is_error_not_blocked(self):
        self.assertEqual(classify(503, "", MARKER), "error")

    def test_challenge_detection_is_case_insensitive(self):
        body = "<html>CHECKING YOUR BROWSER before accessing</html>"
        self.assertEqual(classify(200, body, MARKER), "challenge")

    def test_challenge_markers_include_the_known_cloudflare_strings(self):
        for m in ("cf-challenge", "just a moment", "checking your browser", "captcha"):
            self.assertIn(m, [x.lower() for x in CHALLENGE_MARKERS])


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_probe_access -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.probe_access'`

- [x] **Step 3: Write minimal implementation**

`scripts/__init__.py` and `tests/__init__.py`: empty files.

`scripts/probe_access.py`:

```python
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
    lowered = body.lower()
    if any(m.lower() in lowered for m in CHALLENGE_MARKERS):
        return "challenge"
    if expect_marker not in body:
        return "not_product_page"
    return "served"


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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_probe_access -v`
Expected: PASS — 8 tests OK

- [ ] **Step 5: Commit**

```bash
git add scripts/ tests/
git commit -m "feat: Phase 0 access probe with tested verdict logic"
```

---

## Task 2: Control run from the home connection

The home connection is the known-good baseline (it returned 200 on 2026-09-09). If the probe does not say `served` from here, the probe is wrong, not the shop — fix the probe before trusting any cloud result.

**Files:**
- Create: `docs/notes/2026-09-11-hosted-ip-findings.md`

**Interfaces:**
- Consumes: `scripts/probe_access.py` from Task 1.
- Produces: the findings note, with the control section filled.

- [x] **Step 1: Run the probe from the owner's machine**

Run, from the project directory:

```bash
python scripts/probe_access.py --label home > /tmp/probe-home.jsonl; echo "exit=$?"
cat /tmp/probe-home.jsonl
```

Expected: `exit=0`, and two JSON lines each with `"verdict": "served"`, `"status": 200`, and `"server": "cloudflare"` (the shop is behind Cloudflare, so this is the normal fingerprint).

If either verdict is not `served`, **stop**. Either the shop changed since 2026-09-09 (re-run `python main.py --debug-url <url>` to check the marker still exists), or `classify` has a bug. Do not proceed to a cloud run with a probe that fails its own control.

- [x] **Step 2: Create the findings note with the control result**

`docs/notes/2026-09-11-hosted-ip-findings.md`:

```markdown
# Hosted-IP access findings

**Date:** 2026-09-11
**Status:** in progress — control done, cloud vantage points pending
**Answers:** spec §9 "Blocked before we start: does a hosted IP get served at all?"
**Method:** `scripts/probe_access.py` — one honest request per product page,
no header tricks, no proxy. "Served" requires HTTP 200 *and* the price element
present *and* no challenge markers. A 200 that is a Cloudflare interstitial
counts as a block.

## Vantage points

| Label | Provider / region | Type | Verdict |
|---|---|---|---|
| home | owner's ISP, Zagreb | residential | (fill from Step 1) |
| (cloud 1) | | datacenter | pending |
| (cloud 2) | | datacenter | pending |

## Raw output

### home

```
(paste /tmp/probe-home.jsonl here, verbatim)
```

## Interpretation

(written in Task 5, after all vantage points are measured)
```

Paste the actual JSONL output into the `home` section and fill the `home` row of the table.

- [ ] **Step 3: Commit**

```bash
git add docs/notes/2026-09-11-hosted-ip-findings.md
git commit -m "docs: Phase 0 control measurement from home connection"
```

---

## Task 3: First cloud vantage point — Hetzner

Hetzner is the primary candidate: cheapest always-on box in the EU, and its IP ranges are well-known to bot-protection vendors, so it is the *pessimistic* test. If Hetzner is served, most hosts will be.

This task is executed by the owner, not an agent — it requires a Hetzner account and a payment method. The agent's job is to make it a copy-paste exercise.

> **Execution note (2026-09-11):** the owner chose not to rent a server. The
> datacenter vantage point is a GitHub Actions runner instead
> (`.github/workflows/probe-access.yml`, label `github-actions-azure`). Steps
> 1-3 below are replaced by: push the branch, run the workflow manually, copy
> the JSONL from the job summary. Step 4-5 apply unchanged. The limit of the
> substitution is recorded in the findings note.

**Files:**
- Modify: `docs/notes/2026-09-11-hosted-ip-findings.md`

**Interfaces:**
- Consumes: `scripts/probe_access.py`.
- Produces: the `hetzner` row and raw section in the findings note.

- [ ] **Step 1: Create the smallest server**

In the Hetzner Cloud console (or with `hcloud` if installed):

- Location: **Falkenstein (fsn1)** or **Nuremberg (nbg1)** — EU, close to the shop
- Image: **Ubuntu 24.04**
- Type: **CX22** (or the smallest shared-vCPU type offered) — this runs for minutes, cost is negligible
- SSH key: the owner's

Note the public IPv4 address as `<IP>`.

- [ ] **Step 2: Copy the probe and run it**

From the project directory on the owner's machine:

```bash
scp scripts/probe_access.py root@<IP>:/root/
ssh root@<IP> 'python3 /root/probe_access.py --label hetzner-fsn1' > /tmp/probe-hetzner.jsonl; echo "exit=$?"
cat /tmp/probe-hetzner.jsonl
```

Ubuntu 24.04 ships Python 3.12; nothing needs installing.

Expected: two JSON lines. **Either outcome is a valid result.** Record it exactly:

- `exit=0`, both `served` → the pessimistic host is served. Strong positive.
- `exit=1`, `blocked` or `challenge` → the hosting assumption is wrong for this shop. Also a valid, important result.

- [ ] **Step 3: Destroy the server**

Delete it in the console (or `hcloud server delete <name>`). Confirm it is gone — an idle server is a recurring charge for nothing.

- [ ] **Step 4: Record the result**

In the findings note: fill the `hetzner-fsn1` row of the table, and add a `### hetzner-fsn1` section under "Raw output" with the JSONL pasted verbatim. Include the `cf_ray` and `cf_mitigated` values — `cf_mitigated: challenge` is Cloudflare telling you exactly what happened.

- [ ] **Step 5: Commit**

```bash
git add docs/notes/2026-09-11-hosted-ip-findings.md
git commit -m "docs: Phase 0 measurement from Hetzner"
```

---

## Task 4: Second cloud vantage point — Fly.io (conditional)

**Run this task only if Task 3 was NOT `served`.** One blocked host is not a hosting verdict; two blocked hosts from different providers is. If Hetzner was served, skip to Task 5 and note the skip.

Fly.io is a useful second point because its egress IPs are a different class (it fronts through its own anycast edge) and it is the other cheap candidate in the spec.

**Files:**
- Modify: `docs/notes/2026-09-11-hosted-ip-findings.md`

- [ ] **Step 1: Run the probe from a throwaway Fly machine**

Requires `flyctl` installed and `fly auth login` done by the owner.

```bash
fly machine run python:3.12-slim \
  --region ams \
  --name probe-$(date +%s) \
  --rm \
  --file-local /root/probe_access.py=scripts/probe_access.py \
  -- python3 /root/probe_access.py --label fly-ams
```

`--rm` destroys the machine when the command exits. If `--file-local` is not supported by the installed `flyctl` version, fall back to `fly machine run ... -- sh -c 'curl -sO https://raw.githubusercontent.com/saler-luka/price-watcher/main/scripts/probe_access.py && python3 probe_access.py --label fly-ams'` — this requires the probe to be pushed first, which Task 1's commit does.

Expected: two JSON lines, either verdict. Record exactly.

- [ ] **Step 2: Confirm nothing is left running**

```bash
fly machine list
```

Expected: no `probe-*` machine listed.

- [ ] **Step 3: Record the result and commit**

Fill the `fly-ams` row and raw section in the findings note.

```bash
git add docs/notes/2026-09-11-hosted-ip-findings.md
git commit -m "docs: Phase 0 measurement from Fly.io"
```

---

## Task 5: Interpret, decide, and unblock Phase 1

The measurement is worthless until it becomes a decision someone can act on.

**Files:**
- Modify: `docs/notes/2026-09-11-hosted-ip-findings.md` (Interpretation section, status)
- Modify: `docs/specs/2026-09-09-price-watcher-saas-design.md` (§13 Amendments)
- Modify: `HANDOFF.md` (status table, "Do this first" section)

- [ ] **Step 1: Write the interpretation into the findings note**

Replace the `## Interpretation` placeholder with the matching paragraph. Exactly one of these three applies:

**Outcome A — served from Hetzner:**

```markdown
## Interpretation

**Datacenter IPs are served.** The pessimistic host (Hetzner, whose ranges
are widely fingerprinted) received the real product page for both products.
The 403 in the original handoff was specific to that build environment's IP,
not to datacenter IPs in general.

**Decision:** proceed with spec decision 7 unchanged — one container on
Hetzner (or equivalent) + Postgres. No residential egress, no local-agent
tier needed for these shops.

**Caveat carried forward:** this is one shop. Other shops may block. The
service's per-domain `blocked` status (spec §9 "Graceful degradation") is
still required; this result means it is an edge case, not the common path.
```

**Outcome B — blocked from Hetzner, served from Fly:**

```markdown
## Interpretation

**Blocking is provider-specific, not datacenter-wide.** Hetzner's ranges are
refused; Fly.io's are served. The shop (or Cloudflare on its behalf) is
scoring IP reputation, and the cheapest hosts have the worst reputation.

**Decision:** amend spec decision 7 to name Fly.io as the host. The
architecture is unchanged; the provider is now a constraint, not a
preference. Re-run this probe if the provider ever changes.

**Caveat carried forward:** IP reputation drifts. A host that is served
today may be blocked in six months. The per-domain `blocked` status and the
local-agent fallback in spec §9 move from "edge case" to "expected
eventually" — build them in Phase 3, not later.
```

**Outcome C — blocked from both:**

```markdown
## Interpretation

**Datacenter IPs are not served by this shop.** Two providers of different
classes were both refused. The hosted-crawler shape does not work for the
shop that motivated the product, and there is no reason to assume it works
for comparable small shops behind Cloudflare.

**Decision:** this changes the product shape, and the choice is the owner's,
not this note's. The options, in the order the spec's §9 prefers them:

1. **Local-agent-first.** The user runs the checker on their own machine
   (the existing CLI already does this); the hosted service holds the
   watchlist, the registry, and notifications, and receives results rather
   than fetching. Defensible, no evasion, and the code exists.
2. **Residential egress.** A paid residential proxy for the fetcher. Works,
   but changes the cost model and moves the legal posture from "polite
   scraper" to "deliberately evading an access control." The spec's §9
   explicitly declines this.
3. **Ask the shop.** Email tennis-zone.hr for an allowlist entry. Cheap to
   try, zero-risk, and the most likely to succeed for a small shop that
   robots.txt already shows welcomes crawlers.

**Phase 1 is NOT unblocked until the owner has chosen.** Tasks 1, 2, 4, 5
and 6 of the Phase 1 plan are pure and can proceed regardless; Tasks 7-12
assume a fetching server and must wait.
```

Set the note's `**Status:**` line to `complete`.

- [ ] **Step 2: Record the decision in the spec**

Append to §13 of `docs/specs/2026-09-09-price-watcher-saas-design.md`, after the 2026-09-10 entry:

```markdown
### 2026-09-11 — Phase 0 result: hosted-IP access

Measured per `docs/notes/2026-09-11-hosted-ip-findings.md`. Outcome: **(A / B / C)**.

(One paragraph: what was decided, and which spec decision it confirms or
amends. For A: "Decision 7 confirmed unchanged." For B: "Decision 7 amended:
host is Fly.io." For C: "Decision 7 suspended pending the owner's choice
between local-agent-first, residential egress, or asking the shop; see the
findings note.")
```

This is the last entry the "amendments before implementation" exemption in §13 admits. Phase 1 begins after this commit, and the append-only rule applies literally from then on.

- [ ] **Step 3: Update the handoff**

In `HANDOFF.md`:

- Status table, **Next action** row → for A or B: `Phase 1, Task 1 of docs/plans/2026-09-09-phase-1-crawler-and-registry.md`. For C: `Owner must choose a product shape — see docs/notes/2026-09-11-hosted-ip-findings.md. Phase 1 Tasks 1, 2, 4, 5, 6 may proceed meanwhile.`
- Add a row: `| **Phase 0** | Done 2026-09-11. Outcome (A/B/C): one line. Evidence in docs/notes/. |`
- In "Do this first: Phase 0", replace the body with two lines: that it is done, and where the result lives. Keep the heading so old references still resolve.
- Tree diagram: add `docs/notes/2026-09-11-hosted-ip-findings.md` and `scripts/probe_access.py`.

- [ ] **Step 4: Run the full check and commit**

```bash
python -m unittest tests.test_probe_access -v
git add docs/ HANDOFF.md
git commit -m "docs: Phase 0 complete - hosted-IP decision recorded"
git push
```

Expected: 8 tests OK; commit and push succeed; `git status` clean.

---

## Self-Review

**Spec coverage.** §9 "Blocked before we start" → Tasks 2-4 measure it, Task 5 answers it. §9 "Graceful degradation, not evasion" → the probe sends no tricks (Task 1 constraints) and Outcome C in Task 5 lists only the spec's sanctioned responses. §11 Phase 0 row "Validate hosted-IP access. No code." → the probe is a throwaway script, not service code; the spec's "no code" means no *application* code, and a tested measurement tool is in keeping with the spirit. §13 → Task 5 Step 2 closes the amendment window.

**Placeholder scan.** The findings note template contains `(fill from Step 1)`, `(paste ... here)` and `(written in Task 5)` — these are **fields in a document the executor fills with measured data**, not plan placeholders; the plan tells the executor exactly what goes in each and when. Task 5's Outcome A/B/C paragraphs are complete text, one of which is copied verbatim.

**Type consistency.** `classify` has the same signature in Task 1's test and implementation. The five verdict strings (`served`, `blocked`, `challenge`, `not_product_page`, `error`) are used identically in `classify`, `probe`, `main`, the tests, and the findings template. `--label` values `home`, `hetzner-fsn1`, `fly-ams` match between the run commands and the note's table rows.

**One thing this plan deliberately does not do:** decide the outcome in advance. Three of the five tasks have branches that depend on measured data, and Task 5 refuses to unblock Phase 1 under Outcome C without the owner. A plan that assumed Outcome A would be building on the exact assumption Phase 0 exists to test.

---

## Execution Handoff

Plan complete. Note the division of labour: **Task 1 is agent work**, **Tasks 2-4 need the owner** (a home machine, a Hetzner account, a payment method, SSH), **Task 5 is agent work on the owner's data**.

Two execution options:

1. **Subagent-Driven** — a fresh subagent for Task 1 and Task 5, with the owner running 2-4 in between.
2. **Inline** — Task 1 now in this session; then pause for the owner's measurements; then Task 5.
