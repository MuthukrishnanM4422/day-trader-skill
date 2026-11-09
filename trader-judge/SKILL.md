---
name: trader-judge
description: Rules on the bull and bear cases (decide), reviews whether past BUY calls made money after costs (review), proposes an entry/stop/target plan from the chart (plan). Writes trader/state/judge.json. Can only block, never relax a gate.
---

# Trader Judge: the verdict

`judge.py` reads `trader/bull.json` and `trader/bear.json` and rules on the
trade. Three commands:

```sh
python3 trader-judge/judge.py decide                      # read the two cases, rule
python3 trader-judge/judge.py plan RELIANCE.NS --market IN  # propose entry/stop/target
python3 trader-judge/judge.py review                      # did past BUY calls make money?
```

## decide: the rules

Every threshold lives in `trader/config.json` (`judge_bull_min` 65,
`judge_bear_max` 35, `judge_gap_min` 25, `judge_wait_*` for the WAIT band):

- **BUY** - bull >= 65, bear <= 35, gap >= 25, no hard veto, replay
  expectancy after costs >= `min_net_expectancy_r` (0.05R), both files fresh
  and for the same plan fingerprint and date.
- **WAIT** - borderline: bull >= 55, bear <= 45, gap >= 10. Not a trade.
- **NO-BUY** - everything else, any veto, or stale/mismatched files.

`decide` writes `trader/state/judge.json` (plus a per-symbol copy under
`trader/state/judges/`), appends to `trader/state/judge_log.json`, prints
the verdict and a plain-words explanation, and exits 0 only on BUY.

## The one thing it can do

The Judge can only **add** a block. `trader-risk/check.py` reads the verdict
when `"require_judge": true` in config (the default under market IN) and
blocks anything that is not a BUY. The Judge never turns a BLOCK into a
PASS, never relaxes another gate, and your "yes" never overrides it.

## plan: propose a setup

`judge.py plan SYMBOL` pulls the last 60 days of 5-minute bars, sets the
stop at 1x the 5-minute ATR below the entry and the target at 1.5R, rounds
to the tick, and writes a plan the rest of the pipeline can score.

## review: the honest scorecard

`judge.py review` replays the decision log against recorded fills and
reports how many BUY calls made money after costs. A handful of trades
proves nothing either way; it says so.

## Flags and files

- `--market IN` switches the whole chain to the NSE: IST dates, rupee costs,
  the 30-minute opening range and the India charge model inside review.
- `decide --symbol T` rules on one symbol only (the per-symbol file under
  `trader/state/judges/` wins over the shared one when both exist).
- `review --days N` and `--json` shape the scorecard.
- Everything reads from `trader/` and writes only `trader/state/` - no
  network except `plan`, which fetches 5-minute bars from Yahoo.

## Fine print

The Judge is a gate, not a gun: it can only ever stop a trade, and it stops
it for a written reason you can read in `judge.json`. Nothing here places an
order - trader-broker renders the ticket and waits for your "yes" first,
and a BUY still has to clear trader-risk and your own limits. Education and
tooling, not financial advice.

Run order: entry -> exit plan -> timemachine -> bull + bear -> **judge** ->
budget -> risk -> broker.
