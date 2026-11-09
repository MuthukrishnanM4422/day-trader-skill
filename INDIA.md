# Day trading on the NSE (India)

The same sixteen skills, pointed at India instead of the US. One flag -
`--market IN` on any tool, or `"market": "IN"` once in `trader/config.json` -
swaps every clock to IST, every session to 9:15 AM to 3:30 PM NSE time, every
price to rupees, every ticker to the NSE spelling, and the broker ticket to a
manual order sheet you place in your own app.

**Not financial advice.** Same rule as the rest of this pack: software that
reads public data and does arithmetic. Most people who day trade lose money.
Indian intraday equity has extra rules worth knowing before you start - read
to the end.

## Setup

1. Copy the folders next to each other (`lib/` must sit beside the `trader-*`
   folders - it holds the shared market profile the tools load at runtime):

   ```bash
   cp -r day-trader-skill/trader-* day-trader-skill/lib ~/.claude/skills/
   ```

2. Copy the India config and edit the rupee limits:

   ```bash
   cp trader-budget/config.india.example.json trader/config.json
   ```

   Every `*_usd` limit also answers to a `*_inr` name, so
   `max_loss_per_trade_inr` and `budget_inr` work exactly as written.

3. The Judge gate is ON under India by default (`require_judge` auto): a BUY
   from `/trader-judge` is required before trader-risk will pass an entry.

## The trading day, IST

| time | what happens |
| --- | --- |
| 9:00-9:08 | pre-open auction (not tradable through MIS) |
| 9:15 | regular session opens |
| 9:15-9:45 | opening range the scanner uses (30 minutes, not 15) |
| ~14:30 | `no_entries_after` default - no new trades after this |
| 15:10 | `flat_by` default - be out before the broker's auto square-off |
| ~15:20 | most brokers auto square-off MIS positions; check yours |
| 15:30 | close |

No early closes. `alerts.py clock --market IN` tells you if the NSE is open
and uses the NSE holiday list (which the lib carries for 2025-2027).

## Commands that change under --market IN

```bash
python3 trader-scanner/scan.py universe --market IN      # NSE EQUITY_L.csv -> trader/nse_universe.txt
python3 trader-scanner/scan.py orb --market IN           # 30-minute range, 9:45 first entry
python3 trader-alerts/alerts.py quote RELIANCE --market IN            # Yahoo .NS tickers, rupees
python3 trader-alerts/alerts.py quote RELIANCE --market IN --price RELIANCE=1450.50   # your broker's quote
python3 trader-timemachine/timemachine.py run --market IN --write     # 75 bars a session, INR costs
python3 trader-broker/ticket.py show --market IN         # the manual order sheet
python3 trader-dashboard/dashboard.py build --market IN  # the page in IST and rupees
```

## Data differences to know

- **Universe**: `scan.py universe --market IN` downloads the NSE's
  `EQUITY_L.csv` (series EQ only - equity shares) from
  `archives.nseindia.com`. NSE's site blocks many scripts; if the download
  fails, open nseindia.com -> Market Data -> Securities list, save
  `EQUITY_L.csv`, and rerun with `--offline <folder>`. A starter list of the
  liquid names ships as `trader-scanner/nse_universe.txt`.
- **Quotes**: Yahoo serves `.NS` tickers (`RELIANCE` -> `RELIANCE.NS`),
  free, delayed, unofficial. For anything that matters use your broker's
  price: `alerts.py quote --market IN --price SYM=PX` injects it into the
  checks.
- **Stooq is US-only** - `--source stooq` refuses under IN.
- **SEC tools** (`/trader-news` filings, `/trader-analyst`) do not cover
  NSE names; they skip cleanly. Indian results live on nseindia.com and
  bseindia.com corporate-announcement pages - `/trader-news` still tracks
  headlines and keyword-tags SEBI/RBI/circuit-breaker news.
- **The 6-day 5-minute limit** on free Yahoo bars is the same on the NSE.

## The India cost model

`timemachine`, `judge review` and the dashboard price an intraday round trip
with Zerodha-style equity-intraday charges (in `lib/market_profile.py`,
`india_intraday_costs`):

- brokerage: min(0.03% of turnover, Rs 20) per side
- STT: 0.025% of turnover, sell side only
- exchange transaction: 0.00307% both sides
- SEBI charge: Rs 10 per crore, both sides
- GST: 18% on brokerage + txn + SEBI
- stamp duty: 0.003% of turnover, buy side only
- plus `slippage_bps` (default 5) on both sides, because no model can see
  your fill

These are typical discount-broker numbers, not a quote - check your own
broker's charge sheet.

## The manual order sheet (there is no broker connector)

Under market IN there is no `place_stock_order` call. `ticket.py show`
renders the proposed order as a plain-text **order sheet**: the entry
(product MIS, MARKET or LIMIT at the planned price), then the two exits -
an SL-M (stop-loss market) at the stop and a LIMIT at the target - plus the
flat-by time, the max loss at the stop, and two warnings: your broker will
auto square-off MIS near 15:20 IST, and when one exit order fills you must
cancel the other yourself. The same explicit-yes flow stands: nothing is
marked placed until `ticket.py result --status placed`, and fills still go
in `trader/portfolio.json` via `ticket.py fill`.

If your broker exposes a connector (Zerodha Kite Connect, Dhan, Fyers),
`"broker": "manual"` is what tells every tool to print sheets instead of
tool calls. A future connector would slot in beside it.

## SEBI/intraday rules the pack encodes

- **Cash segment only**: no short selling overnight is possible intraday; MIS
  sells are covered the same day. `allow_short` stays false.
- **No PDT rule** in India: `pdt_guard` is off in the India config. The other
  gates (edge test, drawdown, streaks, live lock) are all unchanged and on.
- **Circuit filters**: stocks have daily price bands (5%/10%/20% by category).
  The data guard and the Bear's vetoes still apply.
- **ASM/GSM lists**: SEBI's surveillance measures can force
  trade-for-trade settlement - check whether your symbol is on one before
  you plan a day trade. `bear.py --event 'ASM list'` hard-vetoes if you flag it.

## What does not change

Your yes. Every order still needs `trader-risk` to pass and you to type yes
for that exact ticket. Paper trading is still the default. The Judge can
only add blocks. No options, margin, short selling or leverage unless you
turn them on - and on the NSE intraday, all four are separate products with
their own rules the pack does not touch.
