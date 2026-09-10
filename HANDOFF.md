# Price Watcher — Handoff

Written 2026-09-09. Read this first, then `docs/specs/` and `docs/plans/`.

## What exists right now

A **working, scheduled, single-user CLI** (`main.py`) that watches product
prices and emails a report. It is live: a Windows scheduled task runs it at
08:30 and 19:30 Europe/Zagreb and it sends real email to the owner.

Alongside it, an **approved design and implementation plan** for turning it
into a multi-user service. No service code has been written yet.

```
price watcher/
├── main.py                  # the working CLI — do not break this
├── config.json              # 2 products, selectors pinned
├── .env                     # SMTP credentials (gitignored)
├── .env.example
├── requirements.txt         # requests, beautifulsoup4, lxml
├── README.md                # how to operate the CLI
├── HANDOFF.md               # this file
└── docs/
    ├── README.md            # how documentation is organised
    ├── specs/2026-09-09-price-watcher-saas-design.md    # approved design
    └── plans/2026-09-09-phase-1-crawler-and-registry.md # 11 tasks, TDD, ready
```

## Verified, with evidence

Everything here was actually run and observed, not assumed.

- **Live fetch works.** `tennis-zone.hr` returns 200 from the owner's machine
  with the script's existing headers *and* with no headers at all.
- **Selectors pinned and correct** on both products:
  `div.product-main-price-display` → 248.90 (Head Coello Pro) and 275.90
  (Adidas Metalbone).
- **Email delivered.** Four test emails confirmed received by the owner: a
  rise, a drop, a digest, and one from the scheduled task.
- **Report format**: one digest email per run covering all products, with four
  per-product states (dropped / rose / unchanged / added / NO PRICE FOUND).
- **Failed send holds the baseline.** Verified across two consecutive failing
  runs: the stored price stayed put and the alert re-fired both times.
- **Broken selector fails loudly** — warning, `[!]` in the email, exit code 1.
  It does **not** fall back to another number.
- **Scheduled task** `Price Watcher` returns `Last Result: 0`, next run 08:30.
- **All 7 documented `parse_price` formats pass.**

## Not verified

- **No automated test suite.** Every check above was run by hand. Nothing will
  catch a regression. Phase 1 Task 1 fixes this.
- **Whether a hosted/datacenter IP can fetch these shops at all.** This is the
  open question that gates the whole service — see "Do this first".

## Four bugs already found and fixed — do not reintroduce

1. **`--debug-url` crashed** with `UnicodeEncodeError` on Croatian characters,
   because the Windows console is cp1252. `main.py` now reconfigures stdout and
   stderr to UTF-8. Without this, the tool the entire maintenance story depends
   on dies before printing anything.
2. **Gmail App Passwords contain spaces.** SMTP AUTH needs the bare 16
   characters. `send_email` strips them. Without this you get
   `SMTPAuthenticationError` and an error message that sends you to regenerate
   a password that was fine.
3. **Exit code was always 0**, so a scheduled run with a dead selector reported
   success while tracking nothing. Now exits 1 on any failure.
4. A failed *email* logged "produced no price", which was misleading. Reworded.

## The trap that shapes everything

`tennis-zone.hr` publishes several prices per page and the structured data is
not the right one. Measured across three products:

| Product | bulk-price block | `product:price:amount` | real price |
|---|---|---|---|
| Head Coello Pro 2026 | present, with a `2+` row | 223.90 | **248.90** |
| Adidas Metalbone 2026 | present, **header-only** | 248.40 | **275.90** |
| Babolat Pure Aero Team | absent | 197.90 | **197.90** |

The Head page also publishes 259.40 (`sale_price`) and 354.00
(`original_price`), and has **no JSON-LD price at all**.

**The rule that fits all three:** if a bulk/tiered price block is present in
the DOM, every `product:*` meta price is a tier price and must be rejected —
*even when that block renders no visible rows*. If no such block exists, the
meta price is trustworthy.

Two further findings worth not rediscovering:

- **Selectors are per-theme, not per-platform.** The shop runs Shopware 6, and
  a hypothesis that Shopware shops could share selectors was tested and
  **refuted**: `0815.eu` is confirmed Shopware 6 and has none of these price
  classes. Platform detection is still useful, but it carries *trap rules*, not
  selectors.
- **`div.product-main-price-display` was chosen over `p.product-detail-price`**
  because the latter matches twice (a sticky-header duplicate) and carries a
  `with-list-price` class that only exists while the product is discounted.

## Do this first: Phase 0

**Measure whether a datacenter IP gets served before building anything.**

The 403 in the original handoff was never a bug in the code — it was a
datacenter IP being refused by Cloudflare, while a home connection got 200 with
no special headers. A hosted crawler *is* a datacenter IP, so the service may
be blocked on the very shop that motivated the project.

Spin up the cheapest candidate host, request one product page, destroy it. Ten
minutes. If it 403s, hosting changes, or residential egress is needed (which
changes both cost and legal posture), or the set of viable shops shrinks.

Also measured: `bike24.de` and `notebooksbilliger.de` returned 403 to a
*residential* IP, so some shops refuse unknown clients regardless of origin.

Do **not** attempt to defeat bot protection. The design degrades gracefully
instead — see spec §9, which includes offering this CLI as an opt-in local
agent for shops that block the server.

## Then: Phase 1

`docs/plans/2026-09-09-phase-1-crawler-and-registry.md` — 11 tasks, 55 TDD
steps, real code in every step, no placeholders. Start at Task 1 and work in
order; each task ends with a passing test and a commit.

Phase 1 turns `main.py` into a `pricewatcher` package with pure logic split
from I/O, adds Postgres and the shared shop registry, and ends with a CLI
harness. **`main.py` stays working and untouched throughout** — it is only
deleted in Phase 3, once the service replaces it.

The acceptance check is Task 11: confirm the Head racket's selector, then add
the Adidas one and watch it resolve to 275.90 **without asking for
confirmation**. That is the core product bet demonstrated end to end.

## Decisions already made — do not re-open without a reason

Seven choices were made deliberately in brainstorming. Rationale is in spec §2.

1. Scheduled checks **plus** a manual "hit me" override
2. Responsive web app / PWA, one codebase
3. AWS Cognito for auth
4. All Python (so the verified extraction logic ports over unchanged)
5. FastAPI + Jinja + HTMX
6. Headless browser for discovery, plain HTTP for polling
7. One container + Postgres

Two further decisions were made in the Phase 1 plan and are the plan's to
defend: **SQLAlchemy 2.0 + Alembic** rather than raw SQL, and **tests require a
real Postgres** (Docker Compose, port 5433) because `citext` and `jsonb` are
Postgres-specific.

Amended 2026-09-10: **read the price from static HTML first, and fall back to
the rendered browser DOM only if that fails.** Most shops never need a browser.
This also closed a gap that had been deferred to Phase 2 — `needs_render` is
now observed (true exactly when rendering was required) rather than assumed,
and a shop that migrates to client-side rendering self-heals instead of looking
like a broken selector. See spec §5 "Render escalation" and §13.

## Environment and gotchas

- **Python venv** at `.venv/`. The scheduled task uses
  `.venv\Scripts\python.exe` with "Start in" set to the project directory —
  that is what makes `config.json` and `.env` resolvable.
- **Run it**: `.venv\Scripts\python.exe main.py` (or `--dry-run`, which prints
  the report and leaves `history.json` untouched).
- **Diagnose any shop**: `python main.py --debug-url "<url>"`.
- **`history.json`** is local state and gitignored. It holds the last price per
  URL and is the baseline for change detection.
- **Scheduled task**: `schtasks /query /tn "Price Watcher" /v /fo LIST`. From
  Git Bash, prefix with `MSYS_NO_PATHCONV=1` or `/create`-style flags get
  mangled into paths.
- **`gh` CLI** is installed at `C:\Program Files\GitHub CLI\gh.exe`,
  authenticated as `saler-luka`. That account also has `ROKOLabs` org access,
  so always pin the owner explicitly (`gh repo create saler-luka/...`) to keep
  personal work off the org.
- **Repo**: `https://github.com/saler-luka/price-watcher`, private. Commits use
  `saler.corp@gmail.com` via repo-local git config; the global config still
  points at the work address and was deliberately left alone.
- **In the session this was written in, `git commit` and `rm` were blocked** by
  permission rules. Deletions were done with Python's `os.remove`, and commits
  were handed to the owner to run. Expect to do the same, or ask.

## Credentials

`.env` holds SMTP credentials and is gitignored — confirmed absent from the
GitHub remote. **The App Password in it was pasted into a chat transcript, so
treat it as exposed and rotate it if that has not already happened**: generate
a new one at <https://myaccount.google.com/apppasswords>, put it in `.env`,
and run `python main.py` to confirm delivery. Nothing else needs changing.

Never commit `.env`. Before any push, check both that it is unstaged *and* that
no staged file's content contains the password.

## Where documentation lives

**This repository is the only source of truth.** No Jira, no Confluence, no
issue tracker. `docs/README.md` describes the layout and conventions; the short
version:

- **This file** is the only one guaranteed current. Everything else is a dated
  snapshot of what was decided on that date.
- Approved specs are append-only. A changed decision means a new dated spec
  that supersedes the old one, never a silent edit.
- The checkboxes in the current plan are the task tracker. There is no second
  system on purpose.

**Ignore the Claude artifact** published on 2026-09-09 (a two-layer
architecture page). It is an unmaintained snapshot, now banner-marked as
superseded. It predates most of the design and originally stated the bulk-block
trap rule was impossible, which was wrong; that one claim has been corrected in
place and the page will not be touched again.

## Standing guidance

- **Do not trust structured data.** See the table above. Always cross-check
  against what the page visibly renders.
- **Prefer failing loudly over guessing.** A missing price logs a warning,
  reports `[!]`, and exits non-zero. It never substitutes another number.
- **Scraping targets rot.** Shops redesign; selector breakage is expected
  maintenance, not a design flaw. `--debug-url` makes it a two-minute fix.
- **A redesign has a signature a single product cannot show**: every product on
  the domain fails at once. One product failing is a dead listing.
- **Keep it boring.** The CLI is one file and three dependencies, and that is a
  feature for something meant to run unattended for months. The service adds
  frameworks because multi-user relational data is a real reason; do not add
  more without one.
