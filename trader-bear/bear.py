#!/usr/bin/env python3
"""The Bear: writes the case AGAINST one candidate trade, independently.

    python3 bear.py RELIANCE.NS --plan trader/plan.json
    python3 bear.py --plan trader/plan.json --flag ASM --flag "thin tape"
    python3 bear.py --event "2026-11-10 quarterly results" --market IN

It reads the same plan, the same charts and the same agent state files as the
Bull (trader-bull), through the shared engine in trader-judge/evidence_lib.py.
It never reads trader/bull.json: the two cases are written blind to each
other, then the Judge (trader-judge) reads both.

On top of the 0-100 evidence score the Bear sets HARD VETOES: any one of them
forces the Judge to NO-BUY no matter how pretty the numbers look (bad or
missing data, results today or tomorrow, costs eating the risk, thin
liquidity, too few past replays, outside the entry window, a derivative
symbol, or a manual flag like ASM/GSM - exchange surveillance lists you must
check yourself).

The score is a weighted checklist, not a prediction. Most intraday traders
lose money. Education and tooling, not financial advice. Nothing here places
an order.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_evidence():
    for base in (_HERE, os.path.join(_HERE, "lib"),
                 os.path.join(_HERE, "..", "trader-judge")):
        p = os.path.normpath(os.path.join(base, "evidence_lib.py"))
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("evidence_lib", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


ev = _load_evidence()
if ev is None:
    sys.stderr.write("bear.py: cannot find trader-judge/evidence_lib.py - keep the "
                     "trader-judge folder next to trader-bear\n")


class BearError(Exception):
    pass


def _need_ev():
    if ev is None:
        raise BearError("needs trader-judge/evidence_lib.py (the shared evidence engine)")


def _map01(x, steps):
    for cut, s in steps:
        if x is not None and x >= cut:
            return s
    return 0.0


def _down(x, steps):
    """smaller-is-worse mapping: x <= cut -> score."""
    for cut, s in steps:
        if x is not None and x <= cut:
            return s
    return 0.0


def in_entry_window(cand, cfg, now_utc=None):
    """(ok, why). The trade date must be a trading day and, if it is today,
    now must sit inside the config entry window."""
    day = datetime.date.fromisoformat(str(cand["date"]))
    if ev.mpl():
        ok, why = ev.mpl().is_trading_day(ev.market(), day)
        if not ok:
            return False, "%s is not a trading day (%s)" % (day.isoformat(), why)
    elif day.weekday() >= 5:
        return False, "%s is a weekend" % day.isoformat()
    if day != (now_utc and ev.to_market(now_utc).date() or ev.market_today()):
        return True, ""                      # a plan for another day is not a window problem
    local = ev.to_market(now_utc or ev.utc_now()).time()
    before = ev.hhmm(cfg.get("no_entries_before") or "09:45", "no_entries_before")
    after = ev.hhmm(cfg.get("no_entries_after") or "15:00", "no_entries_after")
    if local < before or local > after:
        return False, ("it is %02d:%02d %s, outside the entry window %s to %s"
                       % (local.hour, local.minute, ev.tzshort(),
                          before.strftime("%H:%M"), after.strftime("%H:%M")))
    return True, ""


def build_case(cand, data, state, cfg, notes=None, notes_points=None, sources=None,
               cli_events=None, flags=None, headlines=None, now_utc=None):
    """The bear's evidence items and hard vetoes. Pure once loaded."""
    ctx = ev.context(cand, data, state, cfg)
    items, vetoes = [], []

    # ------------------------------------------------------- past
    dn = sum(1 for ok in ((ctx["close"] and ctx["sma20"] and ctx["close"] < ctx["sma20"]),
                          (ctx["close"] and ctx["sma50"] and ctx["close"] < ctx["sma50"]),
                          (ctx["sma50"] and ctx["sma200"] and ctx["sma50"] < ctx["sma200"])) if ok)
    items.append(ev.item("past_downtrend", "price below the 20 and 50 day averages, "
                         "50 below the 200 (%d of 3)" % dn, "%d/3" % dn,
                         15, dn / 3.0, "daily chart 1y", "past"))
    rs = None
    if ctx["ret_3m"] is not None and ctx["idx_ret_3m"] is not None:
        rs = ctx["ret_3m"] - ctx["idx_ret_3m"]
    items.append(ev.item("past_weak_rs", "3-month move %s the index"
                         % ("trails" if (rs or 0) < 0 else "beats"),
                         "%+.1f%% vs index" % (100 * rs) if rs is not None else "no data",
                         10, _down(rs, ((-0.10, 1.0), (0.0, 0.6), (0.05, 0.3))), "daily vs index", "past"))
    rep = ctx["replay"]
    if rep["trades"]:
        s = _down(rep["avg_R_net"], ((0.0, 1.0), (0.05, 0.6), (0.15, 0.3)))
        if rep["trades"] < ev.REPLAY_MIN:
            s = max(s, 0.5)
        items.append(ev.item("past_replay_weak",
                             "base-rate replay of this geometry: %+.2fR net of costs over %d sessions"
                             % (rep["avg_R_net"], rep["trades"]),
                             "avg %+.2fR net, win %.0f%%, n=%d" % (rep["avg_R_net"],
                                                                   100 * (rep["win_rate"] or 0), rep["trades"]),
                             15, s, "60 sessions of 5-minute bars", "past"))
    else:
        items.append(ev.item("past_replay_weak", "no past sessions to replay", "n=0",
                             15, 0.7, "60 sessions of 5-minute bars", "past"))

    # ------------------------------------------------------- present
    vr = ctx["vol_ratio"]
    items.append(ev.item("present_low_volume", "latest daily volume vs its 20-day average",
                         "%.2fx" % vr if vr else "no data",
                         8, _down(vr, ((0.7, 1.0), (1.0, 0.5))), "daily chart", "present"))
    over = 0.0
    if ctx["rsi14"] and ctx["rsi14"] > 75:
        over = 1.0
    elif ctx["rsi14"] and ctx["rsi14"] > 70:
        over = 0.6
    if ctx["sma20"] and ctx["close"] and ctx["close"] > ctx["sma20"] * 1.10:
        over = max(over, 0.6)
    items.append(ev.item("present_overextension", "RSI %.0f and distance above the 20-day average"
                         % (ctx["rsi14"] or 0), "RSI %s" % ("%.1f" % ctx["rsi14"] if ctx["rsi14"] else "n/a"),
                         10, over, "daily chart", "present"))
    a5, rps = ctx["atr5m"], ctx["risk_ps"]
    noise = _down(rps / a5 if (a5 and rps) else None, ((0.5, 1.0), (1.0, 0.5)))
    items.append(ev.item("present_stop_in_noise",
                         "the stop %.2f away vs the 5-minute ATR %.2f" % (rps or 0, a5 or 0),
                         "risk/ATR %s" % ("%.2f" % (rps / a5) if a5 and rps else "n/a"),
                         12, noise, "5-minute bars", "present"))
    weak = ctx["idx_close"] and ctx["idx_sma50"] and ctx["idx_close"] < ctx["idx_sma50"]
    items.append(ev.item("present_regime_weak", "the index is %s its 50-day average"
                         % ("below" if weak else "above" if ctx["idx_sma50"] else "unknown vs"),
                         "%s vs %s" % (ev.mny(ctx["idx_close"]), ev.mny(ctx["idx_sma50"]))
                         if ctx["idx_close"] else "no data",
                         8, 1.0 if weak else 0.0, ev.index_symbol() + " daily", "present"))
    cr = ctx["cost_r"]
    items.append(ev.item("present_cost_drag",
                         "round-trip cost %.2fR of the risk (India charges run about 3x the US)"
                         % cr, "cost %+.2fR" % -cr, 10,
                         _map01(cr, ((0.25, 1.0), (0.15, 0.7), (0.10, 0.4), (-1, 0.1))),
                         "market cost model", "present"))
    floor = ev.liquidity_floor(cfg)
    ratio = (ctx["adv"] / floor) if (ctx["adv"] and floor) else None
    items.append(ev.item("present_thin_liquidity", "average daily traded value vs the floor",
                         "%s vs %s floor" % (ev.mny(ctx["adv"]), ev.mny(floor)) if ctx["adv"] else "no data",
                         8, _down(ratio, ((0.5, 1.0), (1.0, 0.6))), "daily chart", "present"))
    events = (cli_events or []) + (ctx["events"] or [])
    soon = [e for e in events if ev.days_until(e.get("date"), ctx["today"]) is not None
            and 0 <= ev.days_until(e["date"], ctx["today"]) <= 3]
    near = [e for e in events if ev.days_until(e.get("date"), ctx["today"]) is not None
            and 3 < ev.days_until(e["date"], ctx["today"]) <= 7]
    items.append(ev.item("present_event_risk",
                         "%d known event(s) within 3 days, %d within a week" % (len(soon), len(near)),
                         "; ".join("%s %s" % (e["what"], e["date"]) for e in (soon + near)[:3]) or "none",
                         8, 1.0 if soon else (0.5 if near else 0.0),
                         "--event / trader-analyst", "future"))
    bearish = [h for h in (headlines or [])
               if set(h.get("tags") or []) & {"regulatory", "halt", "earnings", "analyst"}]
    items.append(ev.item("present_bad_news", "%d recent headline(s) tagged earnings, "
                         "regulatory, halt or analyst" % len(bearish),
                         "; ".join(h["title"][:60] for h in bearish[:2]) or "none seen",
                         6, min(1.0, len(bearish) / 2.0), "yahoo headlines", "present"))
    ok_window, why_window = in_entry_window(cand, cfg, now_utc)
    items.append(ev.item("present_session_timing", "the entry window",
                         why_window or "inside the window",
                         8, 0.0 if ok_window else 1.0, "config + clock", "present"))

    items += ev.note_items(notes, notes_points, sources)
    return ctx, items, vetoes


def hard_vetoes(cand, data, state, cfg, ctx, flags=None, now_utc=None):
    """Any one of these forces NO-BUY. They are not scored; they are walls."""
    v = []
    verdict, greasons = ctx["guard"]
    if verdict != "OK":
        v.append({"id": "data_guard", "reason": "trader-guard says %s%s - bad or missing "
                  "data means no trade" % (verdict or "no check on file",
                                           (": " + "; ".join(greasons)) if greasons else "")})
    for e in (ctx["events"] or []):
        dd = ev.days_until(e.get("date"), ctx["today"])
        if dd is not None and 0 <= dd <= float(cfg.get("event_veto_days", 1)):
            v.append({"id": "event", "reason": "%s on %s (%s): an event today or tomorrow "
                      "can gap straight through a stop" % (e.get("what", "event"), e["date"],
                                                           "today" if dd == 0 else "tomorrow")})
    floor = ev.liquidity_floor(cfg)
    if ctx["adv"] is not None and ctx["adv"] < floor:
        v.append({"id": "liquidity", "reason": "average daily traded value %s is under the "
                  "%s floor: too thin to trust an intraday stop" % (ev.mny(ctx["adv"]), ev.mny(floor))})
    elif ctx["adv"] is None:
        v.append({"id": "liquidity", "reason": "no daily bars to measure liquidity: too little "
                  "data to trust"})
    if float(cfg.get("cost_r_veto", 0.25)) < ctx["cost_r"]:
        v.append({"id": "cost", "reason": "the round-trip cost is %.0f%% of the risk (over 25%%): "
                  "the charges alone eat the trade" % (100 * ctx["cost_r"])})
    rep = ctx["replay"]
    if rep["trades"] < ev.REPLAY_MIN:
        v.append({"id": "few_replays", "reason": "only %d past sessions replayed this geometry "
                  "(need %d): too little history to take the odds seriously"
                  % (rep["trades"], ev.REPLAY_MIN)})
    ok_window, why_window = in_entry_window(cand, cfg, now_utc)
    if not ok_window:
        v.append({"id": "window", "reason": "outside the entry window: %s" % why_window})
    if ev.mpl():
        if ev.mpl().is_derivative(cand["symbol"]):
            v.append({"id": "derivative", "reason": "%s looks like a futures or options symbol: "
                      "this pack trades cash equity only, long side only" % cand["symbol"]})
        else:
            ok, why = ev.mpl().valid_symbol(ev.market(), cand["symbol"])
            if not ok:
                v.append({"id": "symbol", "reason": why})
    for f in flags or []:
        v.append({"id": "flag", "reason": "manual flag: %s" % f})
    return v


def verdict_doc(cand, ctx, items, vetoes, market):
    return {"created_by": "trader-bear", "market": market, "symbol": cand["symbol"],
            "side": cand["side"], "date": cand["date"], "plan_fp": cand["plan_fp"],
            "score": ev.score(items), "items": items, "vetoes": vetoes,
            "context": {k: v for k, v in ctx.items()
                        if k not in ("today", "events") and not k.startswith("_")},
            "context_today": ctx["today"].isoformat(),
            "events_seen": ctx["events"],
            "generated_at": ev.iso(ev.utc_now()),
            "note": ("A rules-based opinion, not a prediction or advice. Any hard veto "
                     "forces NO-BUY; nothing was placed.")}


def explain(doc, top_n=5):
    lines = ["BEAR %d/100  %s %s %s (%s)" % (doc["score"], doc["symbol"], doc["side"],
                                           doc["date"], doc["market"])]
    for i in ev.top(doc["items"], top_n):
        if i["points"] <= 0:
            continue
        lines.append("  +%.1f  %-26s %s  (%s)" % (i["points"], i["id"], i["value"], i["source"]))
    for vdoc in doc["vetoes"]:
        lines.append("  VETO %-14s %s" % (vdoc["id"], vdoc["reason"]))
    if not doc["vetoes"]:
        lines.append("  no hard veto fired")
    lines.append("A checklist score, not a prediction. Next: trader-judge decides.")
    return "\n".join(lines)


def main(argv=None):
    _need_ev()
    ap = argparse.ArgumentParser(description="The Bear: the case AGAINST the candidate trade.")
    ap.add_argument("symbol", nargs="?", help="override the plan's symbol")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--target", type=float)
    ap.add_argument("--date")
    ap.add_argument("--side", default=None)
    ap.add_argument("--market", choices=["US", "IN"], help="market profile (default US; IN = NSE)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", help="a chart JSON file, or a folder of them")
    ap.add_argument("--index-offline", help="a saved index chart")
    ap.add_argument("--headlines", help="a saved Yahoo RSS file for this ticker")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--now", help="pretend it is this market time (entry-window check)")
    ap.add_argument("--event", action="append", default=[],
                    help='a scheduled risk: "YYYY-MM-DD what happens" (repeatable)')
    ap.add_argument("--flag", action="append", default=[],
                    help="a manual veto, repeatable (ASM, GSM, illiquid, results today...)")
    ap.add_argument("--note", action="append", default=[], help="research note text (repeatable)")
    ap.add_argument("--note-points", action="append", type=float, default=[])
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--out", default="trader/bear.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = ev.load_config(a.config)
        ev.resolve_market(a.market, cfg)
        cand = ev.candidate(a.plan, a.symbol, a.side, a.entry, a.stop, a.target, a.date)
        if a.no_fetch and not a.offline:
            raise BearError("--no-fetch needs --offline so the chart comes from a file")
        data = ev.fetch_all(cand, a.offline, a.index_offline)
        state = ev.read_state(plan_path=a.plan, config_path=a.config)
        now_utc = ev.parse_utc(a.now) if a.now else None
        if a.now and now_utc is None:
            now_utc = ev.market_to_utc(datetime.datetime.fromisoformat(a.now.replace("Z", "")))
        events = ev.cli_events(a.event)
        headlines = ev.fetch_headlines(cand["symbol"], a.headlines) if not a.no_fetch or a.headlines else []
        ctx, items, _ = build_case(cand, data, state, cfg, a.note, a.note_points, a.source,
                                   events, a.flag, headlines, now_utc)
        ctx["events"] = (ctx["events"] or []) + events
        vetoes = hard_vetoes(cand, data, state, cfg, ctx, a.flag, now_utc)
        doc = verdict_doc(cand, ctx, items, vetoes, ev.market())
        ev.write_json(a.out, doc)
        print(json.dumps(doc, indent=2) if a.json else explain(doc))
        print("Wrote %s." % a.out)
        return 0
    except (BearError, ev.EvError) as e:
        print("bear.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
