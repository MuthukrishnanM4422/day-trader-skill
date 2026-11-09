"""Shared market profile: US (default) and IN (India, NSE cash equities).

One definition of the things every trader-* tool needs about a market:
session hours, the holiday calendar, the time-zone math, the currency and
tick size, the ticker rules, and the round-trip cost model. Python 3.8+,
standard library only. No network.

India notes (verify before relying on them):
  Session   09:15 to 15:30 IST, Monday to Friday. IST is a fixed UTC+05:30,
            no daylight saving, so a naive local time maps to UTC exactly.
  Trap      the Indian session runs 23:45 to 06:00 New York time. Any
            "today" computed in New York time is the wrong trading date for
            half the Indian day. Always take the date in MARKET time.
  Holidays  NSE_HOLIDAYS below is the 2026 list from nseindia.com circulars
            as reported at research time; CHECK it against the live NSE
            holiday page before each use. It covers 2026 only: a date past
            the list is treated as a normal trading day and the tools warn.
  Muhurat   Sunday 2026-11-08 special session: Sundays are closed anyway.
  Symbols   Yahoo wants .NS for NSE (RELIANCE.NS). Cash equity only:
            derivative formats (futures, options) are refused.
  Costs     Zerodha-style equity intraday charges. Round trip is roughly
            8 to 11 paise per Rs 100 traded, about three times the US model,
            so many setups that look fine in the US correctly show no edge.
"""

import datetime
import re

EPOCH = datetime.datetime(1970, 1, 1)
UTC = datetime.timezone.utc

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------- calendars

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
US_YEARS = (2026, 2027)

# NSE equity holidays 2026. Names are best-effort labels from the published
# list; CHECK nseindia.com for the live circular, and extend the dict for
# later years (a date outside the list is treated as a trading day).
NSE_HOLIDAYS = {
    "2026-01-15": "special trading holiday (NSE circular)",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Babasaheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day / May Day",
    "2026-05-28": "Bakri Id (Eid al-Adha)",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh festival (per NSE list)",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali - Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
NSE_EARLY_CLOSE = {}   # no half sessions in the published 2026 list
IN_YEARS = (2026,)


# ---------------------------------------------------------------- profiles

PROFILES = {
    "US": {
        "code": "US",
        "name": "US equities (NYSE and Nasdaq)",
        "exchange": "NYSE",
        "tz_label": "New York time",
        "tz_short": "ET",
        "open": datetime.time(9, 30),
        "close": datetime.time(16, 0),
        "early": datetime.time(13, 0),
        "holidays": NYSE_HOLIDAYS,
        "early_close": NYSE_EARLY_CLOSE,
        "calendar_years": US_YEARS,
        "currency": "USD",
        "money_sign": "$",
        "money_word": "dollars",
        "price_dp": 2,
        "tick": 0.01,
        "yahoo_suffix": "",
        "yahoo_dot": "-",          # BRK.B trades on Yahoo as BRK-B
        "index_symbol": "^GSPC",
        "bars_5m": 78,
        "bars_1m": 390,
        "long_only": True,
        "cost_model": "us_default",
    },
    "IN": {
        "code": "IN",
        "name": "India NSE cash equities",
        "exchange": "NSE",
        "tz_label": "India time (IST)",
        "tz_short": "IST",
        "open": datetime.time(9, 15),
        "close": datetime.time(15, 30),
        "early": None,
        "holidays": NSE_HOLIDAYS,
        "early_close": NSE_EARLY_CLOSE,
        "calendar_years": IN_YEARS,
        "currency": "INR",
        "money_sign": "Rs ",
        "money_word": "rupees",
        "price_dp": 2,
        "tick": 0.05,
        "yahoo_suffix": ".NS",
        "yahoo_dot": ".",          # keep the dot: RELIANCE.NS stays RELIANCE.NS
        "index_symbol": "^NSEI",
        "bars_5m": 75,
        "bars_1m": 375,
        "long_only": True,
        "cost_model": "india_intraday",
    },
}

# names a user or config may write for the India profile
_ALIASES = {"IN": "IN", "INDIA": "IN", "NSE": "IN", "US": "US", "USA": "US",
            "NYSE": "US", "NASDAQ": "US", "": "US", None: "US"}


def code(market):
    """Normalize a market name to a profile code (default US)."""
    key = None if market is None else str(market).strip().upper()
    if key not in _ALIASES:
        raise ValueError("unknown market %r (want US or IN)" % market)
    return _ALIASES[key]


def profile(market):
    return PROFILES[code(market)]


# ---------------------------------------------------------------- time zone

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


def offset_hours(market, utc):
    """Hours from UTC to market wall-clock time at instant `utc` (naive UTC)."""
    if code(market) == "IN":
        return 5.5
    return eastern_offset(utc)


def to_market(utc, market):
    """naive UTC -> naive market-local wall clock."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=offset_hours(market, utc))


def market_to_utc(local, market):
    """naive market-local wall clock -> naive UTC."""
    local = local.replace(tzinfo=None)
    if code(market) == "IN":
        return local - IST_OFFSET
    # New York has DST: try both candidate offsets, keep the one that is
    # self-consistent (same approach the per-skill copies use).
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def utc_now():
    return datetime.datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def market_now(market, now_utc=None):
    return to_market(now_utc or utc_now(), market)


def today(market, now_utc=None):
    """The trading date in MARKET time. For India this is the IST date, which
    differs from the New York date for half of every Indian session."""
    return market_now(market, now_utc).date()


def tz_label(market):
    return profile(market)["tz_label"]


def tz_short(market):
    return profile(market)["tz_short"]


# ---------------------------------------------------------------- calendar

def holiday_name(market, d):
    return profile(market)["holidays"].get(d.isoformat())


def is_trading_day(market, d):
    """(is_open, reason). Weekends and the market's holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = holiday_name(market, d)
    if name:
        return False, "%s holiday: %s" % (profile(market)["exchange"], name)
    return True, ""


def session_times(market, d):
    """(open_time, close_time) in market-local time, or None when closed."""
    ok, _ = is_trading_day(market, d)
    if not ok:
        return None
    p = profile(market)
    early = p["early_close"].get(d.isoformat())
    return p["open"], (p["early"] if early and p["early"] else p["close"])


def early_close_reason(market, d):
    return profile(market)["early_close"].get(d.isoformat())


def next_session(market, d):
    """(date, open_time) of the next trading day after d, or None."""
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = session_times(market, nd)
        if s:
            return nd, s[0]
        nd += datetime.timedelta(days=1)
    return None


def calendar_known(market, d):
    return d.year in profile(market)["calendar_years"]


def market_status(market, now_utc=None):
    """One word about the market right now plus a detail string.
    -> (status, detail): open | pre-open | closed | holiday | weekend"""
    local = market_now(market, now_utc)
    d = local.date()
    ok, why = is_trading_day(market, d)
    if not ok:
        kind = "holiday" if "holiday" in why else "weekend"
        return kind, why
    o, c = session_times(market, d)
    t = local.time()
    if t < o:
        return "pre-open", "opens %02d:%02d %s" % (o.hour, o.minute, tz_short(market))
    if t >= c:
        return "closed", "closed at %02d:%02d %s" % (c.hour, c.minute, tz_short(market))
    return "open", "open %02d:%02d to %02d:%02d %s" % (o.hour, o.minute, c.hour, c.minute,
                                                     tz_short(market))


def bars_per_session(market, interval):
    p = profile(market)
    return p["bars_5m"] if interval in ("5m", 300) else p["bars_1m"]


# ---------------------------------------------------------------- symbols

_IN_BASE = re.compile(r"^[A-Z][A-Z0-9&\-]{0,19}$")
_IN_FULL = re.compile(r"^[A-Z][A-Z0-9&\-]{0,19}\.NS$")
_IN_INDEX = re.compile(r"^\^NSE[A-Z]*$")            # ^NSEI, ^NSEBANK: data only
_FUT = re.compile(r"(?:-FUT|_FUT|FUT$|_F\d+$|\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}FUT$)")
_OPT = re.compile(r"\d(?:CE|PE)$|\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d*(?:CE|PE)$")


def is_index(sym):
    return bool(_IN_INDEX.match(str(sym).upper()))


def is_derivative(sym):
    """True when the string looks like a futures or options symbol, which the
    India profile refuses (cash equity only)."""
    s = str(sym or "").upper().strip()
    s = s[:-3] if s.endswith(".NS") or s.endswith(".BO") else s
    if is_index(s):
        return False
    return bool(_FUT.search(s) or _OPT.search(s))


def valid_symbol(market, sym):
    """(ok, reason). The market's ticker rules."""
    s = str(sym or "").strip()
    if code(market) == "IN":
        if is_index(s):
            return True, ""
        if not s:
            return False, "empty symbol"
        if s.endswith(".BO"):
            return False, "this pack trades NSE only: use the .NS symbol, not .BO (BSE)"
        if is_derivative(s):
            return False, ("futures and options are out of scope: this pack trades NSE cash "
                           "equity only, long side only")
        bare = s[:-3] if s.endswith(".NS") else s
        if not _IN_BASE.match(bare):
            return False, "not an NSE cash symbol like RELIANCE or RELIANCE.NS"
        return True, ""
    return bool(re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", s.upper())), "not a US ticker" \
        if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", s.upper()) else ""


def yahoo_symbol(market, sym):
    """The ticker to send Yahoo. US: dots become dashes (BRK.B -> BRK-B).
    IN: bare names get the .NS suffix, existing .NS is kept (RELIANCE.NS)."""
    s = str(sym).upper().strip()
    if code(market) == "IN":
        if s.startswith("^"):
            return s
        if s.endswith(".NS"):
            return s
        if s.endswith(".BO"):      # let the validation message explain
            return s
        return s.replace(".", "") + ".NS"
    return s.replace(".", "-")


def bare_symbol(sym):
    """RELIANCE.NS -> RELIANCE; leaves US tickers alone."""
    s = str(sym).upper().strip()
    return s[:-3] if s.endswith(".NS") else s


# ---------------------------------------------------------------- money

def _indian_group(intpart):
    """1234567 -> 12,34,567 (crore/lakh grouping)."""
    if len(intpart) <= 3:
        return intpart
    head, tail = intpart[:-3], intpart[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def fmt_money(market, x, dp=2):
    """US: $1234.56   IN: Rs 1,234.56 (lakh/crore grouping)."""
    if x is None:
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    neg = v < 0
    text = ("%.*f" % (dp, abs(v)))
    if code(market) == "IN":
        intpart, _, frac = text.partition(".")
        text = _indian_group(intpart) + ("." + frac if frac else "")
    return ("-" if neg else "") + profile(market)["money_sign"] + text


def fmt_price(market, x):
    return fmt_money(market, x, profile(market)["price_dp"])


def money_word(market):
    return profile(market)["money_word"]


def tick(market):
    return profile(market)["tick"]


def tick_round(x, market):
    """Nearest tick (Rs 0.05 on the NSE, $0.01 in the US)."""
    t = tick(market)
    return round(round(float(x) / t) * t, 10)


def tick_floor(x, market):
    """Down to the tick, for stops."""
    import math
    t = tick(market)
    return round(math.floor(float(x) / t + 1e-9) * t, 10)


def tick_ceil(x, market):
    import math
    t = tick(market)
    return round(math.ceil(float(x) / t - 1e-9) * t, 10)


# ---------------------------------------------------------------- costs
# Zerodha-style equity intraday charges (research time, CHECK the live page):
#   brokerage    min(0.03% of turnover, Rs 20) per executed order
#   STT          0.025% of turnover, sell side only
#   exchange txn 0.00307% of turnover (NSE), both sides
#   SEBI charge  Rs 10 per crore of turnover, both sides
#   GST          18% on brokerage + exchange txn + SEBI
#   stamp duty   0.003% of turnover, buy side only

IN_BROKERAGE_PCT = 0.0003
IN_BROKERAGE_CAP = 20.0
IN_STT_SELL_PCT = 0.00025
IN_TXN_PCT = 0.0000307
IN_SEBI_PER_CR = 10.0 / 1e7
IN_GST_PCT = 0.18
IN_STAMP_BUY_PCT = 0.00003


def india_intraday_costs(buy_value, sell_value):
    """Round-trip rupee cost for an intraday equity trade.
    -> {"total": r, "brokerage": .., "stt": .., "txn": .., "sebi": ..,
        "gst": .., "stamp": ..}"""
    bv, sv = float(buy_value), float(sell_value)
    br = min(IN_BROKERAGE_PCT * bv, IN_BROKERAGE_CAP) + min(IN_BROKERAGE_PCT * sv, IN_BROKERAGE_CAP)
    stt = IN_STT_SELL_PCT * sv
    txn = IN_TXN_PCT * (bv + sv)
    sebi = IN_SEBI_PER_CR * (bv + sv)
    stamp = IN_STAMP_BUY_PCT * bv
    gst = IN_GST_PCT * (br + txn + sebi)
    total = br + stt + txn + sebi + gst + stamp
    return {"total": total, "brokerage": br, "stt": stt, "txn": txn, "sebi": sebi,
            "gst": gst, "stamp": stamp}


def cost_model_name(market, cfg=None):
    name = (cfg or {}).get("cost_model")
    if name:
        return str(name)
    return profile(market)["cost_model"]


def round_trip_cost(market, qty, entry, exit_, cfg=None):
    """Total estimated round-trip cost in account currency for qty shares
    bought at entry and sold at exit_. US: per-share fee plus slippage on both
    sides (the model the rest of the pack already uses). IN: the
    india_intraday model above; slippage from cfg["slippage_bps"] is added on
    top because no model can see your fill."""
    cfg = cfg or {}
    qty, entry, exit_ = int(qty), float(entry), float(exit_)
    if qty <= 0 or entry <= 0 or exit_ <= 0:
        return 0.0
    if cost_model_name(market, cfg) == "india_intraday":
        slip = float(cfg.get("slippage_bps") or 0) / 1e4 * qty * (entry + exit_)
        return india_intraday_costs(qty * entry, qty * exit_)["total"] + slip
    fee = float(cfg.get("fee_per_share_usd") or 0)
    slip = float(cfg.get("slippage_bps") or 0) / 1e4 * qty * (entry + exit_)
    return 2 * fee * qty + slip


# ---------------------------------------------------------------- config

def apply_inr_aliases(doc, market):
    """Under market IN a limit written as *_inr (say max_loss_per_trade_inr)
    is copied onto the matching *_usd key, so every limit in the tools is in
    account currency and rupees work without renaming a hundred call sites.
    Returns the (mutated) doc."""
    if code(market) != "IN" or not isinstance(doc, dict):
        return doc
    for k, v in list(doc.items()):
        if isinstance(k, str) and k.endswith("_inr"):
            doc[k[:-4] + "_usd"] = v
    return doc


# ---------------------------------------------------------------- fingerprint

def plan_fingerprint(d):
    """One stable id for a candidate trade, shared by the exit plan, the
    bull/bear files, the judge and the risk gate: sha1 of
    symbol|side|date|entry|stop|target with prices at 2 decimals."""
    import hashlib
    def px(v):
        try:
            return "%.2f" % float(v)
        except (TypeError, ValueError):
            return str(v)
    parts = [str(d.get("symbol") or "").upper(), str(d.get("side") or "buy").lower(),
             str(d.get("date") or ""), px(d.get("entry")), px(d.get("stop")), px(d.get("target"))]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- misc

def session_minutes(market):
    o, c = profile(market)["open"], profile(market)["close"]
    return (c.hour * 60 + c.minute) - (o.hour * 60 + o.minute)
