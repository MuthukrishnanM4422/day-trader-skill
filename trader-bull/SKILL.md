---
name: trader-bull
description: Scores the case FOR a trade from the pipeline's evidence files (entry trigger, exit plan, time machine, news, analyst, data guard) and writes trader/bull.json for the Judge. Read-only - never touches an order.
---

# Trader Bull: the case for the trade

`bull.py` gathers every signal the pipeline has written and scores the case
**for** taking the trade: did the entry trigger fire, does the exit plan
respect the risk, does the replay show an edge after costs, is the news and
the fundamental picture clean.

Run it after the entry, exit plan and time machine:

```sh
python3 trader-bull/bull.py                    # plan + entry + state -> trader/bull.json
python3 trader-bull/bull.py RELIANCE.NS --market IN   # India, NSE ticker
python3 trader-bull/bull.py --event 'SEBI ASM list'   # a risk event only you know
python3 trader-bull/bull.py --json             # full evidence table on stdout
```

## What it writes

`trader/bull.json`:

```json
{
  "created_by": "trader-bull",
  "symbol": "RELIANCE.NS", "side": "buy", "date": "2026-10-19",
  "score": 72, "plan_fp": "a1b2c3d4e5f6",
  "items": [{"id": "trigger_fired", "claim": "...", "value": "...", "points": 18, "source": "entry.json", "why_it_matters": "..."}],
  "context": {"replay": {"avg_R_net": 0.31, "edge_ok": true}, "news_flags": 0}
}
```

Each `item` carries a claim, the raw value, the points it contributes, the
file it came from, and a plain-words `why_it_matters`. `plan_fp` is the
fingerprint of the plan it scored: the Judge refuses a case built for a
different plan.

## What it does NOT do

- Place orders, or override anything. It is one side of the argument.
- Score the case **against** the trade: that is trader-bear's job.
- Decide: only the Judge (trader-judge) rules on the two cases together.
- Guarantee a winner. A high score means the evidence lined up, not that the
  trade will work. Most setups the pipeline scores still fail: the replay's
  honest answer is usually "no edge".

## How the score works

Every item carries points the evidence can earn, and the total is clamped to
0..100. An item with no file behind it scores zero and says so - missing
evidence is never guessed. The same items at the same values always give the
same score, so the Judge can compare today's case with yesterday's.

The biggest single item is `trigger_fired` (the entry agent's own signal),
then the plan's risk and reward:risk, the time machine's net expectancy, and
small marks for clean news, analyst numbers and the data guard. The exact
weights live at the top of `bull.py`, not hidden in a model.

## Inputs it reads

`trader/entry.json`, `trader/plan.json`, `trader/timemachine.json`,
`trader/state/news.json`, `trader/metrics.json` (optional),
`trader/state/guard.json` (optional), `trader/config.json`. Everything it
cannot find is listed under `"missing"` in the output - check that first
when a score looks off.

## Fine print

`bull.py` is read-only: it writes one file and never touches an order.
Nothing here places a trade - trader-broker renders the ticket and waits
for your "yes" first, and trader-risk and the Judge still stand between a
good score and that ticket. Education and tooling, not financial advice.

Run order: entry -> exit plan -> timemachine -> **bull** + bear -> judge ->
budget -> risk -> broker.
