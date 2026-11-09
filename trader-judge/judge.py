#!/usr/bin/env python3
"""The Judge: reads the Bull and the Bear files and rules on the trade.

    python3 judge.py decide                       # read bull.json + bear.json, write judge.json
    python3 judge.py decide --bull F --bear F --plan F
    python3 judge.py review                       # did past BUY calls make money after costs?
    python3 judge.py plan RELIANCE.NS --side buy  # propose entry/stop/target, write plan.json

The rule (every threshold lives in trader/config.json):
    BUY      bull >= judge_bull_min (65) AND bear <= judge_bear_max (35)
             AND the gap >= judge_gap_min (25) AND no hard veto fired
             AND replay expectancy after costs >= min_net_expectancy_r (0.05)
    WAIT     borderline: bull >= judge_wait_bull_min (55) AND
             bear <= judge_wait_bear_max (45) AND gap >= judge_wait_gap_min (10)
    NO-BUY   everything else, or a stale/mismatched pair of files

The Judge's verdict is a rules-based opinion, never a prediction. It can only
ADD a block in trader-risk ("require_judge"); it never relaxes any other
gate, and your own "yes" never overrides it. Most intraday traders lose
money. Education and tooling, not financial advice.
"""

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import evidence_lib as ev   # noqa: E402  (sibling file, same folder)


class JudgeError(Exception):
    pass


def _age_minutes(iso_text, now_utc):
    when = ev.parse_utc(iso_text)
    if when is None:
        return None
    return (now_utc - when).total_seconds() / 60.0


def _expectancy(bull, bear, state):
    """Replay expectancy after costs, preferring the bull file's number
    (both sides run the same replay engine), else the Time Machine's."""
    for doc in (bull, bear):
        rep = (doc or {}).get("context", {}).get("replay") or {}
        if rep.get("avg_R_net") is not None:
            return rep["avg_R_net"], "%s replay" % doc.get("created_by", "?")
    edge = ev.timemachine_edge(state or {})
    if edge and edge.get("mc_avg_R") is not None:
        return edge["mc_avg_R"], "timemachine"
    return None, "none"


def check_pair(bull, bear, cand_fp, today, now_utc, max_age_min):
    """Freshness + agreement problems, as strings. Empty = the pair is usable."""
    out = []
    for name, doc in (("bull", bull), ("bear", bear)):
        if not doc:
            out.append("no %s.json: run trader-%s first" % (name, name))
            continue
        age = _age_minutes(doc.get("generated_at"), now_utc)
        if age is None or age < -1 or age > max_age_min:
            out.append("%s.json is %s old (limit %s minutes): run trader-%s again"
                       % (name, ("no timestamp" if age is None else "%.0f minutes" % age),
                          "%.0f" % max_age_min, name))
    if bull and bear:
        for name, doc in (("bull", bull), ("bear", bear)):
            if str(doc.get("plan_fp") or "") != str(cand_fp):
                out.append("%s.json is for a different plan (fingerprint %s)"
                           % (name, doc.get("plan_fp") or "none"))
            if str(doc.get("date") or "") != str(today):
                out.append("%s.json is for %s, not %s" % (name, doc.get("date"), today))
        if str(bull.get("symbol") or "").upper() != str(bear.get("symbol") or "").upper():
            out.append("bull is %s, bear is %s" % (bull.get("symbol"), bear.get("symbol")))
    return out


def decide(bull, bear, cand, cfg, now_utc=None, state=None):
    """The verdict. Pure once the files are read."""
    c = dict(ev.DEFAULT_JUDGE)
    c.update({k: v for k, v in (cfg or {}).items() if k in c or k.startswith("judge_")})
    now_utc = now_utc or ev.utc_now()
    today = str(cand.get("date") or ev.market_today(now_utc).isoformat())
    pair = check_pair(bull, bear, cand["plan_fp"], today, now_utc, float(c["judge_max_age_min"]))
    bs = (bull or {}).get("score")
    rs = (bear or {}).get("score")
    vetoes = list((bear or {}).get("vetoes") or [])
    exp, exp_from = _expectancy(bull, bear, state)
    gap = (bs - rs) if (bs is not None and rs is not None) else None
    reasons = []
    if pair:
        verdict = "NO-BUY"
        reasons = pair
    elif vetoes:
        verdict = "NO-BUY"
        reasons = ["veto: " + v["reason"] for v in vetoes]
    elif exp is not None and exp < float(c["min_net_expectancy_r"]):
        verdict = "NO-BUY"
        reasons = ["replay expectancy %+.2fR after costs is under the %s minimum"
                   % (exp, c["min_net_expectancy_r"])]
    elif bs >= c["judge_bull_min"] and rs <= c["judge_bear_max"] and gap >= c["judge_gap_min"]:
        verdict = "BUY"
    elif bs >= c["judge_wait_bull_min"] and rs <= c["judge_wait_bear_max"] \
            and gap >= c["judge_wait_gap_min"]:
        verdict = "WAIT"
        reasons = ["borderline: bull %d, bear %d, gap %d - not strong enough for a BUY rule"
                   % (bs, rs, gap)]
    else:
        verdict = "NO-BUY"
        reasons = ["scores miss the BUY rule: bull %d (need %d), bear %d (need <= %d), "
                   "gap %d (need %d)" % (bs, c["judge_bull_min"], rs, c["judge_bear_max"],
                                         gap, c["judge_gap_min"])]
    conviction = ("high" if verdict == "BUY" and gap >= 2 * c["judge_gap_min"] and bs >= 75
                  else "medium" if verdict == "BUY" else "low")
    return {"verdict": verdict, "conviction": conviction, "scores": {
                "bull": bs, "bear": rs, "gap": gap}, "expectancy_r": exp,
            "expectancy_from": exp_from, "vetoes": vetoes, "rule_reasons": reasons,
            "thresholds": {k: c[k] for k in sorted(c) if k.startswith(("judge_", "min_net"))}}


def _plain_explain(dec, bull, bear):
    """One paragraph in plain words."""
    s = dec["scores"]
    if s["bull"] is None:
        return "No scored case on file yet: run trader-bull and trader-bear first."
    bits = ["Bull %d vs Bear %d (gap %s)." % (s["bull"], s["bear"], s["gap"])]
    if dec["verdict"] == "BUY":
        bits.append("The numbers clear every bar and no veto fired.")
        for i in ev.top((bull or {}).get("items") or [], 2):
            if i["points"] > 0:
                bits.append("Strongest for: %s (%s)." % (i["claim"].rstrip("."), i["value"]))
    elif dec["verdict"] == "WAIT":
        bits.append("Close, but not strong enough for the BUY rule: " +
                    (dec["rule_reasons"][0] if dec["rule_reasons"] else "borderline") + ".")
        bits.append("Wait for a cleaner setup or skip today - skipping is a fine answer.")
    else:
        for vdoc in dec["vetoes"][:2]:
            bits.append("Veto: %s." % vdoc["reason"])
        if not dec["vetoes"] and dec["rule_reasons"]:
            bits.append(dec["rule_reasons"][0] + ".")
        for i in ev.top((bear or {}).get("items") or [], 2):
            if i["points"] > 0:
                bits.append("Strongest against: %s (%s)." % (i["claim"].rstrip("."), i["value"]))
    bits.append("This is a rules checklist, not a prediction: it can still lose.")
    return " ".join(bits)


def decide_doc(bull, bear, cand, cfg, now_utc=None, state=None):
    dec = decide(bull, bear, cand, cfg, now_utc, state)
    dec.update({"created_by": "trader-judge", "market": ev.market(),
                "symbol": cand["symbol"], "side": cand["side"], "date": cand["date"],
                "plan_fp": cand["plan_fp"], "created_at": ev.iso(now_utc or ev.utc_now()),
                "for": [ {k: i[k] for k in ("id", "claim", "value", "points", "source")}
                        for i in ev.top((bull or {}).get("items") or [], 5) if i["points"] > 0],
                "against": [{k: i[k] for k in ("id", "claim", "value", "points", "source")}
                            for i in ev.top((bear or {}).get("items") or [], 5) if i["points"] > 0]})
    dec["headline_reasons"] = ([i["claim"] for i in dec["for"][:2]] +
                               [i["claim"] for i in dec["against"][:2]] +
                               [v["reason"] for v in dec["vetoes"][:2]])
    dec["explanation"] = _plain_explain(dec, bull, bear)
    dec["note"] = ("A rules-based opinion, not a prediction or advice. It can only block "
                   "in trader-risk; it never relaxes any other gate.")
    return dec


def explain_print(doc):
    s = doc["scores"]
    lines = ["JUDGE: %s  %s %s %s (%s)" % (doc["verdict"], doc["symbol"], doc["side"],
                                         doc["date"], doc["market"]),
             "  bull %d / bear %d / gap %s / expectancy %sR (%s) / conviction %s" % (
                 s["bull"], s["bear"], s["gap"],
                 ("%+.2f" % doc["expectancy_r"]) if doc["expectancy_r"] is not None else "n/a",
                 doc["expectancy_from"], doc["conviction"])]
    if doc["for"]:
        lines.append("  for: " + "; ".join("%s (%s)" % (i["id"], i["value"]) for i in doc["for"][:3]))
    if doc["against"]:
        lines.append("  against: " + "; ".join("%s (%s)" % (i["id"], i["value"])
                                               for i in doc["against"][:3]))
    for v in doc["vetoes"]:
        lines.append("  VETO %s: %s" % (v["id"], v["reason"]))
    lines.append("  " + doc["explanation"])
    return "\n".join(lines)


# ---------------------------------------------------------------- logging

def log_decision(path, doc):
    log = ev.read_json(path) or {"created_by": "trader-judge", "decisions": []}
    log.setdefault("decisions", []).append({
        "at": doc["created_at"], "symbol": doc["symbol"], "date": doc["date"],
        "verdict": doc["verdict"], "conviction": doc["conviction"],
        "bull": doc["scores"]["bull"], "bear": doc["scores"]["bear"],
        "gap": doc["scores"]["gap"], "expectancy_r": doc["expectancy_r"],
        "plan_fp": doc["plan_fp"], "market": doc["market"]})
    ev.write_json(path, log)


# ---------------------------------------------------------------- review

def realized_trades(state, cfg):
    """symbol-day realized profit after costs, the same FIFO the risk manager
    uses. -> {(symbol, day): pnl}, plus a count of closed trades."""
    fee = float(cfg.get("fee_per_share_usd") or 0.0)
    slip = float(cfg.get("slippage_bps") or 0.0) / 10000.0
    fills = [r for r in (state or {}).get("orders", []) if r.get("status") == "filled"]
    fills.sort(key=lambda r: (str(r.get("date") or str(r.get("at", ""))[:10]), str(r.get("at", ""))))
    lots, out = {}, {}
    for r in fills:
        sym = str(r.get("symbol", "")).upper()
        try:
            q, px = float(r.get("qty")), float(r.get("price"))
        except (TypeError, ValueError):
            continue
        side = str(r.get("side", "")).lower()
        day = str(r.get("date") or str(r.get("at", ""))[:10])
        if side == "buy":
            lots.setdefault(sym, []).append([q, px])
        elif side == "sell":
            left = q
            while left > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(left, lot[0])
                gross = (px - lot[1]) * take
                if ev.mpl():
                    costs = ev.mpl().round_trip_cost(ev.market(), take, lot[1], px, cfg)
                else:
                    costs = fee * take * 2 + slip * (px + lot[1]) * take
                out[(sym, day)] = out.get((sym, day), 0.0) + gross - costs
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    lots[sym].pop(0)
    return out


def review(log_doc, state, cfg):
    """How often did BUY calls make money after costs? The honest scorecard."""
    rows = []
    pnl_map = realized_trades(state, cfg)
    for d in (log_doc or {}).get("decisions") or []:
        row = dict(d)
        key = (str(d.get("symbol") or "").upper(), str(d.get("date") or ""))
        pnl = pnl_map.get(key)
        if pnl is None and ev.mpl():          # plans may carry the .NS suffix either way
            for (s, day), v in pnl_map.items():
                if day == key[1] and ev.mpl().bare_symbol(s) == ev.mpl().bare_symbol(key[0]):
                    pnl = v
                    break
        row["realized"] = round(pnl, 2) if pnl is not None else None
        rows.append(row)
    buys = [r for r in rows if r.get("verdict") == "BUY"]
    closed = [r for r in buys if r.get("realized") is not None]
    wins = [r for r in closed if r["realized"] > 0]
    return {"calls": len(rows), "buys": len(buys), "closed": len(closed),
            "wins": len(wins), "win_rate": (len(wins) / len(closed)) if closed else None,
            "net": round(sum(r["realized"] for r in closed), 2) if closed else None,
            "rows": rows}


def print_review(rep, market):
    lines = ["JUDGE REVIEW - did the calls make money after costs?",
             "  %d decisions logged: %d BUY, %d with a recorded outcome, %d of those won%s"
             % (rep["calls"], rep["buys"], rep["closed"], rep["wins"],
                " (%.0f%%)" % (100 * rep["win_rate"]) if rep["win_rate"] is not None else "")]
    if rep["net"] is not None:
        lines.append("  net of costs across closed BUY calls: %s" % ev.mny(rep["net"]))
    for r in rep["rows"]:
        res = ("  -> %s" % ev.mny(r["realized"])) if r.get("realized") is not None else "  -> no fill logged"
        lines.append("  %s  %-14s %-6s bull %d bear %d%s" % (
            r.get("date"), r.get("symbol"), r.get("verdict"), r.get("bull") or 0,
            r.get("bear") or 0, res))
    if rep["closed"] < 10:
        lines.append("  Too few closed calls to mean anything yet - a handful of trades "
                     "proves nothing either way.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- plan

def make_plan(symbol, side, entry, bars5m, market, date=None, target_r=1.5):
    """entry - 1x 5-minute ATR stop, entry + target_r risk target, tick-rounded."""
    a5 = ev.atr(bars5m, 14)
    if a5 is None:
        raise JudgeError("not enough 5-minute bars to measure the ATR")
    tk = ev.mpl().tick(market) if ev.mpl() else 0.01
    stop = ev.mpl().tick_floor(entry - a5, market) if ev.mpl() else int((entry - a5) * 100) / 100.0
    risk = entry - stop
    if risk <= 0:
        raise JudgeError("the ATR stop lands on or above the entry: do not force this one")
    target = ev.mpl().tick_round(entry + target_r * risk, market) if ev.mpl() \
        else round(int((entry + target_r * risk) * 100 + 0.5) / 100.0, 2)
    plan = {"created_by": "trader-judge", "symbol": symbol, "side": side,
            "date": date or ev.market_today().isoformat(), "setup": "judge",
            "entry": entry, "entry_time": None, "entry_bar": None, "interval": "5m",
            "opening_range": None, "stop": stop, "stop_rule": "atr", "stop_label": "1x 5m ATR",
            "target": target, "target_r": target_r, "target_label": "%sR" % target_r,
            "risk_per_share": round(risk, 4), "reward_risk": round((target - entry) / risk, 2),
            "flat_by": None, "atr": round(a5, 4), "tick": tk,
            "created_at": ev.iso(ev.utc_now()),
            "note": "Proposed by the Judge from the chart's own volatility. Not advice."}
    plan["plan_fp"] = ev.plan_fingerprint(plan)
    return plan


def cmd_decide(a):
    cfg = ev.load_config(a.config)
    ev.resolve_market(a.market, cfg)
    cand = ev.candidate(a.plan, a.symbol, a.side, a.entry, a.stop, a.target, a.date)
    bull = ev.read_json(a.bull)
    bear = ev.read_json(a.bear)
    now_utc = ev.parse_utc(a.now) or ev.utc_now()
    state = ev.read_state(plan_path=a.plan, config_path=a.config)
    doc = decide_doc(bull, bear, cand, cfg, now_utc, state)
    ev.write_json(a.out, doc)
    per = os.path.join(os.path.dirname(a.out) or ".", "judges")
    safe = doc["symbol"].replace("/", "_").replace("^", "_")
    ev.write_json(os.path.join(per, safe + ".json"), doc)
    log_decision(a.log, doc)
    print(json.dumps(doc, indent=2) if a.json else explain_print(doc))
    print("Wrote %s (logged to %s)." % (a.out, a.log))
    return 0 if doc["verdict"] == "BUY" else 1


def cmd_review(a):
    cfg = ev.load_config(a.config)
    ev.resolve_market(a.market, cfg)
    log = ev.read_json(a.log)
    state = ev.read_json(a.state) or {}
    rep = review(log, state, cfg)
    print(json.dumps(rep, indent=2) if a.json else print_review(rep, ev.market()))
    return 0


def cmd_plan(a):
    cfg = ev.load_config(a.config)
    ev.resolve_market(a.market, cfg)
    sym = a.symbol.upper()
    if ev.mpl():
        if ev.mpl().is_derivative(sym):
            raise JudgeError("%s is a derivative format: cash equity only" % sym)
        ok, why = ev.mpl().valid_symbol(ev.market(), sym)
        if not ok:
            raise JudgeError(why)
    side = (a.side or "buy").lower()
    if side != "buy":
        raise JudgeError("long side only")
    ch = ev.get_chart(sym, "60d", "5m", a.offline)
    if not ch["bars"]:
        raise JudgeError("no 5-minute bars for %s" % sym)
    entry = a.entry if a.entry is not None else round(ch["bars"][-1]["c"], 2)
    plan = make_plan(sym, side, entry, ch["bars"], ev.market(), a.date, a.target_r)
    ev.write_json(a.out, plan)
    print(json.dumps(plan, indent=2) if a.json else
          "PLAN %s: entry %s, stop %s (1x 5m ATR %.2f), target %s (%.1fR). Wrote %s.\n"
          "Next: trader-bull and trader-bear score it, then judge.py decide." % (
              sym, ev.mny(plan["entry"]), ev.mny(plan["stop"]), plan["atr"],
              ev.mny(plan["target"]), a.target_r, a.out))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Judge: rules on the bull and bear cases.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--market", choices=["US", "IN"], help="market profile (default US; IN = NSE)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("decide", help="read the bull and bear files and rule")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--bull", default="trader/bull.json")
    p.add_argument("--bear", default="trader/bear.json")
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--target", type=float)
    p.add_argument("--date")
    p.add_argument("--side", default=None)
    p.add_argument("--now", help="pretend it is this UTC time (freshness check)")
    p.add_argument("--out", default="trader/state/judge.json")
    p.add_argument("--log", default="trader/state/judge_log.json")
    common(p)

    p = sub.add_parser("review", help="did past BUY calls make money after costs?")
    p.add_argument("--log", default="trader/state/judge_log.json")
    p.add_argument("--state", default="trader/state/broker.json")
    common(p)

    p = sub.add_parser("plan", help="propose entry/stop/target from the chart, write plan.json")
    p.add_argument("symbol")
    p.add_argument("--side", default="buy")
    p.add_argument("--entry", type=float, help="default: the last 5-minute close")
    p.add_argument("--target-r", type=float, default=1.5)
    p.add_argument("--date")
    p.add_argument("--offline", help="a saved 60d 5-minute chart")
    p.add_argument("--out", default="trader/plan.json")
    common(p)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"decide": cmd_decide, "review": cmd_review, "plan": cmd_plan}[a.cmd](a)
    except (JudgeError, ev.EvError) as e:
        print("judge.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
