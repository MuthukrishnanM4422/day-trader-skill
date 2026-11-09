---
name: trader-guard
description: >-
  Data Guard. Checks the market data right before an order, because the free
  Yahoo feed can lag, freeze, skip bars or print a bad price. For each symbol
  it checks that the market is open, the last 1-minute bar is fresh, the bars
  are sane (no missing or negative prices, no 20 % jumps, no frozen or halted
  feed, USD only) and that Stooq's last quote agrees with Yahoo's within a
  tolerance. It records OK or BAD per symbol in trader/state/data_guard.json,
  and trader-risk blocks any entry whose symbol is not OK within the last few
  minutes. When it cannot confirm the data, the answer is BAD, not "probably
  fine". Use when the user says "is the data good", "check the data", "is
  this price right", "is the feed stale", "check TSLA before I buy", or
  before /trader-risk, always.
argument-hint: "[ticker ...]"
---

# trader-guard

The Data Guard is the first gate on the way to an order. The rest of the
pack reads free, unofficial data. A stale quote, a frozen feed or a bad
print can turn a sensible-looking trade into a loss before any rule gets to
act. This skill does one job: decide whether the data is good enough to act
on, and say so in a file that trader-risk reads. It never places anything and
never edits an order. Most people who day trade lose money, and bad data is
one avoidable way to lose it faster.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/guard.py" data TSLA                   # check, print, record
python3 "${CLAUDE_SKILL_DIR}/guard.py" data TSLA AAPL              # several symbols, one request each
python3 "${CLAUDE_SKILL_DIR}/guard.py" data TSLA --json            # the verdict as JSON
python3 "${CLAUDE_SKILL_DIR}/guard.py" data TSLA --no-write        # look without recording
python3 "${CLAUDE_SKILL_DIR}/guard.py" show                        # what is recorded, and how old
```

`--now "2026-10-19 10:15"` pins the clock (New York time). `--yahoo-file` and
`--stooq-file` read saved feeds for one symbol, with no network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | `data_guard_max_age_min` (3), `data_guard_tolerance_pct` (1.0), `require_second_source` (true) |
| `trader/state/data_guard.json` | the verdict per symbol, with the time of the check, written here and read by trader-risk |

Network: Yahoo Finance's 1-minute chart for today, and Stooq's one-line CSV
quote. Both are free and unofficial. The guard is polite (a pause between
symbols) and falls back from query2 to query1.

## The checks

Any failed check is a reason, and any reason makes the verdict BAD.

| check | what fails it |
| --- | --- |
| market | the regular session is not open now (Yahoo's own trading period for today) |
| fresh | the last bar started more than `data_guard_max_age_min` plus one minute ago; the last 5 bars identical with no volume (a frozen feed) |
| sane bars | a missing, zero or negative price; a high below the low; an open or close outside the bar; a jump of more than 20 % from one bar to the next (a bad print or an unadjusted split); a currency that is not USD; data for a different symbol |
| halt | no volume in the last 3 bars: a trading halt or a dead feed |
| second source | Yahoo and Stooq differ by more than the tolerance, Stooq's quote is from another day, or Stooq cannot be reached while `require_second_source` is true |

The price it records is the higher of the two sources, so a lagging quote
cannot make a buy look cheaper than it is.

## Steps

1. Run `guard.py data SYMBOL` for the symbol about to be traded, right after
   the entry timer says it triggered and before trader-budget and trader-risk.
2. If it prints OK, carry on. The record is good for `data_guard_max_age_min`
   minutes: after that, run it again. trader-risk checks the age itself.
3. If it prints BAD, read the reasons to the user in plain words. Do not
   work around it: no other source, no `--single-source`, no editing the
   file. Wait and check again, or skip the trade. A BAD is a reason not to trade.
4. `--single-source` drops the Stooq cross-check and is weaker. Use it only
   if the user asks and Stooq is down, and say plainly that the data was
   confirmed by one free feed only. trader-risk still blocks when the config
   has `"require_second_source": true`.

## Rules

- The guard decides only whether the data is good. It does not say whether
  to trade. An OK is not a recommendation and not the user's yes.
- It never writes an OK it did not earn: no data, no second source, a stale
  bar, a closed market and a failed request are all BAD.
- Free feeds can still be wrong together or both lag by a few seconds. The
  broker's price is the one that fills.

## What it never does

It never places an order, never edits `trader/orders.json`, never loosens a
limit, and never turns a BAD into an OK. A yes from the user does not
override it: the user fixes the data problem or skips the trade.

## Safety

- Education and tooling, not financial advice. An OK says the data looks right, not that a trade is wise.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
