---
name: trader-dashboard
description: One self-contained HTML page that says which trade to choose today and walks you through it step by step. build writes trader/dashboard.html; serve hosts it on 127.0.0.1 only. Display only - it can never place an order.
---

# Trader Dashboard: which trade today, step by step

`dashboard.py` rolls every agent's state file into one self-contained HTML
page. No frameworks, no build step, no CDN, no network calls - it opens
with a double-click.

```sh
python3 trader-dashboard/dashboard.py build              # write trader/dashboard.html
python3 trader-dashboard/dashboard.py build --watch      # rebuild every 30s while open
python3 trader-dashboard/dashboard.py serve              # 127.0.0.1:8765, localhost only
python3 trader-dashboard/dashboard.py build --market IN  # force the India profile
```

## What the page shows

- **Top bar**: live market-clock (IST under --market IN), market open/closed
  and the next open, PAPER/LIVE badge, and the honesty banner.
- **The answer**: the ranked candidates. A BUY shows entry, stop, target,
  shares, rupees invested, max loss, round-trip costs and reward:risk.
  Otherwise the page says "No trade today is a valid answer" and why.
- **Bull vs Bear**: both score bars, the top reasons each way, any hard
  vetoes, and the Judge's explanation in plain words.
- **Step-by-step guide**: the fifteen steps of a disciplined trade - done,
  now, or blocked. A blocked step shows the exact block message and what to
  do (usually: skip the trade). Only the current step is open.
- **Safety gates**: every limit with its live value, and the live-lock
  progress bars (30 paper days, 50 closed trades, profit factor 1.3).
- **Record**: closed paper trades, win rate and net after costs, a tiny
  equity curve, and the Judge's own review.
- **Manual order sheet** (when the Judge says BUY): the three orders to
  copy into your broker app - MIS entry, SL-M stop, LIMIT target, flat-by
  time, and the cancel-the-other warning.
- **Plain words and honest limits**: a glossary plus what the tool cannot
  do.

## What it can never do

The page only displays. It holds no keys, sends nothing, and the only
actions on it are copy buttons and a theme toggle. If a file is missing the
panel says which agent produces it and the exact command to run.

## Files it reads

The dashboard is a view over the pipeline's state, nothing more:
`trader/plan.json`, `trader/entry.json`, `trader/timemachine.json`,
`trader/bull.json`, `trader/bear.json`, `trader/state/judge.json`,
`trader/orders.json`, `trader/state/risk.json`, `trader/state/broker.json`,
`trader/portfolio.json`, `trader/watchlist.json`, `trader/config.json` and
`trader/state/guard.json`. A missing file is a muted panel, not an error.

`build` writes `trader/dashboard.html` with everything inlined - no
scripts fetched, no fonts fetched, works offline. `serve` binds 127.0.0.1
only and serves the folder read-only; there is no API and nothing to poke.

## Market profiles

`--market IN` (or `"market": "IN"` in config) swaps the page to the India
view: IST clock, rupee formatting, NSE session times and the manual order
sheet panel. The US view is the default and is unchanged - dollars, New
York time, the Alpaca tool-input block.

## Fine print

The page is a mirror: if it says BUY, the trade still goes through
trader-risk, the ticket and your "yes" like always - the dashboard cannot
short-circuit that. Numbers on the page are the pipeline's own files, and
stale files are marked stale rather than hidden. Education and tooling, not
financial advice.

Run order: the dashboard sits after everything - run it whenever you want
to see where the pipeline stands.
