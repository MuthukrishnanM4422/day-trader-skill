# The Day Trader skill

16 Claude skills for day trading, US and India (NSE): scan thousands of charts, track breaking news, time every entry and exit, replay every outcome, and let the Bull, Bear and Judge argue it out before you risk a dollar.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, Nasdaq Trader's symbol lists, SEC EDGAR
and the House Clerk.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

Version 3 added a data guard and a set of safety gates that are ON by default:
bad or stale data blocks a trade, a trade with no edge after costs is blocked,
losing streaks and drawdowns stop new entries, and live trading stays locked
until a paper record earns it. See "Very low risk mode" below.

Version 4 turns the chain into a courtroom: a Bull builds the case for the
trade, a Bear builds the case against it (with hard vetoes for stale data and
scheduled events), and a Judge rules on the two files. It can only add a
block, never remove one. And the whole pipeline now runs on India too: NSE
cash equities, IST dates, rupee costs, and a manual order sheet for your own
broker app. See [INDIA.md](INDIA.md).

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* day-trader-skill/lib ~/.claude/skills/
```

(`lib/` holds the shared market profiles - keep it next to the trader-*
folders or the India mode will not load. The US tools work without it.)

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. The SEC tools
(news filings, the analyst) ask you to identify yourself: put your name and
email in `trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`)
or in the `SEC_USER_AGENT` environment variable.

## The sixteen

| command | job | what it does |
| --- | --- | --- |
| `/trader-guard` | Data Guard | Checks the market data right before an order: fresh, sane, and confirmed by a second source. No OK, no entry. |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-bull` | The Bull | Scores the case FOR the trade from every file the pipeline wrote. |
| `/trader-bear` | The Bear | Scores the case AGAINST it, plus hard vetoes for stale data and scheduled events. |
| `/trader-judge` | The Judge | Rules BUY / WAIT / NO-BUY on the two cases; can only block, never relax a gate. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes; prints a manual order sheet under market IN. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads earnings reports and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads Congress's stock-trade disclosures the minute they are filed. |
| `/trader-dashboard` | The Dashboard | One self-contained HTML page: which trade today, every gate, every step. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> guard -> exit -> timemachine -> bull + bear -> judge
       -> budget -> risk -> broker                                            (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                       (on a schedule)
dashboard: one page over all of it                                            (any time)
```

An example, the trade in the reel: Tesla, Monday October 19 2026, real data.
The reel used v2's looser rules with the v3 safety gates off, so its numbers still
reproduce (`tests/fixtures/config.reel.json`). With the shipped v3 config the same
trade is BLOCKED three ways: 8 shares risk $46.88 against a $25 limit, the 1R target
is under the 1.5 reward:risk minimum, and the Time Machine finds no edge after costs.
That is the safety layer doing its job.

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-guard/guard.py data TSLA                                # data OK? -> trader/state/data_guard.json
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-bull/bull.py                                            # the case for -> trader/bull.json
python3 trader-bear/bear.py                                            # the case against -> trader/bear.json
python3 trader-judge/judge.py decide                                   # BUY / WAIT / NO-BUY
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-risk/check.py --status                                  # where the safety gates stand
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year
python3 trader-dashboard/dashboard.py build                            # -> trader/dashboard.html
```

Every tool takes `--market IN` (or `"market": "IN"` in `trader/config.json`) to
run on the NSE instead: IST dates, a 9:15-15:30 session, rupee prices, the
India intraday charge model, `.NS` tickers on Yahoo, and a manual order sheet
instead of a broker connector. See [INDIA.md](INDIA.md).

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: TSLA's 5-minute bars
for the 60 days to October 19 2026, the 10:15 AM opening-range scan, Yahoo's
TSLA headlines that morning, the House Clerk's 2026 index, real PTR text and
real XBRL facts. They reproduce every number in the example above.

## Very low risk mode

Read this first: **no code can make day trading low risk.** Most people who day
trade lose money, and a rules engine only limits how fast. What v3 does is make
the software refuse bad trades and stop itself. The shipped
`trader-budget/config.example.json` turns all of it on:

| gate | what it does | config |
| --- | --- | --- |
| data guard | blocks an entry unless `/trader-guard` found the symbol's data fresh, sane and confirmed by a second source in the last 3 minutes | `data_guard`, `data_guard_max_age_min`, `data_guard_tolerance_pct`, `require_second_source` |
| edge test | blocks an entry unless the Time Machine, AFTER fees and slippage, shows an edge in the replays, in the history and in the most recent third of it | `require_edge`, `min_net_expectancy_r`, `fee_per_share_usd`, `slippage_bps` |
| tight sizing | 0.5 % of the budget at risk per trade, 1 % a day, 2 % a week, 2 trades a day, 1.5 reward:risk, 25 % max position, 20 % cash buffer | `max_loss_per_trade_usd`, `max_daily_loss_usd`, `max_weekly_loss_usd`, `max_trades_per_day`, `min_reward_risk`, `max_position_pct`, `cash_buffer_pct` |
| entry window | no entries in the first 30 minutes or after 3:00 PM, flat by 3:50 PM | `no_entries_before`, `no_entries_after`, `flat_by` |
| loss streak | 2 losing days in a row means no entries for a day | `max_consecutive_loss_days` |
| drawdown stop | realized equity 5 % of the budget below its peak stops all entries until you set a new `drawdown_since` date | `max_drawdown_pct`, `drawdown_since` |
| live lock | `"paper": false` is refused until the paper log shows 30 trading days, 50 closed trades, a profit after costs and a profit factor of at least 1.3 | `live_min_paper_days`, `live_min_paper_trades`, `live_min_profit_factor` |

The realized profit and loss behind the streak, drawdown and live lock comes from
the fills in `trader/state/broker.json`, so record every fill (`ticket.py result`
or `ticket.py fill`). A gate set to 0 or false is off: leave them on. Expect the
edge test to say NO EDGE most of the time. That is an honest answer, not a bug.
If the Time Machine never shows an edge, the safest trade is no trade.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
Alpaca's MCP server defaults to its paper account. Keys live in environment
variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules.** FINRA removed the $25,000 pattern-day-trader minimum
on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change
in until October 20, 2027, so yours may still apply the old rule. If it does,
set `"pdt_guard": true` and `/trader-risk` counts your day trades.

**Congress's trades are public, not inside information.** Members must disclose
trades over $1,000 within 45 days (the STOCK Act). The Pelosi skill reads those
public filings the minute they post.

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-bull/        SKILL.md, bull.py
trader-bear/        SKILL.md, bear.py
trader-judge/       SKILL.md, judge.py, evidence_lib.py
trader-dashboard/   SKILL.md, dashboard.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, config.india.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
trader-guard/       SKILL.md, guard.py
lib/                market_profile.py (US + India shared market definitions)
INDIA.md            the India (NSE) setup guide
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai). v4 adds the Bull/Bear/Judge courtroom, the
dashboard and the India (NSE) profile on top of v3's safety layer and the original v2 code.
