"""Shared engine for the three decision agents: trader-bull, trader-bear and
trader-judge. Python 3.8+, standard library only.

Bull and Bear import this module by sibling path (it lives in trader-judge/)
and refuse to run without it, so the two cases are written against exactly
the same numbers.

What it provides:
  - market plumbing: lib/market_profile.py (US default, IN for the NSE)
  - data: Yahoo Finance daily and 5-minute charts, the index chart for the
    market regime, Yahoo RSS headlines, and every agent's state file
  - indicators: averages, RSI, 5-minute ATR, 52-week position, traded value
  - a base-rate replay of the stop/target geometry over recent sessions,
    NET of the market's real round-trip costs (the india_intraday model on
    the NSE is roughly three times the US one: many setups honestly show
    no edge, and this engine says so)
  - the evidence model every side scores with

Nothing here places an order or touches a broker. It is a rules-based
opinion engine, not a prediction: it cannot see the future, and most
intraday traders lose money regardless of any score it prints.
"""

import datetime
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; day-trader-skill/3.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5
BACKOFF = 5.0
HOSTS = ("query2", "query1")
EPOCH = datetime.datetime(1970, 1, 1)

NOTE_CAP = 10          # research notes can never add or take more than this
EVENT_CAP = 10         # --event items together are capped the same way
REPLAY_MIN = 20        # fewer past triggers than this: the replay says nothing
DEFAULT_JUDGE = {
    "judge_bull_min": 65, "judge_bear_max": 35, "judge_gap_min": 25,
    "judge_wait_bull_min": 55, "judge_wait_bear_max": 45, "judge_wait_gap_min": 10,
    "judge_max_age_min": 10, "min_net_expectancy_r": 0.05,
    "liquidity_floor": None, "cost_r_veto": 0.25, "event_veto_days": 1, "event_risk_days": 3,
    "no_entries_before": "09:45", "no_entries_after": "15:00",
}


class EvError(Exception):
    pass


# ---------------------------------------------------------------- the lib

_MPL = None
_MPL_TRIED = False
_MARKET = "US"


def mpl():
    """lib/market_profile.py, searched beside the skill folders."""
    global _MPL, _MPL_TRIED
    if not _MPL_TRIED:
        _MPL_TRIED = True
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        for base in (here, os.path.join(here, "lib"), os.path.join(here, "..", "lib"),
                     os.path.join(here, "..", "..", "lib"),
                     os.path.join(here, "..", "trader-judge", "lib")):
            p = os.path.normpath(os.path.join(base, "market_profile.py"))
            if os.path.isfile(p):
                spec = importlib.util.spec_from_file_location("market_profile", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _MPL = mod
                break
    return _MPL


def set_market(code):
    global _MARKET
    if str(code or "").strip().upper() in ("IN", "INDIA", "NSE"):
        if mpl() is None:
            raise EvError('market "IN" needs lib/market_profile.py: keep the lib/ '
                          "folder next to the trader-* folders")
        _MARKET = "IN"
    else:
        _MARKET = "US"
    return _MARKET


def market():
    return _MARKET


def to_market(utc):
    return mpl().to_market(utc, _MARKET) if _MARKET != "US" else utc + datetime.timedelta(hours=-4)


def market_to_utc(local):
    return mpl().market_to_utc(local, _MARKET) if _MARKET != "US" else local + datetime.timedelta(hours=4)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def market_today(now_utc=None):
    """The trading date in MARKET time. For India this is the IST date, never
    the New York date: the NSE session runs 23:45-06:00 New York time."""
    return to_market(now_utc or utc_now()).date()


def mny(x):
    return mpl().fmt_price(_MARKET, x) if mpl() else "$%.2f" % x


def tzshort():
    return mpl().tz_short(_MARKET) if mpl() else "ET"


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return default


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path="trader/config.json"):
    doc = read_json(path) or {}
    if _MARKET != "US" and mpl():
        mpl().apply_inr_aliases(doc, _MARKET)
    return doc


def resolve_market(cli, doc):
    """flag > env TRADER_MARKET > config "market" > US."""
    return set_market(cli or os.environ.get("TRADER_MARKET") or (doc or {}).get("market"))


def plan_fingerprint(d):
    if mpl():
        return mpl().plan_fingerprint(d)
    parts = [str(d.get("symbol") or "").upper(), str(d.get("side") or "buy").lower(),
             str(d.get("date") or ""), "%.2f" % float(d.get("entry") or 0),
             "%.2f" % float(d.get("stop") or 0), "%.2f" % float(d.get("target") or 0)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- candidate

def hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EvError("%s wants a market time like 10:15, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def candidate(plan_path="trader/plan.json", symbol=None, side=None, entry=None,
              stop=None, target=None, date=None, entry_time=None, entry_bar=None):
    """One candidate trade: plan.json fields overridden by any flags given.
    -> the same dict exit.py writes, plus plan_fp."""
    plan = read_json(plan_path) or {}
    c = dict(plan)
    for k, v in (("symbol", symbol), ("side", side), ("entry", entry), ("stop", stop),
                 ("target", target), ("date", date), ("entry_time", entry_time),
                 ("entry_bar", entry_bar)):
        if v is not None:
            c[k] = v
    c["symbol"] = str(c.get("symbol") or "").upper()
    c["side"] = str(c.get("side") or "buy").lower()
    if not c["symbol"]:
        raise EvError("no symbol: pass one or run trader-exit first (it writes trader/plan.json)")
    for k in ("entry", "stop", "target"):
        try:
            c[k] = float(c[k])
        except (TypeError, ValueError):
            raise EvError("the plan needs a number for %s (plan.json or a flag)" % k)
    if c["side"] != "buy":
        raise EvError("long side only: this pack never plans a short or a sell entry")
    if not c.get("date"):
        c["date"] = market_today().isoformat()
    c["risk_per_share"] = c.get("risk_per_share") or round(c["entry"] - c["stop"], 4)
    c["plan_fp"] = plan_fingerprint(c)
    return c


# ---------------------------------------------------------------- Yahoo data

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_symbol(sym):
    if mpl():
        return mpl().yahoo_symbol(_MARKET, sym)
    return str(sym).upper().replace(".", "-")


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


def yahoo_get(path):
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EvError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EvError("Yahoo answered HTTP %d" % code)
            except (urllib.error.URLError, OSError) as e:
                last = "could not connect: %s" % getattr(e, "reason", e)
                break
    raise EvError("Yahoo did not answer (%s on query2 and query1)" % last)


def _sleep(seconds):
    import time
    time.sleep(seconds)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars:[{t,o,h,l,c,v}]}."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EvError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EvError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    import gzip
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def get_chart(sym, rng, interval, offline=None, filename=None):
    """Daily (range=1y&interval=1d) or intraday (range=60d&interval=5m) bars.
    offline may be a directory of <SYM>-<rng><interval>.json(.gz) files or a
    direct file path; filename overrides the lookup name for tests."""
    if offline:
        if os.path.isdir(offline):
            tried = []
            for name in ([filename] if filename else
                         ["%s-%s%s.json" % (yahoo_symbol(sym), rng, interval),
                          "%s-%s%s.json.gz" % (yahoo_symbol(sym), rng, interval),
                          "%s.json" % yahoo_symbol(sym), "%s.json.gz" % yahoo_symbol(sym)]):
                p = os.path.join(offline, name)
                tried.append(p)
                if os.path.isfile(p):
                    return parse_chart(read_text(p), sym)
            raise EvError("no offline chart for %s in %s (tried %s)"
                          % (sym, offline, ", ".join(os.path.basename(x) for x in tried)))
        if not os.path.isfile(offline):
            raise EvError("no offline file %s" % offline)
        return parse_chart(read_text(offline), sym)
    return parse_chart(yahoo_get(chart_path(sym, rng, interval)), sym)


def index_symbol():
    return mpl().profile(_MARKET)["index_symbol"] if mpl() else "^GSPC"


def fetch_all(cand, offline=None, index_offline=None):
    """The three data sets the evidence needs, one dict:
    daily (1y), bars5m (60d), index daily (1y)."""
    daily = get_chart(cand["symbol"], "1y", "1d", offline)
    bars5m = get_chart(cand["symbol"], "60d", "5m", index_offline if index_offline else offline)
    index = get_chart(index_symbol(), "1y", "1d", index_offline or offline)
    return {"daily": daily, "bars5m": bars5m, "index": index}


# ---------------------------------------------------------------- indicators

def sma(vals, n):
    vals = [v for v in vals if v is not None]
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / float(n)


def rsi(closes, n=14):
    """Wilder's RSI on a close list (needs n+1 closes)."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars, n=14):
    """Average true range over the last n bars (list of {h,l,c})."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / float(len(trs))


def pct_change(closes, days):
    if len(closes) < days + 1 or not closes[-days - 1]:
        return None
    return closes[-1] / closes[-days - 1] - 1.0


def week52_pos(closes):
    if not closes:
        return None
    lo, hi = min(closes), max(closes)
    if hi <= lo:
        return 0.5
    return (closes[-1] - lo) / (hi - lo)


def avg_daily_value(daily_bars, n=20):
    """Average close*volume over the last n daily bars: the liquidity number."""
    vals = [b["c"] * b["v"] for b in daily_bars[-n:] if b.get("v")]
    return sum(vals) / float(len(vals)) if vals else None


def sessions(bars5m, market=None):
    """Group 5-minute bars into {date: [bars]} by market-local session day,
    regular session only."""
    m = market or _MARKET
    out = {}
    open_t = mpl().profile(m)["open"] if mpl() else datetime.time(9, 30)
    close_t = mpl().profile(m)["close"] if mpl() else datetime.time(16, 0)
    for b in bars5m:
        local = to_market(utc_of(b["t"]))
        if not (open_t <= local.time() < close_t):
            continue
        if local.time() >= datetime.time(0, 0) and b["t"] % 300:
            continue                      # off-grid tick, not a 5-minute bar
        out.setdefault(local.date(), []).append(b)
    for d in out:
        out[d].sort(key=lambda x: x["t"])
    return out


def bar_slot(bar_local_time, market=None):
    """5-minute slot index of a market-local time since the open (0-based)."""
    m = market or _MARKET
    o = mpl().profile(m)["open"] if mpl() else datetime.time(9, 30)
    mins = (bar_local_time.hour * 60 + bar_local_time.minute) - (o.hour * 60 + o.minute)
    return mins // 5


def entry_slot(cand, day_bars=None):
    """The slot the plan's entry sits in: entry_bar if recorded, else the
    entry_time HH:MM, else the bar whose close first crossed the entry."""
    if cand.get("entry_bar") is not None:
        try:
            return int(cand["entry_bar"])
        except (TypeError, ValueError):
            pass
    t = cand.get("entry_time")
    if t:
        tt = hhmm(str(t).replace(" AM", "").replace(" PM", ""), "entry_time")
        return bar_slot(tt)
    return 10     # 10:05-10:10 area: a sane mid-morning default


def cost_r_per_share(entry_px, risk_ps, cfg=None):
    """Round-trip cost of one share in R units, entry ~= exit price."""
    c = cost_share(entry_px, entry_px, cfg)
    return c / risk_ps if risk_ps else 0.0


def cost_share(buy_px, sell_px, cfg=None):
    if mpl():
        return mpl().round_trip_cost(_MARKET, 1, buy_px, sell_px, cfg or {})
    return 2 * 0.005 + 0.0


def replay_geometry(day_bars, slot, stop_frac, target_frac, end_slot, cfg=None):
    """Base-rate replay: fire the same stop/target geometry at the same time
    of day on every past session, walk the bars, count the outcomes NET of the
    market's real costs.

    day_bars    {date: [5m bars]} sorted sessions
    slot        the 5-minute slot the entry fires at (its close = the fill)
    stop_frac   (entry - stop) / entry
    target_frac (target - entry) / entry
    end_slot    last slot to exit at (flat by)
    -> {"trades","target","stop","neither","win_rate","avg_R","cost_R","avg_R_net"}"""
    out = {"trades": 0, "target": 0, "stop": 0, "neither": 0,
           "win_rate": None, "avg_R": None, "cost_R": None, "avg_R_net": None}
    rs, crs = [], []
    for d in sorted(day_bars):
        bars = day_bars[d]
        if slot >= len(bars):
            continue
        e = bars[slot]["c"]
        if not e:
            continue
        stop_px, target_px = e * (1 - stop_frac), e * (1 + target_frac)
        risk = e - stop_px
        if risk <= 0:
            continue
        last = min(end_slot, len(bars) - 1)
        r_out = (bars[last]["c"] - e) / risk
        hit = False
        for b in bars[slot + 1:last + 1]:
            if b["l"] <= stop_px:          # the stop is checked first: worse case
                r_out = -1.0
                hit = True
                out["stop"] += 1
                break
            if b["h"] >= target_px:
                r_out = (target_px - e) / risk
                hit = True
                out["target"] += 1
                break
        if not hit:
            out["neither"] += 1
        cr = cost_share(e, e, cfg) / risk
        crs.append(cr)
        rs.append(r_out - cr)
        out["trades"] += 1
    if out["trades"]:
        gross = [r + c for r, c in zip(rs, crs)]
        out["win_rate"] = sum(1 for r in gross if r > 0) / float(len(gross))
        out["avg_R"] = sum(gross) / len(gross)
        out["cost_R"] = sum(crs) / len(crs)
        out["avg_R_net"] = sum(rs) / len(rs)
    return out


# ---------------------------------------------------------------- headlines

IN_TAGS = [
    ("earnings", r"\bearnings?\b|\bEPS\b|quarterly|Q[1-4]\s*FY|results?\b|board meeting"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|surveillance|\bASM\b|\bGSM\b|penalty|fraud|raid\b|ban\b"),
    ("macro", r"\bMPC\b|monetary policy|repo rate|inflation|\bCPI\b|\bFII\b|\bFPI\b|\bDII\b|\bGST\b"),
    ("deal", r"\bmerger\b|\bacquisition\b|\bstake\b|\bbuyout\b|\bdeal\b|\bJV\b|joint venture|order win|contract"),
    ("analyst", r"upgrade|downgrade|target price|brokerage|initiated|rating"),
    ("halt", r"\bhalt|circuit|suspended"),
]
US_TAGS = [
    ("earnings", r"\bearnings?\b|\bEPS\b|guidance|results?\b"),
    ("regulatory", r"\bSEC\b|\bFDA\b|\bFTC\b|\bDOJ\b|probe|investigation|fraud|recall"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|rate hike|rate cut|payrolls"),
    ("deal", r"\bmerger\b|\bacquisition\b|\bstake\b|\bbuyout\b|\bdeal\b"),
    ("analyst", r"upgrade|downgrade|price target|initiated|rating"),
    ("halt", r"\bhalt|suspended"),
]


def tag_headline(title):
    text = title or ""
    tags = [name for name, rx in (IN_TAGS if _MARKET == "IN" else US_TAGS)
            if re.search(rx, text, re.I)]
    return tags


def fetch_headlines(sym, offline=None):
    """Yahoo RSS headlines for one ticker -> [{"title","time(utc)","tags"}].
    offline is a saved RSS file path."""
    items = []
    if offline:
        text = read_text(offline)
    else:
        region = "IN&lang=en-IN" if _MARKET == "IN" else "US&lang=en-US"
        url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=%s" % (
            urllib.parse.quote(yahoo_symbol(sym)), region)
        try:
            text = _open_url(url)
        except Exception:
            return items
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        when = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                dt = datetime.datetime.strptime(pub, fmt)
                when = (dt - (dt.utcoffset() or datetime.timedelta())).replace(tzinfo=None)
                break
            except ValueError:
                continue
        items.append({"title": title, "time": when, "tags": tag_headline(title)})
    return items


# ---------------------------------------------------------------- other agents

def read_state(plan_path="trader/plan.json", entry_path="trader/entry.json",
               guard_path="trader/state/data_guard.json",
               tm_path="trader/timemachine.json", analyst_path="trader/state/analyst.json",
               news_path="trader/state/news.json", config_path="trader/config.json"):
    """Everything the earlier agents wrote, in one dict. Missing files are
    normal: they just mean that agent has not run."""
    return {"plan": read_json(plan_path), "entry": read_json(entry_path),
            "guard": read_json(guard_path), "timemachine": read_json(tm_path),
            "analyst": read_json(analyst_path), "news": read_json(news_path),
            "config": load_config(config_path)}


def guard_verdict(state, sym):
    """('OK'|'BAD'|None, reasons list) from trader-guard's file."""
    recs = ((state.get("guard") or {}).get("symbols") or {})
    rec = recs.get(sym)
    if rec is None and mpl():
        rec = recs.get(mpl().yahoo_symbol(_MARKET, sym)) or recs.get(mpl().bare_symbol(sym))
    if not rec:
        return None, []
    return rec.get("verdict"), (rec.get("reasons") or [])


def entry_confirmed(state, cand):
    """True when trader-entry's file says this symbol/date triggered."""
    e = state.get("entry") or {}
    if not e:
        return False
    esym = str(e.get("symbol") or "").upper()
    if mpl() and mpl().bare_symbol(esym) != mpl().bare_symbol(cand["symbol"]):
        return False
    if not mpl() and esym != cand["symbol"]:
        return False
    return str(e.get("date")) == str(cand.get("date")) and e.get("status") == "triggered"


def analyst_events(state, cand):
    """Dates the analyst knows about for this symbol (results, board meets)."""
    a = state.get("analyst") or {}
    if not a:
        return []
    sym = str(a.get("symbol") or "").upper()
    if mpl() and sym and mpl().bare_symbol(sym) != mpl().bare_symbol(cand["symbol"]):
        return []
    out = []
    if a.get("next_results"):
        out.append({"date": str(a["next_results"]), "what": "results (trader-analyst)",
                    "source": "trader/state/analyst.json"})
    for ev in a.get("events") or []:
        if isinstance(ev, dict) and ev.get("date"):
            out.append({"date": str(ev["date"]), "what": str(ev.get("what") or "event"),
                        "source": "trader/state/analyst.json"})
    return out


def cli_events(args_list):
    """--event 'YYYY-MM-DD text' flags -> the same shape as analyst_events."""
    out = []
    for raw in args_list or []:
        m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:\-]?\s*(.*)$", str(raw))
        if not m:
            raise EvError('--event wants "YYYY-MM-DD what happens", got %r' % raw)
        out.append({"date": m.group(1), "what": m.group(2) or "event", "source": "--event"})
    return out


def days_until(date_str, today):
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    return (d - today).days


def liquidity_floor(cfg):
    """Average daily traded value floor in account currency."""
    v = (cfg or {}).get("liquidity_floor")
    if v is not None:
        return float(v)
    return 5e7 if _MARKET == "IN" else 1e7     # Rs 5 crore / $10M


def timemachine_edge(state):
    """The edge numbers trader-timemachine wrote, or None."""
    tm = state.get("timemachine") or {}
    costs = tm.get("costs") or {}
    if "edge_ok" not in costs and not (tm.get("mc") or {}).get("avg_R"):
        return None
    mc = tm.get("mc") or {}
    hist = tm.get("history") or {}
    return {"edge_ok": costs.get("edge_ok"), "edge_reasons": costs.get("edge_reasons") or [],
            "mc_avg_R": mc.get("avg_R"), "history_avg_R": hist.get("avg_R"),
            "history_trades": len(hist.get("trades") or [])}


# ---------------------------------------------------------------- evidence

def item(eid, claim, value, weight, strength, source, kind):
    """One evidence item. points = weight*strength is its signed contribution
    to the side's 0-100 score (a side argues FOR itself; negative strength
    argues against)."""
    strength = max(-1.0, min(1.0, float(strength)))
    return {"id": eid, "claim": claim, "value": value, "weight": round(weight, 2),
            "strength": round(strength, 3), "points": round(weight * strength, 2),
            "source": source, "kind": kind}


def score(items):
    return int(round(max(0.0, min(100.0, sum(i["points"] for i in items)))))


def top(items, n=3):
    return sorted(items, key=lambda i: -abs(i["points"]))[:n]


def note_items(notes, notes_points, sources):
    """--note TEXT --note-points N --source URL triples, capped at NOTE_CAP
    total either way, kind 'research'."""
    out, total = [], 0.0
    for i, text in enumerate(notes or []):
        pts = 0.0
        try:
            pts = float((notes_points or [])[i])
        except (IndexError, TypeError, ValueError):
            pass
        pts = max(-NOTE_CAP, min(NOTE_CAP, pts))
        left = NOTE_CAP - abs(total)
        pts = max(-left, min(left, pts)) * (1 if pts >= 0 else -1) if left >= 0 else 0.0
        total += pts
        src = (sources or [None])[i] if i < len(sources or []) else "--note"
        out.append(item("research_%d" % (i + 1), text, pts, NOTE_CAP,
                        pts / NOTE_CAP if NOTE_CAP else 0, src or "--note", "research"))
    return out


def event_items(events, today, bullish=True):
    """--event / analyst dates -> 'future' evidence items, capped at EVENT_CAP."""
    out, total = [], 0.0
    for i, e in enumerate(events or []):
        dd = days_until(e.get("date"), today)
        if dd is None or dd < 0:
            continue
        pts = min(5.0, EVENT_CAP - total)
        if pts <= 0:
            break
        total += pts
        label = "%s on %s (%s)" % (e.get("what", "event"), e["date"],
                                   "today" if dd == 0 else "in %d day%s" % (dd, "s" if dd != 1 else ""))
        out.append(item("event_%d" % (i + 1), label, dd, 5.0, (pts / 5.0) * (1 if bullish else -1),
                        e.get("source") or "--event", "future"))
    return out


def context(cand, data, state, cfg=None):
    """The shared fact sheet both sides argue from. Every number in one place,
    so the bull and the bear can never be looking at different data."""
    daily, index = data["daily"]["bars"], data["index"]["bars"]
    dcl = [b["c"] for b in daily]
    icl = [b["c"] for b in index]
    day_bars = sessions(data["bars5m"]["bars"])
    cfg = cfg or {}
    slot = entry_slot(cand)
    stop_frac = (cand["entry"] - cand["stop"]) / cand["entry"]
    target_frac = (cand["target"] - cand["entry"]) / cand["entry"]
    end_slot = (mpl().bars_per_session(_MARKET, "5m") - 1) if mpl() else 77
    rep = replay_geometry(day_bars, slot, stop_frac, target_frac, end_slot, cfg)
    adv = avg_daily_value(daily)
    last_vol = daily[-1]["v"] if daily else None
    vol_avg = sma([b["v"] for b in daily], 20)
    risk_ps = cand["entry"] - cand["stop"]
    today = datetime.date.fromisoformat(str(cand.get("date") or market_today().isoformat()))
    return {
        "symbol": cand["symbol"], "close": dcl[-1] if dcl else None,
        "sma20": sma(dcl, 20), "sma50": sma(dcl, 50), "sma200": sma(dcl, 200),
        "rsi14": rsi(dcl, 14), "ret_3m": pct_change(dcl, 63), "idx_ret_3m": pct_change(icl, 63),
        "idx_close": icl[-1] if icl else None, "idx_sma50": sma(icl, 50),
        "week52": week52_pos(dcl), "adv": adv, "last_vol": last_vol, "vol20": vol_avg,
        "vol_ratio": (last_vol / vol_avg) if (last_vol and vol_avg) else None,
        "atr5m": atr(data["bars5m"]["bars"], 14),
        "replay": rep, "entry_slot": slot,
        "cost_r": cost_r_per_share(cand["entry"], risk_ps, cfg),
        "rr": ((cand["target"] - cand["entry"]) / risk_ps) if risk_ps > 0 else None,
        "risk_ps": risk_ps, "today": today,
        "guard": guard_verdict(state, cand["symbol"]),
        "entry_confirmed": entry_confirmed(state, cand),
        "events": analyst_events(state, cand),
        "tm_edge": timemachine_edge(state),
        "sessions_n": len(day_bars),
    }
