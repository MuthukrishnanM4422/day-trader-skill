---
name: trader-bear
description: Scores the case AGAINST a trade from the same evidence, plus hard vetoes (stale data, stale filings, corporate events, data guard BAD, plan fingerprint drift). Writes trader/bear.json. Read-only.
---

# Trader Bear: the case against the trade

`bear.py` hunts every reason not to take the trade. It scores the evidence
the same way trader-bull does, but it also checks **hard vetoes**: things
that kill the trade no matter how good the setup looks.

```sh
python3 trader-bear/bear.py                    # -> trader/bear.json
python3 trader-bear/bear.py RELIANCE.NS --market IN
python3 trader-bear/bear.py --event 'SEBI ASM list'   # hard veto if it names the stock
python3 trader-bear/bear.py --now '2026-10-19 10:15'  # freshness checks against this time
```

## The hard vetoes (any one kills the trade)

- **stale_news**: the last news check is too old for an entry.
- **filing_age**: a 10-K/10-Q older than 90 days, or filings never seen (US).
- **event**: an event the bear knows about (results date, ASM/GSM, `--event`)
  lands inside the trade's window.
- **guard_bad**: the data guard says the price data is not trustworthy.
- **plan_fp**: the bear's plan fingerprint does not match the current plan.

Vetoes appear in `bear.json` under `"vetoes"` with the reason and the file
that triggered them. The Judge turns any veto into NO-BUY.

## What it writes

`trader/bear.json`: same shape as `bull.json` (symbol, side, date, score,
`items`, `plan_fp`) plus `"vetoes": [...]` and `"context"`.

## Scoring vs vetoes

The score and the vetoes are independent. A chart can score 0 - the most
dangerous trade on record - and still have no veto, because vetoes are only
about facts that make the evidence itself untrustworthy: stale news, stale
filings, a scheduled event inside the window, corrupt price data, or a plan
the rest of the pipeline has moved past.

A veto is never downgraded to a warning. `--event "results Tuesday"` is
case-insensitive and lands on the market's trading date, not the exchange's
calendar guess. Under `--market IN` the event checks use the NSE calendar
and IST dates; a US session date never sneaks in by accident.

## Inputs it reads

The same files as trader-bull plus `trader/state/broker.json` (fills),
`trader/state/judge.json` and anything `--event` feeds it. Missing files do
not veto on their own - except the guard, whose absence under a strict
config counts as unverified data.

## Fine print

`bear.py` is read-only: it writes one file and never touches an order.
Nothing here places a trade - trader-broker renders the ticket and waits
for your "yes" first, and every veto lives on file in `bear.json` where the
Judge and you can audit it. Education and tooling, not financial advice.

Run order: entry -> exit plan -> timemachine -> bull + **bear** -> judge ->
budget -> risk -> broker.
