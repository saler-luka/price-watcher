# Price Watcher — multi-user service design

**Date:** 2026-09-09
**Status:** approved design, not yet implemented
**Supersedes:** the roadmap section of `README.md` and the original `HANDOFF.md`

---

## 1. Purpose

Turn the working single-user CLI into a small hosted service where a
non-technical user pastes a product URL from any webshop and gets told when its
price changes.

Two layers:

- **Layer 1 (new)** — figure out where the price lives on a page nobody has
  configured by hand, confirm it once per shop, and remember it.
- **Layer 2 (exists)** — fetch, compare against what the user was last told,
  and notify. Working today; the logic ports over unchanged.

---

## 2. Decisions

All seven were taken deliberately; the rationale matters more than the choice
because it is what to re-examine if a constraint changes.

| # | Decision | Rationale |
|---|---|---|
| 1 | **Scheduled checks + a manual "hit me" override** | Notifications only earn their place when a check happens while the user is away. "Hit me" exists because waiting hours for a first number feels broken. |
| 2 | **Responsive web app (PWA)**, one codebase | Phone and desktop from one build, no app-store review. |
| 3 | **AWS Cognito** for auth | Never store a password; reset/verify flows for free; already familiar. Accepted seam: an AWS dependency for auth only, while the app runs elsewhere. |
| 4 | **All Python** | `parse_price`, the three extraction strategies and the candidate ranker port over *verbatim*, including the cases verified against live pages. Rewriting them would mean re-earning trust in them. |
| 5 | **FastAPI + Jinja + HTMX** | No JS build step, no second language, no API designed against your own client. |
| 6 | **Static HTML first, rendered DOM only on failure** | Amended 2026-09-10; see §13. Most shops put the price in the HTML, so a browser is the exception rather than the default. Escalating on failure also makes `needs_render` a byproduct of what actually happened instead of a guess. |
| 7 | **One container + Postgres** | One deploy, one log stream. SQL makes cross-user URL dedupe and registry queries natural; DynamoDB makes them fiddly. |

### Non-goals for the MVP

Explicitly out of scope, to keep the first implementation finishable:

- Telegram notifications (designed for, built after email)
- Price history charts (the data is recorded from day one; the UI is not built)
- Teams, sharing, or public watchlists
- Payment or subscription handling
- Native mobile apps
- Any attempt to defeat bot protection (see §9)

---

## 3. Architecture

One container runs three things against one Postgres database:

- **web** — FastAPI serving Jinja/HTMX pages and a thin JSON surface
- **scheduler** — picks due products, runs checks, fans reports out to users
- **crawler** — the extraction library, plus Playwright for discovery

Cognito sits outside as the identity provider. Outbound email goes through SMTP
or SES.

Keeping these in one process for the MVP is deliberate: a solo maintainer
debugging one log stream beats three services with a message bus between them.
The seam to split on later is `crawler`, because it is the only part that wants
different resources (memory, a browser binary) from the rest.

---

## 4. Data model

The load-bearing decision: **products are global, watches are per-user.**

```sql
users
  id              uuid pk
  cognito_sub     text unique not null
  email           citext not null          -- requires the citext extension
  alias           text
  timezone        text not null default 'Europe/Zagreb'
  created_at      timestamptz not null default now()

shops                          -- the domain registry, shared by all users
  domain          text pk      -- canonical host, lowercased, no www.
  platform        text         -- 'shopware6' | 'shopify' | 'woocommerce' | null
  selector        text         -- CSS selector for the single-unit price
  price_source    text not null default 'selector' -- 'selector'|'jsonld'|'meta'
  currency        char(3) not null
  decimal_style   text not null            -- 'comma' | 'dot'
  needs_render    boolean not null default false
  distrusted      jsonb not null default '[]'   -- e.g. ["product:price:amount"]
  status          text not null default 'trusted' -- trusted|flagged|broken
  confirmed_by    uuid references users(id)
  confirmed_at    timestamptz
  confirmed_on    text                     -- the product URL used to confirm
  verified_count  int not null default 1

products                       -- one row per URL, regardless of watchers
  id              uuid pk
  canonical_url   text unique not null
  domain          text not null references shops(domain)
  title           text
  created_at      timestamptz not null default now()

watches                        -- a user's interest in a product
  user_id         uuid references users(id)
  product_id      uuid references products(id)
  label           text
  last_reported_price  numeric(12,2)
  last_reported_at     timestamptz
  active          boolean not null default true
  primary key (user_id, product_id)

price_points                   -- append-only observation log
  id              bigserial pk
  product_id      uuid references products(id)
  price           numeric(12,2) not null
  currency        char(3) not null
  source          text not null
  observed_at     timestamptz not null default now()

checks                         -- one row per fetch attempt; drives throttling
  id              bigserial pk
  product_id      uuid references products(id)
  started_at      timestamptz not null default now()
  finished_at     timestamptz
  status          text not null            -- ok|no_price|blocked|error
  http_status     int
  error           text
```

### Why the baseline lives on `watches`

"Changed since last time" means *since you were last told*. A global
last-seen price would tell a user who joined this afternoon that a racket is
"unchanged", when they have never been told anything about it.

So each watch carries its own baseline, and the delivery gate proven in the CLI
applies per user: **a user's baseline advances only once that user's
notification has actually been sent.** If delivery fails, their baseline is
untouched and the change is reported again next run.

### URL canonicalization

Before insert or lookup: lowercase the host, strip `www.`, drop the fragment,
remove tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, `source`), and
sort any remaining query parameters.

This is not cosmetic. Without it the same racket with a tracking parameter
becomes two `products` rows, and the one-fetch-per-URL guarantee silently
breaks. It also keeps requests off the filtered/parameterised paths that shops
commonly disallow in `robots.txt` (see §9).

---

## 5. Adding a product

```
URL in
  → canonicalize, extract domain
  → look up shops.domain
      ├── found, status='trusted'  → fetch (honouring needs_render)
      │                              extract with stored selector
      │                              parses?  ── yes → create product + watch, done
      │                                       └─ no  → mark shop 'flagged', fall through to discovery
      └── not found                → discovery
```

### Discovery (once per shop)

1. Fetch the static HTML. Only if it yields no usable price candidate,
   re-fetch with Playwright and use the rendered DOM (see "Render
   escalation" below).
2. Detect the platform from fingerprints (for Shopware 6:
   `window.activeNavigationId`, `/bundles/storefront/`, `robots.txt` naming it).
3. Collect every price candidate — JSON-LD, the four `product:*` meta
   properties, and every element whose class mentions `price`. This is the
   existing `--debug-url` logic.
4. Rank them (§6).
5. Present the top three to the user with their parsed values:
   *"I think this costs 248,90 €. Is that the price you see?"*
6. On confirmation, write the `shops` row — selector, currency, decimal style,
   `needs_render`, the platform, and the `distrusted` list of sources that
   disagreed with the confirmed value.

The confirmation step is not optional and not a nicety. Evidence in §10 shows a
crawler trusting structured data would report 223.90 for a racket costing
248.90, and 248.40 for one costing 275.90 — plausible-looking numbers, wrong,
and wrong silently. One human answer per shop converts an unreliable heuristic
into a durable fact.

`needs_render` is not guessed: it records whether rendering was actually
required to find the price. A shop confirmed from static HTML gets
`needs_render = false` and every later poll is a plain HTTP request; only
genuinely JS-only shops pay browser cost repeatedly.

### Render escalation

One rule, used by both discovery and polling: **try the static HTML, and
escalate to a rendered DOM only when the static attempt fails.**

What counts as failure depends on what we are looking for:

- **During discovery** (no selector yet) — failure means the static HTML
  produced *no usable candidate at all*. That is the signature of a
  client-side-rendered price.
- **During polling** (a stored selector) — failure means the stored selector
  did not match, or matched something that does not parse.

Escalation is what distinguishes two failures that look identical but need
opposite responses:

| Static | Rendered | Meaning | Response |
|---|---|---|---|
| fails | succeeds with the same selector | the shop moved to client-side rendering | set `needs_render = true`, carry on, no human needed |
| fails | fails too | genuine selector rot — the shop redesigned | flag the shop, ask for re-confirmation |

The first row is worth the complexity on its own: a shop switching to
client-side rendering would otherwise look exactly like a broken selector and
cost a confirmation round-trip, when it needs nothing from anyone.

Two guards, because a browser launch is expensive:

- **Escalate at most once per check.** Never render twice for one product in
  one run.
- **Once a shop is `flagged`, stop escalating** for it. A permanently dead
  product must not launch a browser twice a day forever.

---

## 6. Candidate ranking

Derived from measured behaviour on real pages, not from first principles.

**Prefer** an element that:

1. is **unique** in the DOM — `p.product-detail-price` matched twice on every
   tennis-zone page (a sticky-header duplicate), `div.product-main-price-display`
   matched once
2. has text parsing to exactly one number — a container holding both price and
   RRP yields `None`, which is a useful rejection signal
3. does **not** depend on a conditional class — `with-list-price` only exists
   while a product is discounted, so a selector using it rots when the discount
   ends
4. is not inside a quantity or bulk-price table

**Trap rule for meta prices.** Before trusting any `product:*` meta value,
check whether a bulk/tiered price block is present in the DOM. If it is, the
meta price is a tiered price and must be rejected — *even when the block renders
no visible rows*. If no such block exists, the meta price is usable. This fits
all three measured products in §10.

**Never** silently fall back to a different source. A shop with a pinned
`price_source` that stops matching reports failure; it does not substitute a
number from somewhere else.

---

## 7. Checking and notification

### Scheduled

1. Select products having at least one `active` watch and no `ok` check inside
   the check interval. **MVP fixes this at 12 hours**, run at 08:30 and 19:30 in
   the user's timezone, matching what the CLI does today. Per-user intervals are
   a later concern; nothing in the schema prevents them.
2. Respect politeness, with concrete limits: **at least 5 seconds between
   requests to the same host**, **at most 4 concurrent fetches** across all
   hosts, and `robots.txt` honoured per domain. These are starting values, and
   the per-host gap is the one to raise if a shop complains.
3. One fetch per product → one `price_points` row. The fetch follows the
   render-escalation rule from §5: static HTML, and a rendered DOM only if the
   stored selector fails to match. A shop whose selector fails statically but
   resolves in the rendered DOM has moved to client-side rendering — set
   `needs_render = true` and continue; it needs no human. A shop that fails
   both ways has genuinely rotted: flag it and stop escalating for it.
4. For each watching user: diff against `watches.last_reported_price`, build
   their report, send it, and advance their baseline only on success.

Report content is the format already validated in the CLI: one message per run
covering every product, with four per-product states — `dropped` / `rose`
(with absolute and percentage delta), `unchanged`, `added to the price watcher`,
and `NO PRICE FOUND`. Failures appear in the notification, not only in logs.

### "Hit me"

Re-checks that user's products immediately, subject to:

- a **cache window** — an `ok` check newer than 5 minutes is reused rather than
  re-fetched
- the same **per-domain throttle** as scheduled checks
- a **per-user quota** (10 presses/hour) so the button cannot be turned into a
  shop-hammering tool

"Hit me" **does not send email**. The report renders in place, with an in-app
notification confirming the run finished and summarising what changed — that
acknowledgement is the whole feedback requirement. It still advances the
baseline, because the user has been told; the next scheduled run will not
re-report what they just looked at.

---

## 8. Auth, and the web surface

Cognito holds credentials; Postgres holds `alias`, timezone and notification
preferences, keyed by `cognito_sub`. The app validates Cognito JWTs and never
sees a password. Registration collects email, password and optional alias —
exactly the three fields specified, nothing more.

Screens, all server-rendered with HTMX for the interactive parts:

| Screen | Contents |
|---|---|
| Sign in / register | email, password, alias |
| Products | the main screen: label, current price, last change, last checked |
| Add product | one URL field; swaps in the confirmation panel when discovery is needed |
| Report | the result of a "hit me", rendered in place |
| Settings | alias, notification channel, timezone |

---

## 9. Risks and required validations

### Blocked before we start: does a hosted IP get served at all?

`tennis-zone.hr` returned **403** to the original build environment's
datacenter IP and **200** to a home connection with no special headers. A
crawler on Fly.io or Hetzner is a datacenter IP. The hosted product may
therefore be blocked on the exact shop that motivated it, while the local
script keeps working.

**This must be measured before any application code is written.** Spin up the
cheapest candidate instance, request one product page, destroy it. Ten minutes.
The outcome changes hosting, or requires residential egress (which changes both
the cost model and the legal posture), or bounds which shops are viable.

Separately measured: `bike24.de` and `notebooksbilliger.de` returned 403 to a
**residential** IP, so some shops refuse unknown clients regardless of origin.
Routing through users' own connections would not have rescued those.

### Graceful degradation, not evasion

The service does not try to defeat bot protection. Instead:

- track per-domain block status; when a shop refuses the server, say so plainly
  in the UI rather than failing silently
- for such shops, offer the existing CLI as an opt-in **local agent** the user
  runs on their own machine, fetching their own products from their own IP —
  the defensible version, and the code already exists
- where a shop matters enough, ask it. `tennis-zone.hr`'s `robots.txt` already
  permits product pages; a price tracker sending buyers their way is not a
  threat, and an allowlist entry costs them nothing

### Legal posture

`robots.txt` compliance is necessary, not sufficient. Scraping arbitrary shops
for a **paid** service is a materially different posture from a personal
script, and needs a real answer before anyone is charged. Recorded here as a
required decision, not a technical task.

### Registry trust in a shared world

One user's confirmation decides correctness for everyone watching that shop —
that is exactly what makes the second user free, and it needs a guard: a
confirmation that **disagrees** with a stored selector sets `status='flagged'`
for review rather than overwriting it.

### Selector rot

Shops redesign; this is maintenance, not a design flaw. A redesign has a
signature a single product cannot show: **every product on the domain fails at
once.** One product failing is a dead listing. All of them failing is a
template change — re-inspect, re-confirm, and touch no baselines meanwhile.

### Smaller ones

- Playwright's container image is roughly 1 GB
- Cognito is region-locked AWS while the app runs elsewhere; a login-path
  dependency, acceptable
- multi-shop means multiple currencies and decimal conventions, which is why
  both are stored per shop

---

## 10. Evidence

Measured on 2026-09-09 against live pages. This is what the design is built on.

### The trap, across three products of one shop

| Product | bulk-price block | `product:price:amount` | visible price | meta correct? |
|---|---|---|---|---|
| Head Coello Pro 2026 | present, with a `2+` row | 223.90 | **248.90** | no |
| Adidas Metalbone 2026 | present, header-only | 248.40 | **275.90** | no |
| Babolat Pure Aero Team | absent | 197.90 | **197.90** | yes |

The Head page also publishes `product:sale_price:amount` 259.40 and
`product:original_price:amount` 354.00, and has no JSON-LD price at all. The
Adidas figure 248.40 is exactly 10% off 276.00 — a tier present in the data but
rendering no row, which is why the bulk block's mere presence is the signal
rather than its contents.

### Selectors are per-theme, not per-platform

A hypothesis that Shopware 6 shops could share selectors was tested and
**refuted**: `0815.eu` is confirmed Shopware 6, and a real product page there
has none of tennis-zone's price classes. Themes restructure price markup
freely.

What does generalize is platform-level *meta* behaviour, which is why
`shops.platform` carries trap rules rather than selectors.

Caveat: one clean counter-example, not five. Product pages could not be located
on three other confirmed Shopware shops, and one sample was invalid. Enough to
abandon the assumption; not enough to characterise the platform.

### Verified in the CLI today

Live fetch (200), pinned selectors correct on two products, digest email
delivered, failed-send holding the baseline and re-firing, scheduled task
returning 0 at 08:30 and 19:30 Europe/Zagreb, and all seven `parse_price`
formats passing.

---

## 11. Implementation phasing

This design is too large for a single implementation plan. It decomposes into
phases that are independently useful; **each gets its own plan.**

| Phase | Scope | Done when |
|---|---|---|
| **0** | Validate hosted-IP access (§9). No code. | We know whether a datacenter IP is served |
| **1** | Domain model, Postgres, the crawler as a library, discovery + ranking, registry writes. No auth, no UI. | Adding a URL and confirming a price works end to end from a test harness |
| **2** | Cognito auth, products list, add-product screen with the confirmation panel | A signed-in user can add a product through the browser |
| **3** | Scheduler, per-user diffing, email notification with delivery-gated baselines | A user gets a correct report without touching anything |
| **4** | "Hit me", cache window, per-domain throttle, per-user quota | Manual refresh works and cannot hammer a shop |
| **5** | Telegram as a second notifier | A user can choose their channel |

Phase 0 gates everything. Phase 1 is the subject of the first implementation
plan.

---

## 12. Deferred questions

Recorded so they are not rediscovered as surprises:

1. What happens when a product page 404s permanently — archive the product, or
   keep notifying that it is gone?
2. Should a user be able to override a shop's registry entry for their own
   watch, or only flag it for review?
3. Does the local-agent tier (§9) authenticate as the user and push results, or
   run fully standalone?
4. Currency conversion — store and display the shop's own currency only, or
   normalise for users watching across currencies?

---

## 13. Amendments

The convention in `docs/README.md` is that approved specs are append-only, and
a changed decision goes into a new dated spec that supersedes the old one. That
rule exists to preserve the record of decisions **that were acted upon**.

Amendments made before any implementation began are recorded here instead,
because spawning a near-duplicate spec for a document nothing was built on
would obscure the record rather than protect it. **Once Phase 1 starts, this
section closes and the append-only rule applies literally.**

### 2026-09-10 — static HTML first, render on failure

Decision 6 previously read "headless browser for discovery, plain HTTP for
polling", which meant every new shop paid for a browser launch whether it
needed one or not. It now reads: try static HTML, escalate to a rendered DOM
only on failure. See §5, "Render escalation".

Consequences:

- Most shops never launch a browser at all. `tennis-zone.hr` serves its price
  in static HTML, so discovery there is a single HTTP request.
- `needs_render` becomes an observation rather than a guess — it is true
  exactly when rendering was required. This closes a gap recorded in the
  Phase 1 plan, where `confirm_and_add` could only ever write `false`.
- A shop that migrates to client-side rendering now self-heals instead of
  presenting as a broken selector.
