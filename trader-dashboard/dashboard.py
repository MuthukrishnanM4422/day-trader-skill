#!/usr/bin/env python3
"""The Dashboard: one self-contained page that says which Indian-market
trade to choose today, and walks you through following it step by step.

    python3 dashboard.py build                 # write trader/dashboard.html
    python3 dashboard.py build --watch         # rebuild every 30s while the market is open
    python3 dashboard.py serve [--port 8765]   # serve trader/ on 127.0.0.1 only

The page is plain HTML, CSS and JavaScript in ONE file: no frameworks, no
build step, no CDN, no fonts, images or network calls of any kind. It opens
with a double-click (a file:// page cannot fetch JSON, so the data is
embedded). It only displays: no button on it places, edits or cancels an
order. The only actions are copy-to-clipboard for commands and the order
sheet, a theme toggle, and refresh.

Every number on the page comes from an agent's own file. If a file is
missing the panel says which agent produces it and the exact command to run.
The banner is honest: most intraday traders lose money, and everything here
is a rules-based opinion, not a prediction or advice.
"""

import argparse
import datetime
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


class DashError(Exception):
    pass


# ----------------------------------------------------------- market profile

_MPL = None
_MPL_TRIED = False
_MARKET = "US"


def _mpl():
    global _MPL, _MPL_TRIED
    if not _MPL_TRIED:
        _MPL_TRIED = True
        import importlib.util
        here = _HERE
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
    global _MARKET
    if str(code or "").strip().upper() in ("IN", "INDIA", "NSE"):
        if _mpl() is None:
            raise DashError('market "IN" needs lib/market_profile.py: keep the lib/ '
                            "folder next to the trader-* folders")
        _MARKET = "IN"
    else:
        _MARKET = "US"


def market():
    return _MARKET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


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


def read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def file_rec(path, data):
    """Presence, age and contents of one agent's output file."""
    rec = {"present": os.path.exists(path), "age_sec": None, "data": data}
    if rec["present"]:
        rec["age_sec"] = max(0, int(time.time() - os.path.getmtime(path)))
    return rec


# ---------------------------------------------------------------- snapshot

STATE_FILES = {
    "watchlist": "trader/watchlist.json",
    "entry": "trader/entry.json",
    "plan": "trader/plan.json",
    "timemachine": "trader/timemachine.json",
    "guard": "trader/state/data_guard.json",
    "bull": "trader/bull.json",
    "bear": "trader/bear.json",
    "judge": "trader/state/judge.json",
    "judge_log": "trader/state/judge_log.json",
    "risk": "trader/state/risk.json",
    "orders": "trader/orders.json",
    "prices": "trader/prices.json",
    "broker": "trader/state/broker.json",
    "portfolio": "trader/portfolio.json",
    "news": "trader/state/news.json",
    "analyst": "trader/state/analyst.json",
    "alerts": "trader/alerts.json",
}


def _order_money(o):
    for k in ("limit_price", "stop_price"):
        try:
            return float(o[k])
        except (KeyError, TypeError, ValueError):
            pass
    plan = o.get("plan") or {}
    try:
        return float(plan["entry"])
    except (KeyError, TypeError, ValueError):
        return None


def _candidates(files, cfg, mkt):
    """Every symbol the pipeline has a Judge verdict for today, ranked:
    BUY, then WAIT, then NO-BUY; then conviction, then net expectancy."""
    out = {}
    today = None
    for name, path in (("judge", STATE_FILES["judge"]),):
        j = files[name]["data"] or {}
        if j.get("symbol"):
            today = today or j.get("date")
            out[str(j["symbol"]).upper()] = _cand_from_judge(j)
    jdir = os.path.join(os.path.dirname(STATE_FILES["judge"]), "judges")
    if os.path.isdir(jdir):
        for f in sorted(os.listdir(jdir)):
            if not f.endswith(".json"):
                continue
            j = read_json(os.path.join(jdir, f)) or {}
            if j.get("symbol") and j.get("plan_fp"):
                out[str(j["symbol"]).upper()] = _cand_from_judge(j)
    plan = files["plan"]["data"] or {}
    orders = (files["orders"]["data"] or {}).get("orders") or []
    risk = files["risk"]["data"] or {}
    risk_by_sym = {}
    for r in risk.get("results") or []:
        risk_by_sym[str(r.get("symbol") or "").upper()] = r
    for o in orders:
        sym = str(o.get("symbol") or "").upper()
        c = out.setdefault(sym, {"symbol": sym})
        c["order"] = {"qty": o.get("qty"), "type": o.get("type"),
                      "entry": _order_money(o),
                      "stop": o.get("stop_loss_stop_price") or (o.get("plan") or {}).get("stop"),
                      "target": o.get("take_profit_limit_price") or (o.get("plan") or {}).get("target"),
                      "max_loss": (o.get("plan") or {}).get("max_loss"),
                      "flat_by": (o.get("plan") or {}).get("flat_by")}
        pl = o.get("plan") or {}
        try:
            c["order"]["entry"] = c["order"]["entry"] or float(pl["entry"])
        except (KeyError, TypeError, ValueError):
            pass
    if plan.get("symbol"):
        sym = str(plan["symbol"]).upper()
        c = out.setdefault(sym, {"symbol": sym})
        c["plan"] = {"entry": plan.get("entry"), "stop": plan.get("stop"),
                     "target": plan.get("target"), "date": plan.get("date"),
                     "flat_by": plan.get("flat_by"), "reward_risk": plan.get("reward_risk"),
                     "plan_fp": plan.get("plan_fp")}
    for sym, r in risk_by_sym.items():
        c = out.setdefault(sym, {"symbol": sym})
        c["risk"] = {"verdict": r.get("verdict"), "reasons": r.get("reasons") or [],
                     "max_loss": r.get("max_loss"), "reward_risk": r.get("reward_risk"),
                     "value": r.get("value_usd")}
    for c in out.values():
        det = c.get("order") or c.get("plan") or {}
        for k in ("entry", "stop", "target"):
            if c.get("order") is None and det.get(k) is not None:
                try:
                    det[k] = float(det[k])
                except (TypeError, ValueError):
                    pass
        qty = (c.get("order") or {}).get("qty")
        try:
            qty = int(float(qty))
        except (TypeError, ValueError):
            qty = None
        entry = det.get("entry")
        if qty and entry:
            c["shares"] = qty
            c["invested"] = round(qty * float(entry), 2)
            if _mpl() and det.get("stop") and det.get("target"):
                c["est_cost"] = round(_mpl().round_trip_cost(
                    mkt, qty, float(entry), float(det["target"]), cfg), 2)
        c["verdict"] = (c.get("judge") or {}).get("verdict") or "UNJUDGED"
        c["bull"] = (c.get("judge") or {}).get("scores", {}).get("bull")
        c["bear"] = (c.get("judge") or {}).get("scores", {}).get("bear")
        c["gap"] = (c.get("judge") or {}).get("scores", {}).get("gap")
        c["conviction"] = (c.get("judge") or {}).get("conviction")
        c["expectancy_r"] = (c.get("judge") or {}).get("expectancy_r")
        c["vetoes"] = (c.get("judge") or {}).get("vetoes") or []
        c["explanation"] = (c.get("judge") or {}).get("explanation")
        c["risk_verdict"] = (c.get("risk") or {}).get("verdict")
        c["risk_reasons"] = (c.get("risk") or {}).get("reasons") or []
        c["max_loss"] = (c.get("order") or {}).get("max_loss") or (c.get("risk") or {}).get("max_loss")
        c["rr"] = (c.get("plan") or {}).get("reward_risk") or (c.get("risk") or {}).get("reward_risk")
    rank = {"BUY": 0, "WAIT": 1, "NO-BUY": 2, "UNJUDGED": 3}
    conv = {"high": 0, "medium": 1, "low": 2, None: 3}
    cands = sorted(out.values(), key=lambda c: (
        rank.get(c["verdict"], 9), conv.get(c["conviction"], 9),
        -(c["expectancy_r"] or -9), c["symbol"]))
    return cands, today


def _cand_from_judge(j):
    return {"judge": {"verdict": j.get("verdict"), "conviction": j.get("conviction"),
                      "scores": j.get("scores") or {}, "expectancy_r": j.get("expectancy_r"),
                      "vetoes": j.get("vetoes") or [], "explanation": j.get("explanation"),
                      "for": j.get("for") or [], "against": j.get("against") or [],
                      "date": j.get("date"), "plan_fp": j.get("plan_fp"),
                      "created_at": j.get("created_at")}}


def _broker_summary(broker):
    recs = (broker or {}).get("orders") or []
    fills = [r for r in recs if r.get("status") == "filled"]
    days = sorted({str(r.get("date") or str(r.get("at", ""))[:10]) for r in fills if r})
    paper = all(r.get("paper", True) for r in fills) if fills else True
    return {"records": len(recs), "fills": len(fills), "days": days, "paper": paper,
            "today_fills": [r for r in fills
                            if str(r.get("date") or "") == "" or False]}


def _paper_record(broker, cfg, mkt):
    """Closed paper trades after costs: same FIFO as the risk manager."""
    fills = [r for r in (broker or {}).get("orders", []) if r.get("status") == "filled"]
    fills.sort(key=lambda r: (str(r.get("date") or str(r.get("at", ""))[:10]), str(r.get("at", ""))))
    lots, closed, curve = {}, [], []
    for r in fills:
        sym = str(r.get("symbol", "")).upper()
        try:
            q, px = float(r.get("qty")), float(r.get("price"))
        except (TypeError, ValueError):
            continue
        side = str(r.get("side", "")).lower()
        day = str(r.get("date") or str(r.get("at", ""))[:10])
        if side == "buy":
            lots.setdefault(sym, []).append([q, px, r.get("paper") is True])
        elif side == "sell":
            left = q
            while left > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(left, lot[0])
                gross = (px - lot[1]) * take
                if _mpl():
                    costs = _mpl().round_trip_cost(mkt, take, lot[1], px, cfg)
                else:
                    costs = 0.0
                pnl = gross - costs
                day_lots = lots  # noqa: F841 (documenting the lots dict use)
                closed.append({"day": day, "symbol": sym, "pnl": round(pnl, 2),
                               "paper": bool(lot[2] and r.get("paper") is True)})
                curve.append(round((curve[-1] if curve else 0.0) + pnl, 2))
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    lots[sym].pop(0)
    paper = [t for t in closed if t["paper"]]
    wins = [t for t in paper if t["pnl"] > 0]
    loss = -sum(t["pnl"] for t in paper if t["pnl"] < 0)
    gain = sum(t["pnl"] for t in paper if t["pnl"] > 0)
    return {"closed": len(paper), "wins": len(wins),
            "win_rate": round(len(wins) / len(paper), 3) if paper else None,
            "net": round(sum(t["pnl"] for t in paper), 2),
            "profit_factor": (round(gain / loss, 2) if loss > 0 else
                              ("inf" if gain > 0 else None)),
            "days": len({t["day"] for t in paper}), "curve": curve,
            "trades": paper[-20:]}


def _judge_review(log, broker, cfg, mkt):
    pnl = _paper_record(broker, cfg, mkt)
    by = {}
    for t in pnl["trades"]:
        by[(t["symbol"].upper(), t["day"])] = by.get((t["symbol"].upper(), t["day"]), 0) + t["pnl"]
    rows = []
    for d in (log or {}).get("decisions") or []:
        rows.append({"date": d.get("date"), "symbol": d.get("symbol"), "verdict": d.get("verdict"),
                     "bull": d.get("bull"), "bear": d.get("bear"),
                     "realized": by.get((str(d.get("symbol") or "").upper(), str(d.get("date") or "")))})
    buys = [r for r in rows if r["verdict"] == "BUY" and r["realized"] is not None]
    return {"calls": len(rows), "buys_closed": len(buys),
            "buys_won": sum(1 for r in buys if r["realized"] > 0), "rows": rows[-15:]}


def snapshot(root=".", market=None, cfg=None, now_utc=None):
    """Every agent's file rolled into one JSON-able dict for the page."""
    mkt = market or _MARKET
    cfg = cfg or {}
    now_utc = now_utc or utc_now()
    files = {}
    for name, rel in STATE_FILES.items():
        files[name] = file_rec(os.path.join(root, rel), read_json(os.path.join(root, rel)))
    cands, plan_date = _candidates(files, cfg, mkt)
    status = {"status": "unknown", "detail": "no market profile"}
    cal = {}
    if _mpl():
        status["status"], status["detail"] = _mpl().market_status(mkt, now_utc)
        p = _mpl().profile(mkt)
        cal = {"open": "%02d:%02d" % (p["open"].hour, p["open"].minute),
               "close": "%02d:%02d" % (p["close"].hour, p["close"].minute),
               "tz_short": p["tz_short"], "tz_label": p["tz_label"],
               "holidays": p["holidays"], "offset_hours": 5.5 if mkt == "IN" else None,
               "exchange": p["exchange"]}
        local = _mpl().to_market(now_utc, mkt)
        status["local_time"] = local.strftime("%H:%M")
        status["local_date"] = local.date().isoformat()
        ok, why = _mpl().is_trading_day(mkt, local.date())
        status["trading_day"] = ok
        status["closed_why"] = why
        nxt = _mpl().next_session(mkt, local.date())
        status["next_open"] = "%s %02d:%02d" % (nxt[0].isoformat(), nxt[1].hour, nxt[1].minute) if nxt else None
    broker = files["broker"]["data"] or {}
    guard = files["guard"]["data"] or {}
    tm = files["timemachine"]["data"] or {}
    snap = {
        "generated_at": iso(now_utc),
        "market": mkt,
        "calendar": cal,
        "status": status,
        "config": {k: cfg.get(k) for k in (
            "paper", "budget", "max_loss_per_trade_usd", "max_daily_loss_usd",
            "max_weekly_loss_usd", "max_trades_per_day", "max_drawdown_pct",
            "max_consecutive_loss_days", "live_min_paper_days", "live_min_paper_trades",
            "live_min_profit_factor", "no_entries_before", "no_entries_after", "flat_by",
            "require_judge", "require_second_source", "data_guard_max_age_min",
            "judge_max_age_min", "min_net_expectancy_r", "broker", "cost_model")},
        "candidates": cands,
        "files": {n: {"present": f["present"], "age_sec": f["age_sec"]} for n, f in files.items()},
        "plan_date": plan_date,
        "judge": files["judge"]["data"],
        "bull": files["bull"]["data"],
        "bear": files["bear"]["data"],
        "risk": files["risk"]["data"],
        "plan": files["plan"]["data"],
        "orders": files["orders"]["data"],
        "guard": files["guard"]["data"],
        "timemachine": {"verdict": tm.get("verdict"), "edge": tm.get("costs"),
                        "mc": {k: (tm.get("mc") or {}).get(k) for k in
                               ("p_target", "p_stop", "p_neither", "avg_R", "replays")},
                        "plan": tm.get("plan")} if tm else None,
        "entry": files["entry"]["data"],
        "news": {"last_check": (files["news"]["data"] or {}).get("last_check"),
                 "seen": len((files["news"]["data"] or {}).get("seen") or {})},
        "prices": files["prices"]["data"],
        "broker_summary": _broker_summary(broker),
        "record": _paper_record(broker, cfg, mkt),
        "judge_review": _judge_review(files["judge_log"]["data"], broker, cfg, mkt),
        "alerts": files["alerts"]["data"],
        "note": ("Most intraday traders lose money. This is a rules-based opinion, "
                 "not a prediction or advice."),
    }
    return snap


# ---------------------------------------------------------------- the page

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Trader Dashboard - which trade today</title>
<style>
:root{
  --bg:#f6f7f9; --card:#ffffff; --ink:#17202a; --muted:#5b6570; --line:#dbe1e8;
  --accent:#0b5ed7; --good:#0f7a3d; --warn:#a05a00; --bad:#b3261e; --chip:#eef2f7;
  --focus:#7ab3ff;
}
[data-theme="dark"]{
  --bg:#10151c; --card:#1a212b; --ink:#e6ebf1; --muted:#9aa7b4; --line:#2c3744;
  --accent:#6ea8ff; --good:#54c47e; --warn:#ffb354; --bad:#ff7b72; --chip:#232c38;
  --focus:#2f6fd0;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#10151c; --card:#1a212b; --ink:#e6ebf1; --muted:#9aa7b4; --line:#2c3744;
    --accent:#6ea8ff; --good:#54c47e; --warn:#ffb354; --bad:#ff7b72; --chip:#232c38;
    --focus:#2f6fd0;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.banner{background:#7a1f1a;color:#fff;padding:10px 16px;text-align:center;font-weight:600}
[data-theme="dark"] .banner{background:#5c1713}
.topbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;
  padding:10px 0}
.clock{font-size:1.6rem;font-weight:700}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:700;font-size:.8rem}
.badge.paper{background:var(--chip);color:var(--accent);border:1px solid var(--accent)}
.badge.live{background:var(--bad);color:#fff}
.badge.open{background:var(--good);color:#fff}
.badge.closed{background:var(--muted);color:#fff}
.badge.buy{background:var(--good);color:#fff}
.badge.wait{background:var(--warn);color:#fff}
.badge.nobuy{background:var(--bad);color:#fff}
.badge.unjudged{background:var(--muted);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px;margin:12px 0}
.card h2{margin:0 0 8px;font-size:1.05rem}
.hero{border:2px solid var(--accent)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:8px 0}
.kv dt{color:var(--muted)} .kv dd{margin:0;font-variant-numeric:tabular-nums}
.bar{height:14px;border-radius:7px;background:var(--chip);position:relative;overflow:hidden}
.bar > span{position:absolute;top:0;bottom:0;border-radius:7px}
.bar.bull > span{background:var(--good)} .bar.bear > span{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:.9rem}
th{color:var(--muted);font-weight:600;cursor:pointer;user-select:none}
.step{border:1px solid var(--line);border-radius:10px;margin:6px 0;padding:10px 12px}
.step .st{font-weight:700;font-size:.8rem;letter-spacing:.03em}
.step.done{border-left:4px solid var(--good)}
.step.current{border-left:4px solid var(--accent);background:var(--chip)}
.step.blocked{border-left:4px solid var(--bad)}
.step.todo{opacity:.75}
.step .why{color:var(--muted);font-size:.85rem;margin-top:2px}
.cmd{display:flex;gap:8px;align-items:center;margin-top:6px}
code{background:var(--chip);padding:2px 6px;border-radius:6px;font-size:.85rem;
  overflow-wrap:anywhere}
button{cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);
  border-radius:8px;padding:4px 10px;font:inherit}
button:focus-visible,[tabindex]:focus-visible,a:focus-visible{outline:3px solid var(--focus)}
.muted{color:var(--muted)} .small{font-size:.85rem}
.veto{color:var(--bad);font-weight:600}
.ok{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)}
pre{white-space:pre-wrap;background:var(--chip);padding:10px;border-radius:8px;
  overflow-wrap:anywhere;font-size:.85rem}
.gate{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid var(--line);
  padding:5px 0}
details{margin:4px 0} summary{cursor:pointer}
@media (max-width:720px){.grid2{grid-template-columns:1fr}.wrap{padding:16px}}
</style>
</head>
<body>
<div class="banner" id="honest"></div>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="clock" id="clock">--:--</div>
      <div class="small muted" id="mstatus">market status</div>
      <div class="small muted" id="timers"></div>
    </div>
    <div>
      <span class="badge" id="modebadge">PAPER</span>
      <button id="themebtn" type="button">Theme</button>
    </div>
  </div>
  <div id="hero"></div>
  <div class="grid2">
    <div id="versus"></div>
    <div id="explain"></div>
  </div>
  <div id="table"></div>
  <div id="steps"></div>
  <div class="grid2">
    <div id="safety"></div>
    <div id="record"></div>
  </div>
  <div id="ordersheet"></div>
  <div id="help"></div>
  <p class="small muted" id="footer"></p>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
/* Every decision the page makes lives in these plain functions so they can
   be unit tested. Data comes only from the embedded snapshot; nothing here
   touches the network and nothing can place an order. */
var DATA = JSON.parse(document.getElementById("data").textContent);
var MKT = DATA.market || "US";

function esc(s){ return String(s == null ? "" : s); }
function el(tag, cls, text){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function groupIN(intpart){
  var s = String(intpart), out = "", tail = s.slice(-3), head = s.slice(0, -3);
  if (!head) return s;
  while (head.length > 2){ out = "," + head.slice(-2) + out; head = head.slice(0, -2); }
  return head + out + "," + tail;
}
function fmtMoney(x, dp){
  if (x === null || x === undefined || isNaN(Number(x))) return "not available";
  var v = Number(x), neg = v < 0, t = Math.abs(v).toFixed(dp === undefined ? 2 : dp);
  var parts = t.split(".");
  var intp = MKT === "IN" ? groupIN(parts[0]) : Number(parts[0]).toLocaleString("en-US");
  return (neg ? "-" : "") + (MKT === "IN" ? "Rs " : "$") + intp + (parts[1] ? "." + parts[1] : "");
}
function fmtNum(x, dp){ return (x === null || x === undefined) ? "n/a" : Number(x).toFixed(dp === undefined ? 2 : dp); }
function verdictClass(v){ return v === "BUY" ? "buy" : v === "WAIT" ? "wait" : v === "NO-BUY" ? "nobuy" : "unjudged"; }
function minutesTo(t){ if (t === null || t === undefined) return ""; var m = Math.round(t); return m <= 0 ? "now" : "in " + m + " min"; }

function rankCandidates(cands){
  var rank = {"BUY":0,"WAIT":1,"NO-BUY":2,"UNJUDGED":3}, conv = {"high":0,"medium":1,"low":2};
  return (cands || []).slice().sort(function(a,b){
    return (rank[a.verdict] === undefined ? 9 : rank[a.verdict]) - (rank[b.verdict] === undefined ? 9 : rank[b.verdict])
      || (conv[a.conviction] === undefined ? 9 : conv[a.conviction]) - (conv[b.conviction] === undefined ? 9 : conv[b.conviction])
      || (b.expectancy_r || -9) - (a.expectancy_r || -9);
  });
}

/* The fifteen steps of a disciplined Indian day trade. Status comes only
   from the files each agent wrote: done, current, blocked or not started. */
function computeSteps(s){
  var f = s.files || {}, J = s.judge || {}, R = s.risk || {}, E = s.entry || {},
      G = s.guard || {}, TM = s.timemachine || {}, B = s.broker_summary || {},
      P = s.plan || {}, O = s.orders || {}, W = s.watchlist || {}, AL = s.alerts || {};
  var bull = s.bull || {}, bear = s.bear || {};
  var st = s.status || {};
  var steps = [];
  function add(id, title, status, explain, why, command, blockReason){
    steps.push({id:id, title:title, status:status, explain:explain, why:why,
                command:command, blockReason:blockReason || null});
  }
  var open = st.trading_day === true && st.status === "open";
  add("market", "Check the market is open (and not a holiday)",
      st.trading_day === false ? "blocked" : (open ? "done" : (st.status === "pre-open" ? "current" : "done")),
      st.trading_day === false ? ("Today is closed: " + (st.closed_why || "holiday"))
        : ("NSE runs 09:15 to 15:30 IST, Monday to Friday"),
      "You cannot day-trade a closed market, and a holiday list for the wrong year is a classic trap.",
      "python3 trader-dashboard/dashboard.py build",
      st.trading_day === false ? ("the market is closed today: " + (st.closed_why || "")) : null);
  var scanned = f.watchlist && f.watchlist.present;
  add("scan", "Scan the watchlist",
      scanned ? "done" : "not_started",
      scanned ? "The scanner wrote its hits" : "Run the scanner to get today's candidates",
      "Trading whatever is trending on social media is how accounts die. The scan gives a small, boring list.",
      "python3 trader-scanner/scan.py orb --market IN");
  var newsed = f.news && f.news.present;
  add("news", "Read the news on every candidate",
      newsed ? "done" : "not_started",
      newsed ? "News state is on file" : "Check headlines and announcements before touching a stock",
      "A results announcement or a SEBI action can gap a stock straight through your stop.",
      "python3 trader-news/news.py --market IN");
  var trig = E.status === "triggered";
  add("entry", "Wait for the entry trigger",
      trig ? "done" : (scanned ? "current" : "not_started"),
      trig ? ("Triggered at " + (E.price != null ? fmtMoney(E.price) : "the level")) :
        "The first completed 5-minute bar closing above the opening range",
      "Buying before the trigger is guessing, not trading the setup.",
      "python3 trader-entry/entry.py check RELIANCE --market IN --write");
  var grec = (G.symbols || {})[(P.symbol || (J.symbol || ""))] || {};
  var gok = grec.verdict === "OK";
  add("guard", "Data guard says the data is OK",
      gok ? "done" : (trig ? "current" : "not_started"),
      gok ? "Fresh data, second source agrees" : "Fresh bars plus a second price source",
      "A stale or wrong price makes every later number a lie. No OK, no entry.",
      "python3 trader-guard/guard.py data RELIANCE.NS --second-price 0 --second-source kite",
      (grec.verdict === "BAD") ? ("guard says BAD: " + ((grec.reasons || []).join("; ") || "no reason")) : null);
  var planned = f.plan && f.plan.present && P.entry != null;
  add("plan", "Exit plan: stop and target before entering",
      planned ? "done" : "not_started",
      planned ? ("Stop " + fmtMoney(P.stop) + ", target " + fmtMoney(P.target)) :
        "Set the stop and target BEFORE buying",
      "The stop is the whole point: it caps the loss. Deciding exits later is how small losses become big ones.",
      "python3 trader-exit/exit.py plan --from trader/entry.json --write --market IN");
  var tmok = f.timemachine && f.timemachine.present && TM.verdict;
  add("timemachine", "Time Machine replay",
      tmok ? "done" : (planned ? "current" : "not_started"),
      tmok ? ("Verdict: " + TM.verdict) : "Replay this setup on 10,000 simulated sessions plus every past firing",
      "If the same setup has lost money after costs dozens of times before, today is not special.",
      "python3 trader-timemachine/timemachine.py run --write --market IN",
      (TM && TM.edge && TM.edge.edge_ok === false) ?
        ("no edge after costs: " + ((TM.edge.edge_reasons || []).join("; ") || "")) : null);
  var bb = f.bull && f.bull.present && f.bear && f.bear.present;
  add("cases", "Bull and Bear write the two cases",
      bb ? "done" : (tmok ? "current" : "not_started"),
      bb ? ("Bull " + (bull.score != null ? bull.score : "?") + " vs Bear " + (bear.score != null ? bear.score : "?")) :
        "Two independent scores, for and against, from the same data",
      "One-sided enthusiasm is how losses start. The bear hunts the reasons not to trade.",
      "python3 trader-bull/bull.py && python3 trader-bear/bear.py");
  var judged = f.judge && f.judge.present && J.verdict;
  var jblocked = judged && J.verdict === "NO-BUY";
  add("judge", "The Judge rules",
      jblocked ? "blocked" : (judged ? "done" : (bb ? "current" : "not_started")),
      judged ? (J.verdict + " (bull " + (J.scores || {}).bull + ", bear " + (J.scores || {}).bear + ")") :
        "BUY only if the case is strong, the bear weak, and no veto fired",
      "No trade today is a valid answer. Forcing one is the most expensive habit in day trading.",
      "python3 trader-judge/judge.py decide",
      jblocked ? ("the Judge says NO-BUY: " + (J.explanation || "")) : null);
  var sized = f.orders && f.orders.present && (O.orders || []).length;
  add("size", "Size the position inside the limits",
      sized ? "done" : (judged && !jblocked ? "current" : "not_started"),
      sized ? ((O.orders[0] || {}).qty + " shares written to orders.json") :
        "Position size from max loss per trade, never from confidence",
      "The size decides how much a wrong call costs you. Wrong here, nothing else matters.",
      "python3 trader-budget/size.py --write");
  var rres = (R.results || []);
  var rblock = rres.length && rres[0].verdict === "BLOCK";
  var rpass = rres.length && rres[0].verdict === "PASS";
  add("risk", "Risk check says PASS",
      rblock ? "blocked" : (rpass ? "done" : (sized ? "current" : "not_started")),
      rblock ? ("BLOCKED: " + ((rres[0].reasons || [])[0] || "")) :
        (rpass ? "Every gate cleared" : "Every order passes the gates or it does not go"),
      "The gates are the safety net: daily loss, weekly loss, drawdown, stale data, judge verdict.",
      "python3 trader-risk/check.py --market IN",
      rblock ? ((rres[0].reasons || []).join("; ") + ". What to do: usually skip the trade.") : null);
  var placed = (B.records || 0) > 0;
  add("order", "Place the orders yourself in the broker app",
      placed ? "done" : (rpass ? "current" : "not_started"),
      "Manual order sheet: BUY as MIS, then an SL-M stop and a LIMIT target",
      "No tool here places orders. You copy three numbers into your own app and say the yes word.",
      "python3 trader-broker/ticket.py show");
  var alertset = f.alerts && f.alerts.present && (AL.rules || []).length;
  add("alerts", "Set the stop, target and flat-by alerts",
      alertset ? "done" : "not_started",
      alertset ? ((AL.rules || []).length + " alerts armed") : "Three alerts: stop, target, flat-by time",
      "You will not watch the screen all day. The alerts watch for you.",
      "python3 trader-alerts/alerts.py add --from-plan");
  var sold = (B.fills || 0) > 0;
  add("squareoff", "Be flat before the flat-by time",
      sold ? "done" : (placed ? "current" : "not_started"),
      "Brokers auto square-off MIS positions near 15:20 IST - be out before then",
      "An intraday position held past square-off time can get closed at whatever price the market gives.",
      "python3 trader-broker/ticket.py flatten RELIANCE.NS");
  add("record", "Record the fill and the result",
      sold ? "done" : "not_started",
      "ticket.py result / fill writes what actually happened",
      "The safety gates and the Judge's review learn only from recorded trades. Honest records beat hopeful memory.",
      "python3 trader-broker/ticket.py fill --symbol RELIANCE.NS --side sell --qty N --price P");
  return steps;
}

function stepWord(st){
  return st === "done" ? "DONE" : st === "current" ? "NOW" : st === "blocked" ? "BLOCKED" : "not started";
}

/* ---------- renderers (DOM only, textContent only) ---------- */

function renderTop(){
  document.getElementById("honest").textContent = DATA.note;
  var st = DATA.status || {};
  var badge = document.getElementById("modebadge");
  var paper = DATA.config && DATA.config.paper !== false;
  badge.textContent = paper ? "PAPER" : "LIVE";
  badge.className = "badge " + (paper ? "paper" : "live");
  var mb = el("span", "badge " + (st.status === "open" ? "open" : "closed"),
              st.status === "open" ? "MARKET OPEN" : "MARKET " + String(st.status || "?").toUpperCase());
  document.getElementById("mstatus").textContent = "";
  document.getElementById("mstatus").appendChild(mb);
  document.getElementById("mstatus").appendChild(
    el("span", "", " " + (st.detail || "")));
  document.getElementById("timers").textContent =
    (st.trading_day === false ? "Next open: " + (st.next_open || "unknown") : "");
  function tick(){
    var now = new Date();
    var off = (DATA.calendar && DATA.calendar.offset_hours != null) ? DATA.calendar.offset_hours : null;
    if (off != null){
      var ms = now.getTime() + now.getTimezoneOffset() * 60000 + off * 3600000;
      var d = new Date(ms);
      document.getElementById("clock").textContent =
        ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) +
        ":" + ("0" + d.getSeconds()).slice(-2) + " " + (DATA.calendar.tz_short || "");
    } else {
      document.getElementById("clock").textContent = now.toLocaleTimeString();
    }
  }
  tick(); setInterval(tick, 1000);
}

function kv(dl, k, v){ dl.appendChild(el("dt","",k)); dl.appendChild(el("dd","",v)); }

function renderHero(){
  var box = document.getElementById("hero");
  box.className = "card hero";
  box.appendChild(el("h2","","Which trade should I choose?"));
  var cands = rankCandidates(DATA.candidates);
  var top = cands[0];
  if (!top || top.verdict !== "BUY"){
    var p = el("p","","No trade today is a valid answer.");
    p.style.fontWeight = "700";
    box.appendChild(p);
    var why = el("p","muted", top ? ("Closest candidate " + top.symbol + ": " + top.verdict +
        (top.explanation ? " - " + top.explanation : ""))
        : "Nothing has been judged yet. Run the pipeline or stay out.");
    box.appendChild(why);
    if (top && top.vetoes && top.vetoes.length){
      var v = el("div","veto","Vetoes: " + top.vetoes.map(function(x){return x.reason;}).join(" | "));
      box.appendChild(v);
    }
    box.appendChild(el("p","small muted",
      "Most intraday traders lose money. Sitting out is a decision too."));
    return;
  }
  var head = el("div","");
  head.appendChild(el("span","badge " + verdictClass(top.verdict), top.verdict));
  head.appendChild(el("span",""," " + top.symbol + "  " +
    (top.conviction ? " conviction: " + top.conviction : "")));
  box.appendChild(head);
  var dl = el("dl","kv");
  var det = top.order || top.plan || {};
  kv(dl, "Entry", fmtMoney(det.entry));
  kv(dl, "Stop", fmtMoney(det.stop));
  kv(dl, "Target", fmtMoney(det.target));
  kv(dl, "Shares", top.shares != null ? String(top.shares) : "not available (run trader-budget)");
  kv(dl, "Rupees invested", top.invested != null ? fmtMoney(top.invested) : "not available");
  var budget = DATA.config && DATA.config.budget;
  kv(dl, "Max loss", (top.max_loss != null ? fmtMoney(top.max_loss) : "not available") +
       (budget && top.max_loss != null ? " (" + (100 * top.max_loss / budget).toFixed(1) + "% of budget)" : ""));
  kv(dl, "Round-trip costs", top.est_cost != null ? fmtMoney(top.est_cost) : "not available");
  kv(dl, "Reward:risk", top.rr != null ? fmtNum(top.rr) : "not available");
  kv(dl, "Scores", "bull " + fmtNum(top.bull, 0) + " / bear " + fmtNum(top.bear, 0) +
       " / expectancy " + (top.expectancy_r != null ? (top.expectancy_r >= 0 ? "+" : "") + fmtNum(top.expectancy_r) + "R" : "n/a"));
  box.appendChild(dl);
  if (top.explanation) box.appendChild(el("p","small", top.explanation));
  box.appendChild(el("p","small muted",
    "Even a BUY is a checklist opinion, not a prediction. The stop is what keeps a wrong call small."));
}

function renderVersus(){
  var box = document.getElementById("versus");
  box.className = "card";
  box.appendChild(el("h2","","Bull vs Bear"));
  var cands = rankCandidates(DATA.candidates);
  var sel = cands[0];
  var bull = DATA.bull || {}, bear = DATA.bear || {};
  function scoreBar(label, score, cls){
    var row = el("div","");
    row.appendChild(el("div","small", label + ": " + (score != null ? score : "n/a") + " / 100"));
    var bar = el("div","bar " + cls);
    var fill = el("span","");
    fill.style.width = Math.max(0, Math.min(100, score || 0)) + "%";
    bar.appendChild(fill);
    row.appendChild(bar);
    return row;
  }
  if (sel){
    box.appendChild(el("p","small muted","Selected: " + sel.symbol));
  }
  box.appendChild(scoreBar("Bull", bull.score, "bull"));
  box.appendChild(scoreBar("Bear", bear.score, "bear"));
  function reasons(title, items, cls){
    var d = el("details","");
    d.appendChild(el("summary","small", title));
    (items || []).forEach(function(i){
      d.appendChild(el("div","small " + cls, "+" + fmtNum(i.points,1) + " " + i.claim +
                       " (" + i.value + " - " + i.source + ")"));
    });
    if (!items || !items.length) d.appendChild(el("div","small muted","none on file"));
    return d;
  }
  var J = DATA.judge || {};
  box.appendChild(reasons("Top reasons FOR", J["for"] || (bull.items || []).slice(0,3), "ok"));
  box.appendChild(reasons("Top reasons AGAINST", J.against || (bear.items || []).slice(0,3), "bad"));
  var vetoes = (bear.vetoes || (sel && sel.vetoes) || []);
  if (vetoes.length){
    var vv = el("div","veto","HARD VETOES:");
    vetoes.forEach(function(v){ vv.appendChild(el("div","small","* " + v.reason)); });
    box.appendChild(vv);
  }
  var ex = document.getElementById("explain");
  ex.className = "card";
  ex.appendChild(el("h2","","The Judge, in plain words"));
  ex.appendChild(el("p","", (J.explanation || "No verdict on file yet - run bull, bear, then judge.py decide.")));
  ex.appendChild(el("p","small muted","The Judge can only add blocks. It can never turn a BLOCK into a PASS, and your yes never overrides it."));
}

function renderTable(){
  var box = document.getElementById("table");
  box.className = "card";
  box.appendChild(el("h2","","Every candidate evaluated"));
  var cands = rankCandidates(DATA.candidates);
  if (!cands.length){
    box.appendChild(el("p","muted","No candidates on file. Run the scanner and the pipeline."));
    return;
  }
  var t = el("table");
  var head = el("tr");
  ["Symbol","Verdict","Bull","Bear","Risk","First block"].forEach(function(h){
    var th = el("th","",h); head.appendChild(th); });
  t.appendChild(head);
  cands.forEach(function(c){
    var tr = el("tr");
    tr.appendChild(el("td","",c.symbol));
    var v = el("td"); v.appendChild(el("span","badge " + verdictClass(c.verdict), c.verdict)); tr.appendChild(v);
    tr.appendChild(el("td","",fmtNum(c.bull,0)));
    tr.appendChild(el("td","",fmtNum(c.bear,0)));
    tr.appendChild(el("td","",c.risk_verdict || "-"));
    tr.appendChild(el("td","small", (c.risk_reasons || [])[0] ||
                     (c.vetoes[0] ? "veto: " + c.vetoes[0].reason : "-")));
    t.appendChild(tr);
  });
  box.appendChild(t);
}

function renderSteps(){
  var box = document.getElementById("steps");
  box.className = "card";
  box.appendChild(el("h2","","How to follow it, step by step"));
  var steps = computeSteps(DATA);
  var openKey = null;
  try { openKey = localStorage.getItem("trader.openStep"); } catch(e) {}
  var curIdx = -1;
  steps.forEach(function(s, i){ if (curIdx < 0 && (s.status === "current" || s.status === "blocked")) curIdx = i; });
  steps.forEach(function(s, i){
    var d = el("details","step " + s.status);
    if (i === curIdx || openKey === s.id) d.open = true;
    var sm = el("summary");
    sm.appendChild(el("span","st", stepWord(s.status) + "  "));
    sm.appendChild(document.createTextNode(" " + (i + 1) + ". " + s.title));
    d.appendChild(sm);
    d.appendChild(el("p","", s.explain));
    d.appendChild(el("p","why","Why it matters: " + s.why));
    if (s.blockReason){
      d.appendChild(el("p","bad","Blocked: " + s.blockReason));
      d.appendChild(el("p","small","What to do: usually skip the trade. A block is the tool protecting you, not a dare."));
    }
    var cmd = el("div","cmd");
    var code = el("code","",s.command);
    var btn = el("button","","copy");
    btn.type = "button";
    btn.setAttribute("data-copy", s.command);
    cmd.appendChild(code); cmd.appendChild(btn);
    d.appendChild(cmd);
    sm.addEventListener("click", function(){
      try { localStorage.setItem("trader.openStep", s.id); } catch(e) {}
    });
    box.appendChild(d);
  });
}

function renderSafety(){
  var box = document.getElementById("safety");
  box.className = "card";
  box.appendChild(el("h2","","Safety gates"));
  var c = DATA.config || {}, R = DATA.risk || {}, rec = DATA.record || {};
  var t = (R.today || {});
  function gate(name, val, lim){
    var g = el("div","gate");
    g.appendChild(el("span","",name));
    var cls = "muted";
    var text = (val != null ? String(val) : "not run");
    if (val != null && lim != null && Math.abs(Number(val)) >= Math.abs(Number(lim)) && Number(lim) !== 0) cls = "bad";
    g.appendChild(el("span",cls,text + (lim != null ? " / limit " + lim : "")));
    box.appendChild(g);
  }
  gate("Daily realized loss", t.realized_today != null ? fmtMoney(t.realized_today) : null,
       c.max_daily_loss_usd != null ? fmtMoney(-Math.abs(c.max_daily_loss_usd)) : null);
  gate("Weekly realized loss", t.realized_week != null ? fmtMoney(t.realized_week) : null,
       c.max_weekly_loss_usd != null ? fmtMoney(-Math.abs(c.max_weekly_loss_usd)) : null);
  gate("Trades today", t.entries_placed != null ? String(t.entries_placed) : null,
       c.max_trades_per_day);
  gate("Losing-day streak limit", null, c.max_consecutive_loss_days);
  gate("Drawdown limit", null, c.max_drawdown_pct != null ? c.max_drawdown_pct + "%" : null);
  gate("Data freshness", null, (c.data_guard_max_age_min || 3) + " min");
  gate("Judge freshness", null, (c.judge_max_age_min || 10) + " min");
  box.appendChild(el("h2","","Live lock (paper earns live)"));
  var needD = c.live_min_paper_days || 30, needN = c.live_min_paper_trades || 50,
      needPF = c.live_min_profit_factor || 1.3;
  function prog(label, have, need){
    var d = el("div","");
    d.appendChild(el("div","small", label + ": " + have + " / " + need));
    var bar = el("div","bar bull");
    var f = el("span","");
    f.style.width = Math.min(100, need ? (100 * have / need) : 0) + "%";
    bar.appendChild(f); d.appendChild(bar); box.appendChild(d);
  }
  prog("Paper days", rec.days || 0, needD);
  prog("Closed paper trades", rec.closed || 0, needN);
  prog("Net profit after costs", Math.max(0, rec.net || 0), 1);
  var pf = rec.profit_factor === "inf" ? 99 : (rec.profit_factor || 0);
  prog("Profit factor", pf, needPF);
  box.appendChild(el("p","small muted",
    "paper trading stays the default until the record earns live: " + needD + " days, " +
    needN + " closed trades, profit after costs, profit factor " + needPF + "."));
}

function renderRecord(){
  var box = document.getElementById("record");
  box.className = "card";
  box.appendChild(el("h2","","Paper record (after costs)"));
  var rec = DATA.record || {};
  if (!rec.closed){
    box.appendChild(el("p","muted","No closed paper trades yet. Record fills with ticket.py result / fill."));
  } else {
    var dl = el("dl","kv");
    kv(dl,"Closed trades", String(rec.closed));
    kv(dl,"Win rate", rec.win_rate != null ? (100 * rec.win_rate).toFixed(0) + "%" : "n/a");
    kv(dl,"Net after costs", fmtMoney(rec.net));
    kv(dl,"Profit factor", rec.profit_factor != null ? String(rec.profit_factor) : "n/a");
    box.appendChild(dl);
    var pts = rec.curve || [];
    if (pts.length > 1){
      var w = 260, h = 60, lo = Math.min.apply(null, pts.concat([0])), hi = Math.max.apply(null, pts.concat([0]));
      var span = (hi - lo) || 1;
      var d = "M0," + (h - ((pts[0] - lo) / span) * h).toFixed(1);
      for (var i = 1; i < pts.length; i++)
        d += " L" + (i * w / (pts.length - 1)).toFixed(1) + "," + (h - ((pts[i] - lo) / span) * h).toFixed(1);
      var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h + '" role="img" aria-label="equity curve">'
        + '<path d="' + d + '" fill="none" stroke="var(--accent)" stroke-width="1.6"/></svg>';
      var div = el("div","");
      div.innerHTML = svg;    /* static SVG only: no data strings inside */
      box.appendChild(div);
    }
  }
  var rv = DATA.judge_review || {};
  box.appendChild(el("h2","","Judge's review"));
  box.appendChild(el("p","small",
    (rv.calls || 0) + " decisions logged; " + (rv.buys_closed || 0) + " BUY calls with an outcome; " +
    (rv.buys_won || 0) + " of those made money after costs."));
  if ((rv.buys_closed || 0) < 10)
    box.appendChild(el("p","small muted","Too few outcomes to mean anything yet."));
}

function renderOrderSheet(){
  var box = document.getElementById("ordersheet");
  var cands = rankCandidates(DATA.candidates);
  var top = cands[0];
  if (!top || top.verdict !== "BUY" || !top.order) { box.className = ""; return; }
  box.className = "card";
  box.appendChild(el("h2","","Manual order sheet (copy into your broker app)"));
  var o = top.order;
  var lines = [
    "1. BUY " + o.qty + " " + top.symbol + "  product: MIS (intraday)  order type: " +
      String(o.type || "market").toUpperCase() + (o.type === "limit" ? " at " + fmtMoney(o.entry) : ""),
    "2. SELL " + o.qty + " " + top.symbol + "  MIS  SL-M (stop-loss market)  trigger " + fmtMoney(o.stop),
    "3. SELL " + o.qty + " " + top.symbol + "  MIS  LIMIT  price " + fmtMoney(o.target),
    "Max loss at the stop: " + fmtMoney(o.max_loss) + " (a stop can fill worse)",
    "Flat by " + (o.flat_by || "15:10") + " IST - brokers auto square-off MIS near 15:20; be out first",
    "When one exit order fills, CANCEL THE OTHER yourself",
  ];
  var pre = el("pre","",lines.join("\n"));
  box.appendChild(pre);
  var btn = el("button","","copy order sheet");
  btn.type = "button";
  btn.setAttribute("data-copy", lines.join("\n"));
  box.appendChild(btn);
  box.appendChild(el("p","small muted","Check your broker supports MIS and SL-M first. This page never places the order; you do."));
}

function renderHelp(){
  var box = document.getElementById("help");
  box.className = "card";
  box.appendChild(el("h2","","Plain words and honest limits"));
  var g = [
    ["R","one unit of risk: the distance from your entry to your stop. A 2R target pays two risks."],
    ["stop-loss","the price where you are wrong and get out. It can fill worse in a fast fall or a circuit limit."],
    ["target","the price where you take the win on purpose."],
    ["MIS","Margin Intraday Square-off: an intraday product the broker closes the same day."],
    ["SL-M","stop-loss market order: becomes a market order when the trigger price trades."],
    ["bracket","entry + stop + target placed together. In India you place the two exits as separate orders."],
    ["slippage","the gap between the price you wanted and the fill you got. It is always against you."],
    ["STT","Securities Transaction Tax: 0.025% on the sell side for intraday equity."],
    ["square-off","closing the position. Brokers auto square-off intraday positions near 15:20 IST."],
    ["circuit limit","a stock can freeze at +-5/10/20%: then nothing trades and your stop cannot fill."],
    ["paper trading","fake fills, real lessons. The live lock wants 30 days and 50 closed trades before real money."],
  ];
  var d = el("div","");
  g.forEach(function(pair){
    var det = el("details","");
    det.appendChild(el("summary","",pair[0]));
    det.appendChild(el("p","small",pair[1]));
    d.appendChild(det);
  });
  box.appendChild(d);
  box.appendChild(el("h2","","What this tool cannot do"));
  var lim = el("ul","small");
  ["predict the future - no score is a promise",
   "guarantee your stop fills where you set it",
   "see orders faster than free, delayed data allows",
   "help during a broker outage or a stock at circuit limit",
   "make day trading safe - SEBI's own study found most intraday traders lose money",
  ].forEach(function(t){ lim.appendChild(el("li","",t)); });
  box.appendChild(lim);
  box.appendChild(el("p","small muted","Education and tooling, not financial advice. Check taxes with a Chartered Accountant."));
}

function bindCopy(){
  document.querySelectorAll("[data-copy]").forEach(function(b){
    b.addEventListener("click", function(){
      var text = b.getAttribute("data-copy");
      function done(){ b.textContent = "copied"; setTimeout(function(){ b.textContent = "copy"; }, 1200); }
      if (navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(done, function(){});
      }
    });
  });
  var tb = document.getElementById("themebtn");
  var cur = null;
  try { cur = localStorage.getItem("trader.theme"); } catch(e) {}
  if (cur) document.documentElement.setAttribute("data-theme", cur);
  tb.addEventListener("click", function(){
    var now = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", now);
    try { localStorage.setItem("trader.theme", now); } catch(e) {}
  });
}

function render(){
  renderTop(); renderHero(); renderVersus(); renderTable(); renderSteps();
  renderSafety(); renderRecord(); renderOrderSheet(); renderHelp();
  document.getElementById("footer").textContent =
    "Built " + DATA.generated_at + " from the agents' own files. Missing panels mean that " +
    "agent has not run yet. Nothing on this page can place an order.";
  bindCopy();
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
else render();
</script>
</body>
</html>
"""


def embed_data(snap):
    """Snapshot -> the JSON string inside the page, escaped so that nothing
    from a headline or a file can break out of the script tag."""
    blob = json.dumps(snap, ensure_ascii=True, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return blob


def build_html(snap):
    return PAGE.replace("__DATA__", embed_data(snap))


def build(root=".", out=None, market=None, cfg=None, now_utc=None):
    cfg = cfg or read_json(os.path.join(root, "trader", "config.json")) or {}
    if _mpl():
        _mpl().apply_inr_aliases(cfg, _MARKET)
    snap = snapshot(root, market or _MARKET, cfg, now_utc)
    html = build_html(snap)
    out = out or os.path.join(root, "trader", "dashboard.html")
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out)
    return out, snap


# ---------------------------------------------------------------- preview (tests + sanity)

def preview_steps(snap):
    """A Python mirror of the page's computeSteps for testing and the status
    command: returns [(id, status, blockReason)]. Keep in step with the JS."""
    f = snap.get("files") or {}
    J = snap.get("judge") or {}
    R = snap.get("risk") or {}
    E = snap.get("entry") or {}
    G = snap.get("guard") or {}
    TM = snap.get("timemachine") or {}
    st = snap.get("status") or {}
    steps = []

    def add(sid, status, block=None):
        steps.append((sid, "blocked" if block else status, block))

    open_ = st.get("trading_day") is True and st.get("status") == "open"
    add("market", "done" if (open_ or st.get("status") not in (None, "unknown")) else "blocked",
        None if st.get("trading_day") else ("the market is closed today: %s" % st.get("closed_why")))
    scanned = (f.get("watchlist") or {}).get("present")
    add("scan", "done" if scanned else "not_started")
    add("news", "done" if (f.get("news") or {}).get("present") else "not_started")
    trig = E.get("status") == "triggered"
    add("entry", "done" if trig else ("current" if scanned else "not_started"))
    sym = (snap.get("plan") or {}).get("symbol") or J.get("symbol") or ""
    grec = ((G.get("symbols") or {}).get(sym)) or {}
    add("guard", "done" if grec.get("verdict") == "OK" else ("current" if trig else "not_started"),
        ("guard says BAD: %s" % "; ".join(grec.get("reasons") or ["no reason"]))
        if grec.get("verdict") == "BAD" else None)
    planned = (f.get("plan") or {}).get("present") and (snap.get("plan") or {}).get("entry") is not None
    add("plan", "done" if planned else "not_started")
    tmok = (f.get("timemachine") or {}).get("present") and TM.get("verdict")
    add("timemachine", "done" if tmok else ("current" if planned else "not_started"),
        ("no edge after costs: %s" % "; ".join((TM.get("edge") or {}).get("edge_reasons") or []))
        if (TM.get("edge") or {}).get("edge_ok") is False else None)
    bb = (f.get("bull") or {}).get("present") and (f.get("bear") or {}).get("present")
    add("cases", "done" if bb else ("current" if tmok else "not_started"))
    judged = (f.get("judge") or {}).get("present") and J.get("verdict")
    add("judge", "done" if judged else ("current" if bb else "not_started"),
        ("the Judge says NO-BUY: %s" % (J.get("explanation") or "")) if J.get("verdict") == "NO-BUY" else None)
    sized = (f.get("orders") or {}).get("present") and ((snap.get("orders") or {}).get("orders") or [])
    add("size", "done" if sized else ("current" if judged and J.get("verdict") != "NO-BUY" else "not_started"))
    rres = (R.get("results") or [])
    rb = rres and rres[0].get("verdict") == "BLOCK"
    rp = rres and rres[0].get("verdict") == "PASS"
    add("risk", "done" if rp else ("current" if sized else "not_started"),
        ("; ".join(rres[0].get("reasons") or []) + ". What to do: usually skip the trade.") if rb else None)
    add("order", "done" if (snap.get("broker_summary") or {}).get("records") else
        ("current" if rp else "not_started"))
    add("alerts", "done" if (f.get("alerts") or {}).get("present") and
        ((snap.get("alerts") or {}).get("rules") or []) else "not_started")
    add("squareoff", "done" if (snap.get("broker_summary") or {}).get("fills") else
        ("current" if (snap.get("broker_summary") or {}).get("records") else "not_started"))
    add("record", "done" if (snap.get("broker_summary") or {}).get("fills") else "not_started")
    return steps


# ---------------------------------------------------------------- commands

def cmd_build(a):
    cfg = read_json(a.config) or {}
    set_market(a.market or os.environ.get("TRADER_MARKET") or cfg.get("market"))
    out, snap = build(a.root, a.out, _MARKET, cfg)
    if not a.watch:
        print("Wrote %s (market %s, %d candidate(s))." % (out, _MARKET, len(snap["candidates"])))
        for sid, status, block in preview_steps(snap):
            if status in ("current", "blocked"):
                print("  next step: %s%s" % (sid, (" - " + block) if block else ""))
                break
        return 0
    print("watch: rebuilding every 30s while the market is open (Ctrl-C to stop)")
    try:
        while True:
            snap_status = (snap.get("status") or {}).get("status")
            out, snap = build(a.root, a.out, _MARKET, cfg)
            print("%s  rebuilt %s (%s)" % (iso(utc_now()), out, snap_status or "?"), flush=True)
            if _mpl() and snap_status == "closed":
                # still rebuild overnight but slowly: a stale page hurts no one
                time.sleep(300)
            else:
                time.sleep(30)
    except KeyboardInterrupt:
        print("stopped.")
        return 0


def cmd_serve(a):
    import http.server
    import functools
    root = os.path.abspath(os.path.join(a.root, "trader"))
    if not os.path.isdir(root):
        raise DashError("no trader/ folder at %s - run build first" % root)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print("Serving %s on http://127.0.0.1:%d/dashboard.html (localhost only, Ctrl-C to stop)"
          % (root, a.port))
    print("The page reloads itself every 30 seconds.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Dashboard: one page, which trade today.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("build", help="write trader/dashboard.html")
    p.add_argument("--root", default=".", help="the folder that holds trader/")
    p.add_argument("--out", help="output path (default trader/dashboard.html)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--market", choices=["US", "IN"], help="market profile (default: config or US)")
    p.add_argument("--watch", action="store_true", help="rebuild every 30s while the market is open")
    p = sub.add_parser("serve", help="serve trader/ on 127.0.0.1 only")
    p.add_argument("--root", default=".")
    p.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"build": cmd_build, "serve": cmd_serve}[a.cmd](a)
    except DashError as e:
        print("dashboard.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
