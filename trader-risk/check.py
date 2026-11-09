#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 10:15"        # pin the clock (New York time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order (Alpaca's rule),
                a limit order without a limit price, unknown type; a bracket
                that is not whole shares, not day / gtc, or misses a leg
  asset         options (OCC symbols) unless "allow_options": true; crypto
                and anything that is not a stock or ETF
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_usd, over max_order_usd
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) over max_loss_per_trade_usd. The entry is
                the limit price for a limit order, else the HIGHER of the plan's
                entry and a fresh price (under 15 minutes old), so a stale plan
                cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_usd (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (New York
                time), or the market is closed (weekend, NYSE holiday, outside
                9:30 to the close; on a 1:00 p.m. early close the window ends
                3 hours earlier too)
  pdt           only with "pdt_guard": true: 3 day trades already in the last
                5 business days (a 4th would trip the old pattern-day-trader rule)

The v3 safety gates (entries only, an exit is never blocked by them):

  data          "data_guard": true (default). trader-guard must have checked THIS
                symbol a few minutes ago (trader/state/data_guard.json, newer than
                data_guard_max_age_min) and found the data OK: fresh bars, sane
                prices, and a second source within data_guard_tolerance_pct
  edge          "require_edge": true (default). trader-timemachine must have run for
                THIS symbol today (trader/timemachine.json) and its result AFTER
                costs (fee_per_share_usd, slippage_bps) must show an edge: average R
                after costs of at least min_net_expectancy_r, in the replays and in
                the history, including the last third of the history on its own
  weekly loss   realized loss this week plus today's open risk plus this order's max
                loss over max_weekly_loss_usd; realized loss today over the daily limit
  streak        every one of the last max_consecutive_loss_days trading days had
                trades and lost money: no entries today (a one day cool-off)
  drawdown      realized equity down max_drawdown_pct of the budget from its peak
                (counted from "drawdown_since", a date you set to restart)
  live lock     "paper": false is refused until the paper log shows live_min_paper_days
                trading days and live_min_paper_trades closed trades, a profit after
                costs and a profit factor of at least live_min_profit_factor
The realized profit and loss comes from the fills in trader/state/broker.json
(oldest shares sold first), after the same costs. Fills the log does not mark as
paper never count toward the live lock.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 20, "max_position_pct": 25, "max_order_usd": 1250,
    "min_order_usd": 5, "max_loss_per_trade_usd": 25, "max_daily_loss_usd": 50, "max_trades_per_day": 2,
    "max_orders_per_day": 6, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "10:00", "no_entries_after": "15:00", "flat_by": "15:50",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": True,
    # v3 safety gates: ON unless you switch them off
    "max_weekly_loss_usd": 100, "max_consecutive_loss_days": 2, "max_drawdown_pct": 5,
    "drawdown_since": None, "fee_per_share_usd": 0.005, "slippage_bps": 3,
    "data_guard": True, "data_guard_max_age_min": 3, "require_edge": True, "min_net_expectancy_r": 0.05,
    "live_min_paper_days": 30, "live_min_paper_trades": 50, "live_min_profit_factor": 1.3,
}
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)

# India (NSE cash equity, rupees, IST): values that differ from the US
# defaults above, applied only while the market is IN.
DEFAULTS_IN = {"no_entries_before": "09:45", "no_entries_after": "14:30",
               "flat_by": "15:10", "pdt_guard": False}
# require_judge: None means auto - ON under market IN (the judge guards that
# pipeline), OFF under market US unless you set it true yourself.
DEFAULTS["require_judge"] = None
DEFAULTS["judge_max_age_min"] = 60


class RiskError(Exception):
    pass


# ----------------------------------------------------------- market profile
# One shared market definition lives in lib/market_profile.py: "US" (the
# default, exactly the behaviour below) or "IN" (India, NSE cash equities,
# rupees, IST). Without the lib the tool still runs as the US version.
_MPL = None
_MPL_TRIED = False
_MARKET = "US"


def _mpl():
    global _MPL, _MPL_TRIED
    if not _MPL_TRIED:
        _MPL_TRIED = True
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        for base in (here, os.path.join(here, "lib"), os.path.join(here, "..", "lib"),
                     os.path.join(here, "..", "..", "lib")):
            p = os.path.normpath(os.path.join(base, "market_profile.py"))
            if os.path.isfile(p):
                spec = importlib.util.spec_from_file_location("market_profile", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _MPL = mod
                break
    return _MPL


def set_market(code):
    """Pick the market: 'US' (default) or 'IN'/'NSE'/'India'."""
    global _MARKET
    if str(code or "").strip().upper() in ("IN", "INDIA", "NSE"):
        if _mpl() is None:
            raise RiskError('market "IN" needs lib/market_profile.py: keep the lib/ '
                            "folder next to the trader-* folders")
        _MARKET = "IN"
    else:
        _MARKET = "US"


def market():
    return _MARKET


def to_market(utc):
    """naive UTC -> market wall clock (New York or IST)."""
    return _mpl().to_market(utc, _MARKET) if _MARKET != "US" else to_eastern(utc)


def market_to_utc(local):
    return _mpl().market_to_utc(local, _MARKET) if _MARKET != "US" else eastern_to_utc(local)


def market_today():
    return to_market(parse_now(None)).date()


def tz_name():
    return _mpl().tz_label(_MARKET) if _mpl() else "New York time"


def tzshort():
    return _mpl().tz_short(_MARKET) if _mpl() else "ET"


def clock_word():
    """The short zone word printed in messages: 'New York' or 'IST'."""
    return "IST" if _MARKET == "IN" else "New York"


def apply_market(a, doc):
    """Flag > env > config decides the market, once, before any time math."""
    set_market(getattr(a, "market", None) or os.environ.get("TRADER_MARKET")
               or (doc or {}).get("market"))


def _defaults():
    d = dict(DEFAULTS)
    if _MARKET != "US":
        d.update(DEFAULTS_IN)
    return d


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is market time (New
    York, or IST under market IN)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    for suffix in (" ET", " IST"):
        if t.upper().endswith(suffix):
            t = t[:-len(suffix)].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % tz_name())
    if dt.tzinfo is None:
        return market_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the market's holiday list."""
    if _MARKET != "US":
        return _mpl().is_trading_day(_MARKET, d)
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be a %s like \"09:45\", got %r" % (key, tz_name(), value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _shift(t, hours):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(hours=hours)
    return dt.time()


def _session(d):
    """(open, close) in market time for d, or None when closed."""
    if _MARKET != "US":
        return _mpl().session_times(_MARKET, d)
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (market time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    years = _mpl().profile(_MARKET)["calendar_years"] if _mpl() else CALENDAR_YEARS
    site = "nseindia.com" if _MARKET == "IN" else "nyse.com"
    if d.year not in years:
        warns.append("the holiday list covers %s only: check %s for %s"
                     % (" and ".join(map(str, years)), site, d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    tz = clock_word()
    if not ok:
        return ["window: the market is closed today (%s, %s %s)" % (why, stamp, tz)], warns
    if _MARKET != "US":
        open_t, close = _session(d)
        early_name = None
    else:
        open_t = OPEN
        close = EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE
        early_name = NYSE_EARLY_CLOSE.get(d.isoformat())
    if t < open_t or now_et.time() >= close:
        return ["window: the market is closed (%s %s; the session is %s to %s)"
                % (stamp, tz, open_t.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if early_name and after is not None:
        after = _shift(after, 3)
        warns.append("early close today (%s): no new entries after %s"
                     % (early_name, after.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s %s (it is %s)" % (before.strftime("%H:%M"), tz,
                                                                    t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s %s (it is %s)" % (after.strftime("%H:%M"), tz,
                                                                   t.strftime("%H:%M"))], warns
    return [], warns


def business_days(today, n=5):
    """The last n NYSE trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(40):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    if _MARKET != "US" and _mpl():
        return _mpl().fmt_money(_MARKET, x)
    return "$" + "{:,.2f}".format(float(x)).replace(".00", "")


def _cent(x):
    """A price to the cent, half up (366.475 -> 366.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def day_trades(state, today):
    """Day trades in the last 5 business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for rec in (state or {}).get("orders", []):
        if rec.get("status") not in ("placed", "filled"):
            continue
        d = rec_day(rec)
        if d not in days:
            continue
        key = (d, str(rec.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(rec.get("side", "")).lower())
        if d == today and rec.get("status") == "placed" and rec_is_entry(rec):
            sides[key].add("pending")
    hits = sorted("%s %s" % k for k, s in sides.items()
                  if ("buy" in s and "sell" in s) or (k[0] == today and "pending" in s))
    return len(hits), hits


# ---------------------------------------------------------------- realized profit and loss

def _qty(rec):
    try:
        return float(rec.get("qty"))
    except (TypeError, ValueError):
        return None


def closed_trades(state, c):
    """Closed long trades from the filled records in the log, oldest shares sold first.

    Each is {"day", "symbol", "pnl", "paper"}: pnl after fees and slippage on both
    sides (fee_per_share_usd, slippage_bps); paper is True only when the buy and the
    sell were both logged as paper fills. Sells of shares the log never saw bought
    are ignored (short selling is off, and a fill never recorded has no cost to match)."""
    fee = _f(c.get("fee_per_share_usd")) or 0.0
    slip = (_f(c.get("slippage_bps")) or 0.0) / 10000.0
    fills = [r for r in (state or {}).get("orders", []) if r.get("status") == "filled"]
    fills.sort(key=lambda r: (rec_day(r), str(r.get("at", ""))))
    lots, out = {}, []
    for r in fills:
        sym = str(r.get("symbol", "")).upper()
        q, px = _qty(r), _f(r.get("price"))
        if not sym or not q or q <= 0 or not px:
            continue
        side = str(r.get("side", "")).lower()
        if side == "buy":
            lots.setdefault(sym, []).append([q, px, r.get("paper") is True])
        elif side == "sell":
            left = q
            while left > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(left, lot[0])
                gross = (px - lot[1]) * take
                if _mpl() is not None:
                    # market-aware cost model: identical to the US formula
                    # below; under market IN it is the india_intraday model
                    # (brokerage, STT, exchange charge, SEBI, GST, stamp duty)
                    costs = _mpl().round_trip_cost(_MARKET, take, lot[1], px, c)
                else:
                    costs = fee * take * 2 + slip * (px + lot[1]) * take
                out.append({"day": rec_day(r), "symbol": sym, "pnl": round(gross - costs, 4),
                            "paper": bool(lot[2] and r.get("paper") is True)})
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    lots[sym].pop(0)
    return out


def week_start(today):
    d = datetime.date.fromisoformat(today)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def safety_gates(trades, c, today, loss_today):
    """(reasons that block every entry now, facts for the report). Pure."""
    reasons, facts = [], {}
    by_day = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]
    real_today = by_day.get(today, 0.0)
    wk0 = week_start(today)
    week = sum(v for d, v in by_day.items() if wk0 <= d <= today)
    facts["realized_today"] = round(real_today, 2)
    facts["realized_week"] = round(week, 2)

    daily = _f(c.get("max_daily_loss_usd"))
    if daily and -real_today > daily + CENT:
        reasons.append("daily loss: realized %s today after costs, over your %s daily loss limit; "
                       "no more entries today" % (money(real_today), money(daily)))
    weekly = _f(c.get("max_weekly_loss_usd"))
    if weekly and -week + loss_today > weekly - CENT:
        reasons.append("weekly loss: %s realized this week (from %s) plus %s at risk today reaches your %s "
                       "weekly loss limit; no more entries this week"
                       % (money(week), wk0, money(loss_today), money(weekly)))

    n = int(c.get("max_consecutive_loss_days") or 0)
    if n:
        prev = [d for d in business_days(datetime.date.fromisoformat(today), n + 1) if d != today][:n]
        if len(prev) == n and all(d in by_day and by_day[d] < 0 for d in prev):
            reasons.append("streak: the last %d trading days (%s) all lost money; no entries today, a "
                           "one day cool-off (\"max_consecutive_loss_days\")" % (n, ", ".join(sorted(prev))))

    pct, budget = _f(c.get("max_drawdown_pct")), _f(c.get("budget"))
    since = str(c.get("drawdown_since") or "")
    if pct and budget:
        peak = cum = 0.0
        for d in sorted(by_day):
            if since and d < since:
                continue
            cum += by_day[d]
            peak = max(peak, cum)
        dd = peak - cum
        facts["drawdown_usd"] = round(dd, 2)
        if dd >= pct / 100.0 * budget - CENT:
            reasons.append("drawdown: realized equity is %s below its peak, %s%% of your %s budget (limit "
                           "%s%%). Stop and review. To restart, set \"drawdown_since\" to a new date"
                           % (money(dd), _pct(100.0 * dd / budget), money(budget), _pct(pct)))
    return reasons, facts


def live_lock(trades, c):
    """Reasons "paper": false is refused. Empty when the paper record earns it."""
    if c.get("paper", True):
        return []
    paper = [t for t in trades if t["paper"]]
    days = len({t["day"] for t in paper})
    need_days, need_n = int(c.get("live_min_paper_days") or 0), int(c.get("live_min_paper_trades") or 0)
    net = sum(t["pnl"] for t in paper)
    wins = sum(t["pnl"] for t in paper if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in paper if t["pnl"] < 0)
    pf = (wins / loss) if loss > 0 else (float("inf") if wins > 0 else 0.0)
    need_pf = _f(c.get("live_min_profit_factor")) or 0.0
    short = []
    if days < need_days:
        short.append("%d paper trading days (need %d)" % (days, need_days))
    if len(paper) < need_n:
        short.append("%d closed paper trades (need %d)" % (len(paper), need_n))
    if net <= 0:
        short.append("paper profit after costs is %s (need above $0)" % money(net))
    if pf < need_pf:
        short.append("profit factor %s (need %s)" % ("%.2f" % pf if pf != float("inf") else "n/a", _num(need_pf)))
    if not short:
        return []
    return ["live lock: \"paper\": false is refused until the paper record earns it: " + "; ".join(short)
            + ". Switch back to \"paper\": true, or keep paper trading"]


def data_reasons(guard, sym, c, now_utc):
    """Reasons trader-guard's check of this symbol does not clear the order."""
    if not c.get("data_guard", True):
        return []
    rec = ((guard or {}).get("symbols") or {}).get(sym)
    if rec is None and _MARKET != "US" and _mpl() is not None:
        rec = ((guard or {}).get("symbols") or {}).get(_mpl().yahoo_symbol(_MARKET, sym))
        if rec is None:
            rec = ((guard or {}).get("symbols") or {}).get(_mpl().bare_symbol(sym))
    if not rec:
        return ["data: trader-guard has not checked %s. Run guard.py data %s first" % (sym, sym)]
    when = parse_utc(rec.get("checked_at"))
    max_age = float(c.get("data_guard_max_age_min") or 3)
    if when is None or not -60 <= (now_utc - when).total_seconds() <= max_age * 60:
        return ["data: trader-guard's check of %s is older than %s minutes (or has no time). Run guard.py "
                "data %s again" % (sym, _num(max_age), sym)]
    if rec.get("verdict") != "OK":
        why = "; ".join(rec.get("reasons") or ["no reason given"])
        return ["data: trader-guard says the data for %s is NOT OK: %s" % (sym, why)]
    return []


def edge_reasons(tm, sym, today, c):
    """Reasons the Time Machine's result does not clear the order."""
    if not c.get("require_edge", True):
        return []
    plan = (tm or {}).get("plan") or {}
    if not tm or str(plan.get("symbol", "")).upper() != sym or str(plan.get("date", "")) != today:
        return ["edge: no Time Machine run for %s on %s. Run trader-timemachine --write first; with "
                "\"require_edge\": true an unmeasured trade is not placed" % (sym, today)]
    costs = tm.get("costs") or {}
    if "edge_ok" not in costs:
        return ["edge: this Time Machine result has no after-cost numbers (an older version). Run it again"]
    if costs.get("edge_ok") is not True:
        return ["edge: no edge after costs for %s: %s" % (sym, "; ".join(costs.get("edge_reasons") or ["not ok"]))]
    return []


def _require_judge(c):
    """require_judge: explicit true/false wins; the default (None) is auto -
    on under market IN where the judge guards the pipeline, off under US."""
    v = c.get("require_judge")
    if v is None:
        return _MARKET == "IN"
    return bool(v)


def judge_reasons(judge, sym, o, today, c, now_utc):
    """Reasons trader-judge's verdict does not clear this entry. The judge can
    only ever ADD a block here: a BUY clears this one gate, it never relaxes
    or overrides any other check, and a user 'yes' never overrides it."""
    if not _require_judge(c):
        return []
    if not judge:
        return ["judge: no verdict on file. Run trader-judge (judge.py decide) on this plan "
                "first; with \"require_judge\" an unjudged entry is not placed"]
    plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
    off = []
    jsym = str(judge.get("symbol") or "").upper()
    same = jsym == sym or (_mpl() is not None and
                           _mpl().bare_symbol(jsym) == _mpl().bare_symbol(sym))
    if not same:
        off.append("symbol %s not %s" % (jsym or "?", sym))
    if str(judge.get("date") or "") != str(today):
        off.append("date %s not %s" % (judge.get("date") or "?", today))
    if _mpl() is not None:
        if plan.get("entry") is None or plan.get("stop") is None or plan.get("target") is None:
            off.append("the order has no entry/stop/target to match the judged plan")
        elif str(judge.get("plan_fp") or "") != _mpl().plan_fingerprint(
                {"symbol": sym, "side": o.get("side"), "date": today, "entry": plan.get("entry"),
                 "stop": plan.get("stop"), "target": plan.get("target")}):
            off.append("a different plan than the one on this order")
    when = parse_utc(judge.get("created_at"))
    max_age = float(c.get("judge_max_age_min") or 60)
    if when is None or not -60 <= (now_utc - when).total_seconds() <= max_age * 60:
        off.append("a verdict from %s (older than %s minutes)" % (judge.get("created_at") or "no time",
                                                                  _num(max_age)))
    if off:
        return ["judge: the verdict on file is for %s. Run judge.py decide on this plan"
                % ", ".join(off)]
    verdict = str(judge.get("verdict") or "").upper()
    if verdict != "BUY":
        why = "; ".join(judge.get("headline_reasons") or judge.get("reasons_short") or [])
        return ["judge: the Judge says %s for %s%s; only a BUY clears this gate"
                % (verdict or "NO VERDICT", sym, (": " + why) if why else "")]
    return []


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          guard=None, timemachine=None, judge=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its market date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    guard        trader/state/data_guard.json as a dict (trader-guard's verdicts)
    timemachine  trader/timemachine.json as a dict (needed while "require_edge" is on)
    judge        trader/state/judge.json as a dict (needed while "require_judge" is on)"""
    c = _defaults()
    c.update(cfg or {})
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_et = to_market(now_utc)
    today = today or now_et.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_et, c)
    pdt_n, pdt_list = day_trades(state, today) if (c.get("pdt_guard") and _MARKET == "US") else (0, [])
    trades = closed_trades(state, c)
    gate_reasons, gate_facts = safety_gates(trades, c, today, loss_today)
    gate_reasons += live_lock(trades, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (dollar) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if notional is not None:
                reasons.append("format: a bracket order needs a whole-share qty, not a notional amount")
            elif _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
                reasons.append("format: a bracket order takes whole shares only, got qty %r" % qty)
            if tif not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (oc or "advanced_instructions"))

        # asset
        if _MARKET != "US" and _mpl() is not None:
            if sym and _mpl().is_index(sym):
                reasons.append("asset: %s is an index, not a share you can buy" % sym)
            elif sym and _mpl().is_derivative(sym):
                reasons.append("asset: %s looks like a futures or options symbol; this pack "
                               "trades NSE cash equity only, long side only" % sym)
            elif sym:
                ok, why = _mpl().valid_symbol(_MARKET, sym)
                if not ok:
                    reasons.append("asset: %s" % (why or ("%s is not an NSE cash symbol" % sym)))
        elif sym and OCC.match(sym.replace(" ", "")):
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100%% of what you pay")
        elif sym and ("/" in sym or not TICKER.match(sym)):
            reasons.append("asset: %s does not look like a US stock or ETF ticker; "
                           "this pack trades stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_usd") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_usd"])))
            if c.get("max_order_usd") and value > float(c["max_order_usd"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_usd"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_usd = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                             % (money(value), money(cash)))
            elif after_cash < buffer_usd - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_usd)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    max_loss = round(shares * risk_ps, 2)
                    cap = _f(c.get("max_loss_per_trade_usd"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_usd"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            later.extend(gate_reasons)
            weekly = _f(c.get("max_weekly_loss_usd"))
            if weekly and max_loss is not None and not gate_reasons and \
                    -gate_facts["realized_week"] + loss_today + batch_loss + max_loss > weekly + CENT:
                later.append("weekly loss: %s realized this week plus %s at risk plus %s for this one is over "
                             "your %s weekly loss limit" % (money(gate_facts["realized_week"]),
                                                            money(loss_today + batch_loss), money(max_loss),
                                                            money(weekly)))
            later.extend(data_reasons(guard, sym, c, now_utc))
            later.extend(edge_reasons(timemachine, sym, today, c))
            later.extend(judge_reasons(judge, sym, o, today, c, now_utc))
            if c.get("pdt_guard") and _MARKET == "US" and pdt_n >= 3:
                later.append("pdt: %d day trades in the last 5 business days (%s); a 4th trips the "
                             "pattern-day-trader rule your broker applies (\"pdt_guard\": true)"
                             % (pdt_n, ", ".join(pdt_list)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_usd"]))
                               if _f(c.get("max_loss_per_trade_usd")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_usd": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_new_york": now_et.strftime("%Y-%m-%d %H:%M"),
        "now_market": now_et.strftime("%Y-%m-%d %H:%M"),
        "market": _MARKET,
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2), "day_trades_5d": pdt_n if c.get("pdt_guard") else None,
                  "realized_today": gate_facts["realized_today"], "realized_week": gate_facts["realized_week"]},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s %s. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_market"], clock_word(),
                    money(res["cash_after"]), "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def status(state, cfg, today, now_et):
    """Where the safety gates stand, in plain lines. Pure."""
    c = _defaults()
    c.update(cfg or {})
    trades = closed_trades(state, c)
    done = placed_today(state, today)
    loss_today = sum(_f(r.get("max_loss")) or 0.0 for r in done if rec_is_entry(r))
    reasons, facts = safety_gates(trades, c, today, loss_today)
    paper = [t for t in trades if t["paper"]]
    pw = sum(t["pnl"] for t in paper if t["pnl"] > 0)
    pl = -sum(t["pnl"] for t in paper if t["pnl"] < 0)
    lines = ["Safety status, %s %s (%s account)" % (now_et.strftime("%Y-%m-%d %H:%M"), tz_name(),
                                                    "paper" if c.get("paper", True) else "LIVE"),
             "  closed trades logged: %d (%d paper), net %s after costs"
             % (len(trades), len(paper), money(sum(t["pnl"] for t in trades))),
             "  realized today %s, this week %s; risk at the stops of today's entries %s"
             % (money(facts["realized_today"]), money(facts["realized_week"]), money(loss_today))]
    if "drawdown_usd" in facts:
        lines.append("  drawdown from the equity peak: %s" % money(facts["drawdown_usd"]))
    lines.append("  paper record for the live lock: %d days (need %s), %d closed trades (need %s), net %s, "
                 "profit factor %s (need %s)"
                 % (len({t["day"] for t in paper}), c.get("live_min_paper_days"), len(paper),
                    c.get("live_min_paper_trades"), money(sum(t["pnl"] for t in paper)),
                    ("%.2f" % (pw / pl)) if pl > 0 else ("n/a" if pw == 0 else "no losses yet"),
                    c.get("live_min_profit_factor")))
    gates = reasons + live_lock(trades, c)
    if gates:
        lines.append("ENTRIES ARE BLOCKED now:")
        lines.extend("  - " + r for r in gates)
    else:
        lines.append("No safety gate blocks entries right now (the per-order checks still apply).")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--guard", default="trader/state/data_guard.json", help="trader-guard's data verdicts")
    ap.add_argument("--timemachine", default="trader/timemachine.json", help="trader-timemachine's result")
    ap.add_argument("--judge", default="trader/state/judge.json", help="trader-judge's verdict")
    ap.add_argument("--market", choices=["US", "IN"], help="market profile (default US; IN = NSE)")
    ap.add_argument("--now", help="pretend it is this market time, e.g. \"2026-10-19 10:15\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in market time)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--status", action="store_true",
                    help="print where the safety gates stand (realized profit and loss, streak, drawdown, "
                         "live lock) and exit; no orders file needed")
    a = ap.parse_args(argv)
    try:
        cfg = read_json(a.config, "config", required=False) or {}
        apply_market(a, cfg)
        if _mpl() is not None:
            _mpl().apply_inr_aliases(cfg, _MARKET)
        now = parse_now(a.now)
        if a.status:
            state = read_json(a.state, "broker state", required=False)
            today = a.today or to_market(now).date().isoformat()
            print("\n".join(status(state, cfg, today, to_market(now))))
            return 0
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-19")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        guard = read_json(a.guard, "data guard", required=False)
        tm = read_json(a.timemachine, "time machine result", required=False)
        judge = read_json(a.judge, "judge verdict", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, guard, tm, judge)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
