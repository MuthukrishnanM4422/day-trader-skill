#!/usr/bin/env python3
"""Data guard for trader-guard. Standard library only.

Free price data is the weak spot of this pack: Yahoo's chart endpoint is
unofficial and can lag, freeze, skip bars or return a bad print. This tool
checks the data for ONE or more symbols right before an order, and writes a
verdict that trader-risk reads. No OK verdict, no entry.

    python3 guard.py data TSLA                      # check, print, write trader/state/data_guard.json
    python3 guard.py data TSLA AAPL --max-age-min 3 --tolerance-pct 1.0
    python3 guard.py data TSLA --json               # the verdict as JSON
    python3 guard.py data TSLA --no-write           # look, do not record
    python3 guard.py show                           # what was recorded, and how old it is
    python3 guard.py data TSLA --yahoo-file chart.json --stooq-file quote.csv --now "2026-10-19 10:15"

What it checks (each failure is a reason, any reason makes the verdict BAD):

  market       the regular session is open (Yahoo's own trading period for today)
  fresh        the last 1-minute bar started no more than max_age_min (+ 1 minute)
               ago; a frozen feed (the last 5 bars identical, volume 0) is BAD
  sane bars    no missing or non-positive prices, high >= low, open and close
               inside the bar, no jump of more than 20 % from one bar to the next
               (a bad print or an unadjusted split), a currency that is USD
  halt         the last 3 bars with zero volume: treated as a halt or a dead feed
  second       Yahoo's price against Stooq's last quote for the same ticker. They
  source       must agree within data_guard_tolerance_pct (default 1.0 %). With
               "require_second_source": true (default) a missing second source is
               BAD too: one free feed alone is not trusted

The price it records is the HIGHER of the two sources, so a lagging quote cannot
understate what a buy costs. Exit code 0 when every symbol is OK, 1 otherwise.

Both feeds are free and unofficial and can fail for a day. That is the point:
when the guard cannot confirm the data, the answer is BAD, not "probably fine".
Times are New York (ET) wall clock; --now pins the clock.
Education and tooling, not financial advice.
"""

import argparse
import csv
import datetime
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; trader-guard/1.0)"
PAUSE = 0.5
YAHOO_HOSTS = ("query2", "query1")
MAX_JUMP = 0.20          # one bar to the next
FROZEN_BARS = 5
HALT_BARS = 3
DEFAULTS = {"data_guard_max_age_min": 3, "data_guard_tolerance_pct": 1.0, "require_second_source": True}


class GuardError(Exception):
    pass


# ----------------------------------------------------------- market profile
# One shared market definition lives in lib/market_profile.py: "US" (the
# default, exactly the behaviour above) or "IN" (India, NSE cash equities,
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
    """Pick the market: 'US' (default) or 'IN'/'NSE'/'India'. Called by the
    command entry points once flags and config are known."""
    global _MARKET
    if str(code or "").strip().upper() in ("IN", "INDIA", "NSE"):
        if _mpl() is None:
            raise GuardError('market "IN" needs lib/market_profile.py: keep the lib/ '
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
    """The trading date in MARKET time. For India this is the IST date, never
    the New York date: the NSE session runs 23:45-06:00 New York time."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    return to_market(now).date()


def tz_name():
    return _mpl().tz_label(_MARKET) if _mpl() else "New York time"


def tz_word():
    """The word for market-local time in messages: New York (US) or IST."""
    return _mpl().tz_short(_MARKET) if _MARKET != "US" else "New York"


def mny(x):
    return _mpl().fmt_price(_MARKET, x) if _mpl() else "$%.2f" % x


# ---------------------------------------------------------------- time

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise GuardError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % tz_word())
    if dt.tzinfo is None:
        return market_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).replace(tzinfo=None)


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ny_text(utc):
    return to_market(utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- fetching

_urlopen = urllib.request.urlopen   # tests swap these
_sleep = time.sleep


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        raise GuardError("could not reach %s: %s" % (url.split("/")[2], getattr(e, "reason", e)))


def yahoo_symbol(sym):
    if _MARKET != "US":
        return _mpl().yahoo_symbol(_MARKET, sym)
    return sym.upper().replace(".", "-")


def fetch_yahoo(sym):
    last = "no answer"
    for host in YAHOO_HOSTS:
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m" % (
            host, urllib.parse.quote(yahoo_symbol(sym)))
        try:
            code, text = _get(url)
        except GuardError as e:
            last = str(e)
            continue
        if code == 200:
            return text
        last = "%s answered HTTP %d" % (url.split("?")[0], code)
    raise GuardError(last)


def stooq_symbol(sym):
    return sym.lower().replace(".", "-") + ".us"


def fetch_stooq(sym):
    url = "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % urllib.parse.quote(stooq_symbol(sym))
    code, text = _get(url)
    if code != 200:
        raise GuardError("stooq answered HTTP %d" % code)
    return text


# ---------------------------------------------------------------- parsing

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_yahoo(text, sym):
    """Yahoo chart JSON -> {"meta", "bars"} (bars oldest first, each {"t", "o", "h", "l", "c", "v"}).
    A bar with any missing field is kept with None so the sanity check can flag it."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise GuardError("Yahoo's answer for %s is not JSON" % sym)
    chart = doc.get("chart") or {}
    if chart.get("error"):
        raise GuardError("Yahoo: %s" % (chart["error"].get("description") or chart["error"].get("code")))
    res = (chart.get("result") or [None])[0]
    if not res:
        raise GuardError("Yahoo has no chart for %s" % sym)
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        bar = {"t": int(t)}
        for k, key in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            arr = q.get(key) or []
            bar[k] = _num(arr[i]) if i < len(arr) else None
        bars.append(bar)
    return {"meta": meta, "bars": bars}


def parse_stooq(text):
    """Stooq's one-line CSV -> {"price", "date"} or None when it has no data (N/D)."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        return None
    r = rows[0]
    price = _num(r.get("Close"))
    if price is None or price <= 0:
        return None
    return {"price": price, "date": r.get("Date"), "time": r.get("Time")}


# ---------------------------------------------------------------- the checks (pure)

def check_market(meta, now_utc):
    reg = ((meta.get("currentTradingPeriod") or {}).get("regular")) or {}
    start, end = _num(reg.get("start")), _num(reg.get("end"))
    if start is None or end is None:
        return ["market: Yahoo gave no trading period, so open or closed cannot be confirmed"]
    s, e = utc_of(start), utc_of(end)
    if not s <= now_utc < e:
        return ["market: the regular session is not open now (%s to %s %s, it is %s)"
                % (ny_text(s)[11:], ny_text(e)[11:], tz_word(), ny_text(now_utc))]
    return []


def check_bars(bars, meta, now_utc, max_age_min):
    """Reasons the 1-minute bars cannot be trusted. Empty when they look right."""
    reasons = []
    cur = meta.get("currency")
    want = _mpl().profile(_MARKET)["currency"] if _mpl() else "USD"
    if cur and str(cur).upper() != want:
        reasons.append("currency: the data is in %s, not %s" % (cur, want))
    if not bars:
        return reasons + ["fresh: no bars at all"]
    bad = [b for b in bars if None in (b["o"], b["h"], b["l"], b["c"])]
    last = None
    for b in reversed(bars):
        if None not in (b["o"], b["h"], b["l"], b["c"]):
            last = b
            break
    if last is None:
        return reasons + ["sane bars: every bar has a missing price"]
    age = (now_utc - utc_of(last["t"])).total_seconds()
    limit = max_age_min * 60 + 60
    if age > limit:
        reasons.append("fresh: the last bar started %d min ago (limit %s min), at %s %s"
                       % (age // 60, ("%g" % max_age_min), ny_text(utc_of(last["t"]))[11:], tz_word()))
    if age < -60:
        reasons.append("fresh: the last bar is %d s in the future: the clock or the data is wrong" % -age)
    good = [b for b in bars if b not in bad]
    for b in good:
        when = ny_text(utc_of(b["t"]))[11:]
        if min(b["o"], b["h"], b["l"], b["c"]) <= 0:
            reasons.append("sane bars: a zero or negative price in the %s bar" % when)
            break
        if b["h"] < b["l"] - 1e-9 or not (b["l"] - 1e-9 <= b["o"] <= b["h"] + 1e-9) \
                or not (b["l"] - 1e-9 <= b["c"] <= b["h"] + 1e-9):
            reasons.append("sane bars: the %s bar's open or close is outside its high and low" % when)
            break
    recent_bad = [b for b in bad if b["t"] >= good[-5]["t"]] if len(good) >= 5 else bad
    if recent_bad:
        reasons.append("sane bars: %d of the recent bars have a missing price" % len(recent_bad))
    for prev, b in zip(good, good[1:]):
        if prev["c"] > 0 and abs(b["o"] - prev["c"]) / prev["c"] > MAX_JUMP or \
                prev["c"] > 0 and abs(b["c"] - prev["c"]) / prev["c"] > MAX_JUMP:
            reasons.append("sane bars: a jump of more than %d %% between two bars at %s %s (bad print or "
                           "split)" % (MAX_JUMP * 100, ny_text(utc_of(b["t"]))[11:], tz_word()))
            break
    tail = good[-FROZEN_BARS:]
    if len(tail) == FROZEN_BARS and len({(b["o"], b["h"], b["l"], b["c"]) for b in tail}) == 1 \
            and all((b["v"] or 0) == 0 for b in tail):
        reasons.append("fresh: the last %d bars are identical with no volume: a frozen feed" % FROZEN_BARS)
    elif len(good) >= HALT_BARS and all((b["v"] or 0) == 0 for b in good[-HALT_BARS:]):
        reasons.append("halt: no volume in the last %d bars: a trading halt or a dead feed" % HALT_BARS)
    return reasons


def cross_check(yahoo_price, stooq, tolerance_pct, bar_day, name="Stooq"):
    """(reasons, diff_pct). The second source's last quote against Yahoo's; same-day quote required."""
    if stooq is None:
        return ["second source: %s has no quote for this ticker" % name], None
    if stooq.get("date") and bar_day and str(stooq["date"]) != bar_day:
        return ["second source: %s's quote is from %s, not %s" % (name, stooq["date"], bar_day)], None
    diff = abs(yahoo_price - stooq["price"]) / stooq["price"] * 100.0
    if diff > tolerance_pct:
        return ["second source: Yahoo %.2f and %s %.2f differ by %.2f %% (limit %s %%)"
                % (yahoo_price, name, stooq["price"], diff, ("%g" % tolerance_pct))], round(diff, 3)
    return [], round(diff, 3)


def judge(sym, yahoo_text, stooq_text, now_utc, cfg, stooq_error=None, manual=None):
    """One symbol's verdict from the raw feed texts. Pure (no network).
    `manual` ({price, source} from --second-price) replaces the Stooq cross-check."""
    c = dict(DEFAULTS_IN if _MARKET != "US" else DEFAULTS)
    c.update(cfg or {})
    reasons, warns = [], []
    rec = {"checked_at": iso_z(now_utc), "verdict": "BAD", "reasons": reasons, "warnings": warns}
    try:
        y = parse_yahoo(yahoo_text, sym)
    except GuardError as e:
        reasons.append("fresh: %s" % e)
        return rec
    meta, bars = y["meta"], y["bars"]
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != yahoo_symbol(sym):
        reasons.append("sane bars: the data is for %s, not %s" % (msym, sym))
    reasons.extend(check_market(meta, now_utc))
    reasons.extend(check_bars(bars, meta, now_utc, float(c["data_guard_max_age_min"])))
    closes = [b for b in bars if b["c"] is not None]
    if not closes:
        return rec
    last = closes[-1]
    price_y = last["c"]
    rec.update({"yahoo_price": round(price_y, 4), "last_bar": ny_text(utc_of(last["t"]))[11:],
                "age_sec": int((now_utc - utc_of(last["t"])).total_seconds())})
    price = price_y
    bar_day = ny_text(utc_of(last["t"]))[:10]
    if manual is not None:
        name = str(manual.get("source") or "manual")
        mp = _num(manual.get("price"))
        if mp is None or mp <= 0:
            msg = "second source: --second-price %r is not a price" % (manual.get("price"),)
            (reasons if c.get("require_second_source", True) else warns).append(msg)
        else:
            why, diff = cross_check(price_y, {"price": mp}, float(c["data_guard_tolerance_pct"]),
                                    bar_day, name)
            (reasons if (diff is not None or c.get("require_second_source", True)) else warns).extend(why)
            rec["second_price"] = round(mp, 4)
            rec["second_source"] = name
            price = max(price_y, mp)
            if diff is not None:
                rec["diff_pct"] = diff
    elif stooq_text is None:
        if _MARKET == "IN":
            msg = ("second source: Stooq does not cover NSE - pass --second-price from your broker "
                   "quote (e.g. Kite)")
        else:
            msg = "second source: %s" % (stooq_error or "not checked")
        (reasons if c.get("require_second_source", True) else warns).append(msg)
    else:
        try:
            st = parse_stooq(stooq_text)
        except Exception:
            st = None
        why, diff = cross_check(price_y, st, float(c["data_guard_tolerance_pct"]), bar_day)
        # a disagreement beyond the tolerance always blocks; a missing or off-day quote only when required
        (reasons if (diff is not None or c.get("require_second_source", True)) else warns).extend(why)
        if st is not None:
            rec["stooq_price"] = round(st["price"], 4)
            price = max(price_y, st["price"])
        if diff is not None:
            rec["diff_pct"] = diff
    rec["price"] = round(price, 4)
    rec["verdict"] = "OK" if not reasons else "BAD"
    return rec


# ---------------------------------------------------------------- files and commands

def read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return None


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


DEFAULTS_IN = dict(DEFAULTS)


def apply_market(a, doc):
    """Flag > env > config decides the market, once, before any time math."""
    set_market(getattr(a, "market", None) or os.environ.get("TRADER_MARKET")
               or (doc or {}).get("market"))


def load_cfg(a):
    cfg = read_json(a.config) or {}
    apply_market(a, cfg)
    if _MARKET != "US" and _mpl():
        _mpl().apply_inr_aliases(cfg, _MARKET)
    out = {k: cfg[k] for k in (DEFAULTS_IN if _MARKET != "US" else DEFAULTS) if k in cfg}
    if a.max_age_min is not None:
        out["data_guard_max_age_min"] = a.max_age_min
    if a.tolerance_pct is not None:
        out["data_guard_tolerance_pct"] = a.tolerance_pct
    if a.single_source:
        out["require_second_source"] = False
    return out


def report_line(sym, rec):
    head = "%-5s %s" % (rec["verdict"], sym)
    if "price" in rec:
        if "stooq_price" in rec:
            second = ", stooq %.2f, diff %.2f %%" % (rec["stooq_price"], rec.get("diff_pct", 0))
        elif "second_price" in rec:
            second = ", %s %.2f, diff %.2f %%" % (rec.get("second_source") or "manual",
                                                 rec["second_price"], rec.get("diff_pct", 0))
        else:
            second = ""
        head += "  price %.2f (yahoo %.2f%s), last bar %s %s, %ss old" % (
            rec["price"], rec.get("yahoo_price", 0), second, rec.get("last_bar", "?"),
            tz_word(), rec.get("age_sec", "?"))
    lines = [head]
    lines += ["        - " + r for r in rec["reasons"]]
    lines += ["        ~ " + w for w in rec["warnings"]]
    return lines


def cmd_data(a):
    cfg = load_cfg(a)                      # resolves the market before --now is parsed
    now = parse_now(a.now)
    syms = [s.upper() for s in a.symbols]
    if (a.yahoo_file or a.stooq_file) and len(syms) != 1:
        raise GuardError("--yahoo-file and --stooq-file work with exactly one symbol")
    if a.second_price is not None and len(syms) != 1:
        raise GuardError("--second-price works with exactly one symbol")
    manual = ({"price": a.second_price, "source": a.second_source or "manual"}
              if a.second_price is not None else None)
    out, first = {}, True
    for sym in syms:
        if not first:
            _sleep(PAUSE)
        first = False
        if a.yahoo_file:
            with open(a.yahoo_file, encoding="utf-8") as fh:
                ytext = fh.read()
        else:
            try:
                ytext = fetch_yahoo(sym)
            except GuardError as e:
                out[sym] = {"checked_at": iso_z(now), "verdict": "BAD", "warnings": [],
                            "reasons": ["fresh: could not get Yahoo's bars: %s" % e]}
                continue
        stext, serr = None, None
        if manual is not None:
            pass                              # the --second-price quote replaces Stooq
        elif a.stooq_file:
            with open(a.stooq_file, encoding="utf-8") as fh:
                stext = fh.read()
        elif _MARKET == "IN":
            pass                              # Stooq does not cover the NSE; judge() says so
        elif not a.yahoo_file:
            try:
                stext = fetch_stooq(sym)
            except GuardError as e:
                serr = "could not get Stooq's quote: %s" % e
        else:
            serr = "no --stooq-file given"
        out[sym] = judge(sym, ytext, stext, now, cfg, serr, manual)
    doc = {"created_by": "trader-guard", "checked_at": iso_z(now), "market": _MARKET, "symbols": out}
    if not a.no_write:
        old = read_json(a.out) or {}
        merged = dict(old.get("symbols") or {})
        merged.update(out)
        write_json(a.out, {"created_by": "trader-guard", "checked_at": iso_z(now),
                           "market": _MARKET, "symbols": merged})
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        for sym in syms:
            print("\n".join(report_line(sym, out[sym])))
        bad = [s for s in syms if out[s]["verdict"] != "OK"]
        print("")
        print("%d OK, %d BAD. trader-risk blocks an entry whose symbol is not OK within %s minutes."
              % (len(syms) - len(bad), len(bad), ("%g" % float(cfg.get("data_guard_max_age_min",
                                                                      DEFAULTS["data_guard_max_age_min"])))))
        print("Education and tooling, not financial advice.")
    return 0 if all(r["verdict"] == "OK" for r in out.values()) else 1


def cmd_show(a):
    apply_market(a, read_json(a.config) or {})
    now = parse_now(a.now)
    doc = read_json(a.out)
    if not doc or not doc.get("symbols"):
        print("Nothing recorded yet in %s. Run: guard.py data SYMBOL" % a.out)
        return 1
    for sym, rec in sorted(doc["symbols"].items()):
        when = None
        try:
            when = datetime.datetime.fromisoformat(rec["checked_at"].replace("Z", ""))
        except (KeyError, ValueError):
            pass
        age = ("%d s ago" % (now - when).total_seconds()) if when else "unknown time"
        print("%-5s %s (checked %s)" % (rec.get("verdict"), sym, age))
        for r in rec.get("reasons", []):
            print("        - " + r)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the market data before an order (trader-guard).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--out", default="trader/state/data_guard.json")
        p.add_argument("--market", choices=["US", "IN"], help="market profile (default US; IN = NSE)")
        p.add_argument("--now", help="pretend it is this market time, e.g. \"2026-10-19 10:15\"")

    d = sub.add_parser("data", help="check the data for one or more symbols")
    d.add_argument("symbols", nargs="+")
    common(d)
    d.add_argument("--max-age-min", type=float)
    d.add_argument("--tolerance-pct", type=float)
    d.add_argument("--single-source", action="store_true",
                   help="do not require the Stooq cross-check (weaker: use only if Stooq is down)")
    d.add_argument("--yahoo-file", help="read a saved Yahoo 1m chart JSON instead of the network")
    d.add_argument("--stooq-file", help="read a saved Stooq CSV instead of the network")
    d.add_argument("--second-price", type=float,
                   help="your broker's last price for the one symbol (e.g. from Kite); replaces Stooq")
    d.add_argument("--second-source", help="name of the --second-price source (default manual)")
    d.add_argument("--no-write", action="store_true")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_data)
    s = sub.add_parser("show", help="what was recorded")
    common(s)
    s.set_defaults(fn=cmd_show)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except GuardError as e:
        print("guard.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
