#!/usr/bin/env python3
"""The Bull: writes the strongest honest case FOR one candidate trade.

    python3 bull.py RELIANCE.NS --plan trader/plan.json
    python3 bull.py --plan trader/plan.json --offline tests/fixtures/trader-bull
    python3 bull.py RELIANCE --entry 1450 --stop 1432 --target 1477 --market IN
    python3 bull.py --note "promoter bought stake" --note-points 4 --source "exchange filing"

It reads the same plan, the same charts and the same agent state files as the
Bear (trader-bear), through the shared engine in trader-judge/evidence_lib.py,
and scores the evidence 0 to 100. It writes trader/bull.json for the Judge
(trader-judge), who reads bull and bear together.

The score is a weighted checklist, not a prediction. A high bull score means
the past and the present lean the trade's way; it says nothing about the next
hour. Most intraday traders lose money. Education and tooling, not financial
advice. Nothing here places an order.
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
    sys.stderr.write("bull.py: cannot find trader-judge/evidence_lib.py - keep the "
                     "trader-judge folder next to trader-bull\n")


class BullError(Exception):
    pass


def _need_ev():
    if ev is None:
        raise BullError("needs trader-judge/evidence_lib.py (the shared evidence engine)")


def _map01(x, steps):
    """x -> the highest threshold it clears: ((cut, score), ...) first match."""
    for cut, s in steps:
        if x is not None and x >= cut:
            return s
    return 0.0


def build_case(cand, data, state, cfg, notes=None, notes_points=None,
               sources=None, cli_events=None):
    """The bull's evidence items. Pure once cand/data/state are loaded."""
    ctx = ev.context(cand, data, state, cfg)
    items = []

    # ------------------------------------------------------- past
    met = sum(1 for ok in ((ctx["close"] and ctx["sma20"] and ctx["close"] > ctx["sma20"]),
                           (ctx["close"] and ctx["sma50"] and ctx["close"] > ctx["sma50"]),
                           (ctx["sma50"] and ctx["sma200"] and ctx["sma50"] > ctx["sma200"])) if ok)
    items.append(ev.item("past_trend", "price above the 20 and 50 day averages, "
                         "50 above the 200 (%d of 3)" % met, "%d/3" % met,
                         15, met / 3.0, "daily chart 1y", "past"))

    rs = None
    if ctx["ret_3m"] is not None and ctx["idx_ret_3m"] is not None:
        rs = ctx["ret_3m"] - ctx["idx_ret_3m"]
    items.append(ev.item("past_relative_strength", "3-month move %s the index"
                         % ("beats" if (rs or 0) >= 0 else "trails"),
                         "%+.1f%% vs index" % (100 * rs) if rs is not None else "no data",
                         12, _map01(rs, ((0.10, 1.0), (0.0, 0.6), (-0.05, 0.3))) if rs is not None else 0,
                         "daily chart vs " + ev.index_symbol(), "past"))

    rep = ctx["replay"]
    if rep["trades"]:
        s = _map01(rep["avg_R_net"], ((0.5, 1.0), (0.2, 0.7), (0.05, 0.4), (0.0001, 0.2)))
        s *= min(1.0, rep["trades"] / float(ev.REPLAY_MIN))
        items.append(ev.item("past_replay",
                             "base-rate replay of this stop/target at this time of day: "
                             "%.2fR net of costs over %d past sessions"
                             % (rep["avg_R_net"], rep["trades"]),
                             "avg %+.2fR net, win %.0f%%, n=%d" % (rep["avg_R_net"],
                                                                   100 * (rep["win_rate"] or 0), rep["trades"]),
                             20, s, "60 sessions of 5-minute bars", "past"))
    else:
        items.append(ev.item("past_replay", "no past sessions to replay this geometry on",
                             "n=0", 20, 0, "60 sessions of 5-minute bars", "past"))

    # ------------------------------------------------------- present
    vr = ctx["vol_ratio"]
    items.append(ev.item("present_volume", "latest daily volume vs its 20-day average",
                         "%.2fx" % vr if vr else "no data",
                         10, _map01(vr, ((1.5, 1.0), (1.0, 0.6), (0.7, 0.3))), "daily chart", "present"))
    rr = ctx["rr"]
    items.append(ev.item("present_reward_risk", "reward:risk of the plan",
                         "%.2f" % rr if rr else "n/a",
                         8, _map01(rr, ((2.0, 1.0), (1.5, 0.8), (1.0, 0.3))), "plan.json", "present"))
    w = ctx["week52"]
    items.append(ev.item("present_52week", "position inside the 52-week range",
                         "%.0f%%" % (100 * w) if w is not None else "no data",
                         8, _map01(w, ((0.8, 1.0), (0.6, 0.7), (0.4, 0.4), (-1, 0.1))), "daily chart", "present"))
    reg = ctx["idx_close"] and ctx["idx_sma50"] and ctx["idx_close"] > ctx["idx_sma50"]
    items.append(ev.item("present_regime", "the index is %s its 50-day average"
                         % ("above" if reg else "below" if ctx["idx_sma50"] else "unknown vs"),
                         "%s vs %s" % (ev.mny(ctx["idx_close"]), ev.mny(ctx["idx_sma50"]))
                         if ctx["idx_close"] else "no data",
                         10, 1.0 if reg else 0.0, ev.index_symbol() + " daily", "present"))
    floor = ev.liquidity_floor(cfg)
    adv = ctx["adv"]
    ratio = (adv / floor) if (adv and floor) else None
    items.append(ev.item("present_liquidity", "average daily traded value vs the floor",
                         "%s vs %s floor" % (ev.mny(adv), ev.mny(floor)) if adv else "no data",
                         8, _map01(ratio, ((2.0, 1.0), (1.0, 0.6), (0.5, 0.3))), "daily chart", "present"))
    items.append(ev.item("present_entry", "trader-entry confirmed the trigger today",
                         "triggered" if ctx["entry_confirmed"] else "not confirmed",
                         9, 1.0 if ctx["entry_confirmed"] else 0.0, "trader/entry.json", "present"))

    # ------------------------------------------------------- future / research
    items += ev.event_items(cli_events or [], ctx["today"], bullish=True)
    items += ev.note_items(notes, notes_points, sources)
    return ctx, items


def verdict_doc(cand, ctx, items, market):
    return {"created_by": "trader-bull", "market": market, "symbol": cand["symbol"],
            "side": cand["side"], "date": cand["date"], "plan_fp": cand["plan_fp"],
            "score": ev.score(items), "items": items,
            "context": {k: v for k, v in ctx.items()
                        if k not in ("today", "events") and not k.startswith("_")},
            "context_today": ctx["today"].isoformat(),
            "events_seen": ctx["events"],
            "generated_at": ev.iso(ev.utc_now()),
            "note": ("A rules-based opinion, not a prediction or advice. The Judge weighs "
                     "this against trader-bear; nothing was placed.")}


def explain(doc, top_n=5):
    lines = ["BULL %d/100  %s %s %s (%s)" % (doc["score"], doc["symbol"], doc["side"],
                                           doc["date"], doc["market"])]
    for i in ev.top(doc["items"], top_n):
        if i["points"] <= 0:
            continue
        lines.append("  +%.1f  %-28s %s  (%s)" % (i["points"], i["id"], i["value"], i["source"]))
    weak = [i for i in doc["items"] if i["points"] <= 0]
    if weak:
        lines.append("  silent: " + ", ".join(i["id"] for i in weak[:5]))
    lines.append("A checklist score, not a prediction. Next: trader-bear, then trader-judge.")
    return "\n".join(lines)


def main(argv=None):
    _need_ev()
    ap = argparse.ArgumentParser(description="The Bull: the case FOR the candidate trade.")
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
    ap.add_argument("--index-offline", help="a saved index chart (daily or 5m folder)")
    ap.add_argument("--headlines", help="a saved Yahoo RSS file for this ticker")
    ap.add_argument("--no-fetch", action="store_true", help="use only --offline files")
    ap.add_argument("--event", action="append", default=[],
                    help='a scheduled positive: "YYYY-MM-DD what happens" (repeatable)')
    ap.add_argument("--note", action="append", default=[], help="research note text (repeatable)")
    ap.add_argument("--note-points", action="append", type=float, default=[],
                    help="points for the matching --note, -10 to 10")
    ap.add_argument("--source", action="append", default=[], help="source for the matching --note")
    ap.add_argument("--out", default="trader/bull.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = ev.load_config(a.config)
        ev.resolve_market(a.market, cfg)
        ev.set_market(ev.market())          # normalize
        cand = ev.candidate(a.plan, a.symbol, a.side, a.entry, a.stop, a.target, a.date)
        if a.no_fetch and not a.offline:
            raise BullError("--no-fetch needs --offline so the chart comes from a file")
        data = ev.fetch_all(cand, a.offline, a.index_offline)
        state = ev.read_state(plan_path=a.plan, config_path=a.config)
        events = ev.cli_events(a.event)
        ctx, items = build_case(cand, data, state, cfg, a.note, a.note_points, a.source, events)
        doc = verdict_doc(cand, ctx, items, ev.market())
        ev.write_json(a.out, doc)
        print(json.dumps(doc, indent=2) if a.json else explain(doc))
        print("Wrote %s." % a.out)
        return 0
    except (BullError, ev.EvError) as e:
        print("bull.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
