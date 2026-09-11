# Hosted-IP access findings

**Date:** 2026-09-11
**Status:** in progress — control done, cloud vantage points pending
**Answers:** spec §9 "Blocked before we start: does a hosted IP get served at all?"
**Method:** `scripts/probe_access.py` — one honest request per product page,
no header tricks, no proxy. "Served" requires HTTP 200 *and* the price element
present. A 200 without the price element is classified as a challenge if it
carries a challenge marker, otherwise as "not the product page". A 200 that is
a Cloudflare interstitial therefore counts as a block.

## Vantage points

| Label | Provider / region | Type | Verdict |
|---|---|---|---|
| home | owner's ISP, Zagreb | residential | **served** (both products, exit 0) |
| github-actions-azure | GitHub Actions `ubuntu-latest` (Microsoft/Azure ranges) | datacenter | pending |
| hetzner-fsn1 | Hetzner Cloud, Falkenstein | datacenter | not run — no server; GitHub Actions substituted (see below) |
| fly-ams | Fly.io, Amsterdam (only if Hetzner is not served) | datacenter | pending |

## Vantage-point substitution

The plan's Task 3 names Hetzner. The owner chose not to rent a server, so the
first datacenter vantage point is a GitHub Actions runner
(`.github/workflows/probe-access.yml`, manual trigger only). Azure ranges are
at least as heavily fingerprinted by bot-protection vendors as Hetzner's, so
the test stays pessimistic. **Limit of this substitution:** IP reputation is
scored per provider. A positive result means "this shop serves datacenter IPs
in general"; a negative one means "Azure is refused" and leaves Hetzner
unmeasured. Whichever host is eventually chosen, re-run the probe from it once
before relying on it.

## Raw output

### home

```
{"label": "home", "ip": "78.0.185.192", "url": "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769", "at": "2026-09-11T13:06:58+00:00", "status": 200, "verdict": "served", "server": "cloudflare", "cf_ray": "a396e48b9a22c2de-VIE", "cf_mitigated": null, "body_len": 522448}
{"label": "home", "ip": "78.0.185.192", "url": "https://tennis-zone.hr/padel/reketi/adidas/adidas-metalbone-2026/ST107802", "at": "2026-09-11T13:07:03+00:00", "status": 200, "verdict": "served", "server": "cloudflare", "cf_ray": "a396e4ac7a0fc2b9-VIE", "cf_mitigated": null, "body_len": 533338}
```

Run from the project directory with `.venv\Scripts\python.exe scripts/probe_access.py --label home`.
Both responses: 200, `server: cloudflare`, `cf_mitigated: null`, body ~520 KB
(the real page; a challenge interstitial is a few KB).

## Probe defect caught by the control run

The first control run returned `verdict: challenge` for both products with
`status 200` and `body_len 522448`. The body was saved and inspected: it was
the real product page (title, price element, `248,90`) and matched the bare
`captcha` marker four times — Shopware's feature-flag blob
(`"captcha":true`) and its `js/captcha/captcha.js` form script. No Cloudflare
challenge string was present; the only `cdn-cgi` artifacts were the
email-obfuscation scripts every page carries.

Fix: `classify` now checks for the price element *before* the challenge
markers. An interstitial never carries the shop's own DOM, so the price
element is conclusive; challenge markers only decide the sub-verdict when it
is absent. Regression test:
`test_real_page_that_mentions_captcha_in_a_script_is_served`. This is the
exact failure mode the plan warned about ("if the probe does not say served
from here, the probe is wrong, not the shop"), and it is why the control run
exists.

## Interpretation

(written in Task 5, after all vantage points are measured)
