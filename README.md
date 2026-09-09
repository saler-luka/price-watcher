# Price Watcher

Checks product pages on a schedule and emails you when a price drops or hits
a target you set.

## Setup (Windows / PowerShell)

Drop these files into `C:\Users\lukas\src\price watcher`, then:

```powershell
cd "C:\Users\lukas\src\price watcher"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
notepad .env          # fill in SMTP_USER and a fresh App Password
```

Then run it:

```powershell
python main.py --dry-run     # check prices, no email
python main.py               # check prices, email on drops
```

If PowerShell blocks the venv activation script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Important: this site publishes four different prices

The tennis-zone.hr page for the Head Coello Pro 2026 exposes four numbers.
Only one of them is the price you actually pay:

| Where | Value | What it actually is |
|---|---|---|
| `product:price:amount` | 223.90 | the **2+ bulk** unit price |
| `product:sale_price:amount` | 259.40 | a sale figure |
| `product:original_price:amount` | 354.00 | RRP / list price |
| visible on page | **248.90** | the real single-unit price |

There is no JSON-LD price on this page at all, so the "obvious" structured-data
field is the one you *don't* want. This is already resolved in `config.json`:

```json
"selector": "div.product-main-price-display",
"price_source": "selector"
```

That element was picked because it is unique on the page and contains only the
price. The other candidate, `p.product-detail-price`, matches twice - there is a
duplicate in the sticky header - and it carries a `with-list-price` class that
only exists while the product is discounted, so it would rot the moment the RRP
came off.

Setting `price_source` stops the script silently falling back to a different
(wrong) number if the selector ever breaks. It reports "no price found" and
exits non-zero instead, which is the failure you want.

When a shop redesigns and the selector breaks, re-run:

```powershell
python main.py --debug-url "https://tennis-zone.hr/padel/reketi/head/head-coello-pro-2026/ST109769"
```

That prints every price candidate on the page - JSON-LD, four meta properties,
and every element whose class mentions "price" - with what each one parses to.
Find the one showing the price you want and update `selector`.

## config.json

```json
{
  "products": [
    {
      "name": "Head Coello Pro 2026 (Tennis Zone)",
      "url": "https://tennis-zone.hr/...",
      "selector": "div.product-main-price-display",
      "price_source": "selector"
    }
  ]
}
```

- `selector` - CSS selector for the price element. `null` to auto-detect.
- `price_source` - force `"selector"`, `"jsonld"`, or `"meta"`. `null` = try
  all three in order. Pin it; see the section above for why.

Add more products by appending objects to the array.

## What it emails

Every run sends **one report email** covering all products, whether or not
anything changed. One email per run, not one per product, so adding products
does not add messages.

Each product gets a line describing its state:

```
Subject: Price watcher: Head Coello Pro 2026 (Tennis Zone) dropped to 248.90 EUR

Head Coello Pro 2026 (Tennis Zone)
  dropped from 260.00 to 248.90 EUR (-11.10, -4.3%)
  https://tennis-zone.hr/...

Adidas Metalbone 2026 (Tennis Zone)
  added to the price watcher, current price 275.90 EUR
  https://tennis-zone.hr/...

Some Other Racket
  NO PRICE FOUND - the selector has probably broken.
  Diagnose with: python main.py --debug-url "https://..."
  https://tennis-zone.hr/...
```

The four possible states are `dropped` / `rose` (compared against the last
price seen for that URL), `unchanged`, `added to the price watcher` for a URL
seen for the first time, and `NO PRICE FOUND` when extraction failed.

The subject line is written to be readable in a phone notification: it names
the change if there is exactly one, counts them if there are several, says
"no changes" when nothing moved, and is prefixed `[!]` if anything failed.

Failures are reported **in the email**, not just in the log, so a broken
selector reaches you the same way a price drop does.

If the report cannot be sent, the stored prices are deliberately **not**
advanced and the run exits non-zero, so the change is reported again on the
next run. Otherwise a drop that happened during an SMTP outage would be lost,
which is the one event the tool exists to catch.

`--dry-run` prints the report instead of emailing it, and leaves
`history.json` untouched - so a dry run can never silently consume a change.

## Scheduling on Windows

Already registered as the scheduled task **Price Watcher**, which runs daily at
**08:30** and **19:30** local time. Task Scheduler uses wall-clock local time,
so these stay correct across the CET/CEST switch.

Useful commands:

```powershell
schtasks /query /tn "Price Watcher" /v /fo LIST   # see next run and last result
schtasks /run   /tn "Price Watcher"               # run it now
schtasks /change /tn "Price Watcher" /disable     # pause it
schtasks /delete /tn "Price Watcher" /f           # remove it
```

The task runs as `lukas` with an interactive token, meaning it only fires while
you are logged on and no Windows password needs storing. To run it headless,
re-create it in the Task Scheduler GUI with "Run whether user is logged on or
not", which does require storing that password.

Two settings that matter on a laptop and are already set: it runs on battery,
and `StartWhenAvailable` means a run missed while the machine was asleep
happens once it wakes rather than being skipped.

The script exits `0` on a clean run and `1` if anything failed - no price
found, request failed, or report undelivered. Task Scheduler surfaces this as
"Last Result", so breakage is visible there as well as in the email.

Do not schedule more often than hourly. Twice a day is plenty, and hammering a
small shop is both rude and the fastest way to get the IP blocked.

## Where this goes next

Right now state lives in `history.json` next to the script. If you want to turn
this into the monitoring micro-SaaS idea, the shape barely changes:

- `history.json` → DynamoDB table, partition key = user_id, sort key = url
- the loop → Lambda on an EventBridge schedule
- `send_email` → SNS, so alerts can fan out to email/SMS/webhook
- add a signup form and a `products` table keyed by user

The scraping and parsing logic is the part that stays identical, and it's the
part that's already done.

## Limitations

- **JS-rendered prices** — if a shop paints the price in with JavaScript, a
  plain HTTP fetch won't see it. `--debug-url` showing no candidates is the
  tell. Fix is Playwright instead of requests, per-site.
- **Bot blocking** — larger retailers (Amazon etc.) fingerprint and block
  scraper traffic. Small and mid-size shops are generally fine.
- **Politeness** — every few hours is plenty. Hammering a small shop's server
  every minute is both rude and the fastest way to get your IP blocked.
- **Selectors rot** — shops redesign. If a product silently stops reporting,
  re-run `--debug-url` and update the selector. Pinning `price_source` makes
  this fail loudly instead of quietly reporting a wrong number.
