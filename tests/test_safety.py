import copy
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
tm = load("trader-timemachine", "timemachine")

SAFE_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
NOW = "2026-10-19 10:15"          # Monday
TODAY = "2026-10-19"
NOW_UTC = check.parse_now(NOW)


def safe(**kw):
    with open(SAFE_PATH) as fh:
        c = json.load(fh)
    c.update(kw)
    return c


def fill(day, side, qty, price, paper=True, n=0):
    return {"at": "%sT10:%02d:00-04:00" % (day, n), "date": day, "status": "filled", "symbol": "TSLA",
            "side": side, "qty": str(qty), "price": price, "paper": paper}


def trip(day, buy, sell, qty=10, paper=True, n=0):
    return [fill(day, "buy", qty, buy, paper, n), fill(day, "sell", qty, sell, paper, n + 1)]


def state(*fills):
    out = []
    for f in fills:
        out.extend(f if isinstance(f, list) else [f])
    return {"orders": out}


NO_COST = {"fee_per_share_usd": 0, "slippage_bps": 0}


class RealizedPnl(unittest.TestCase):
    def test_oldest_shares_are_sold_first_and_costs_come_off(self):
        st = state(fill("2026-10-13", "buy", 10, 100.0, n=0), fill("2026-10-13", "buy", 10, 110.0, n=1),
                   fill("2026-10-13", "sell", 15, 120.0, n=2))
        t = check.closed_trades(st, NO_COST)
        self.assertEqual([(x["symbol"], round(x["pnl"], 2)) for x in t], [("TSLA", 200.0), ("TSLA", 50.0)])
        withc = check.closed_trades(st, {"fee_per_share_usd": 0.01, "slippage_bps": 10})
        gross = 250.0
        self.assertLess(sum(x["pnl"] for x in withc), gross)
        # costs: 2 x 0.01 x 15 shares + 10 bps on both sides of each matched piece
        expect = 0.02 * 15 + 0.001 * ((120 + 100) * 10 + (120 + 110) * 5)
        self.assertAlmostEqual(gross - sum(x["pnl"] for x in withc), expect, places=3)

    def test_a_sell_with_no_matching_buy_is_ignored_and_paper_needs_both_sides(self):
        st = state(fill("2026-10-13", "sell", 5, 100.0))
        self.assertEqual(check.closed_trades(st, NO_COST), [])
        st = state(fill("2026-10-13", "buy", 5, 100.0, paper=True), fill("2026-10-13", "sell", 5, 101.0, paper=None, n=1))
        self.assertFalse(check.closed_trades(st, NO_COST)[0]["paper"])
        st = state(trip("2026-10-13", 100, 101, qty=5))
        self.assertTrue(check.closed_trades(st, NO_COST)[0]["paper"])

    def test_junk_records_do_not_crash(self):
        st = {"orders": [{"status": "filled"}, {"status": "filled", "symbol": "X", "side": "buy", "qty": "abc",
                                                  "price": 1}, {"status": "placed"}]}
        self.assertEqual(check.closed_trades(st, NO_COST), [])
        self.assertEqual(check.closed_trades(None, NO_COST), [])


class Gates(unittest.TestCase):
    def gates(self, st, loss_today=0.0, today=TODAY, **kw):
        c = dict(check.DEFAULTS)
        c.update(safe(**kw))
        c.update(NO_COST)
        return check.safety_gates(check.closed_trades(st, c), c, today, loss_today)

    def test_a_clean_week_has_no_block(self):
        reasons, facts = self.gates(state(trip("2026-10-19", 100, 100.5)))
        self.assertEqual(reasons, [])
        self.assertEqual(facts["realized_today"], 5.0)

    def test_realized_loss_today_over_the_daily_limit_blocks(self):
        reasons, _ = self.gates(state(trip("2026-10-19", 100, 94)))          # -60 vs a $50 limit
        self.assertTrue(any(r.startswith("daily loss") for r in reasons), reasons)

    def test_the_weekly_limit_counts_realized_plus_risk_at_the_stops(self):
        st = state(trip("2026-10-12", 100, 100), trip("2026-10-19", 100, 91))   # Mon this week: -90
        reasons, facts = self.gates(st, loss_today=0.0)
        self.assertEqual(facts["realized_week"], -90.0)
        self.assertEqual(reasons[0].split(":")[0], "daily loss")
        reasons, _ = self.gates(state(trip("2026-10-19", 100, 95)), loss_today=60.0, max_daily_loss_usd=500)
        self.assertTrue(any(r.startswith("weekly loss") for r in reasons), reasons)    # -50 + 60 >= 100

    def test_last_weeks_loss_does_not_count_this_week(self):
        reasons, facts = self.gates(state(trip("2026-10-14", 100, 85)), today="2026-10-19",
                                    max_consecutive_loss_days=0, max_drawdown_pct=0)
        self.assertEqual(facts["realized_week"], 0.0)
        self.assertEqual(reasons, [])

    def test_two_losing_days_in_a_row_is_a_cool_off_for_one_day(self):
        st = state(trip("2026-10-15", 100, 99), trip("2026-10-16", 100, 99))     # Thu, Fri
        reasons, _ = self.gates(st, max_drawdown_pct=0)
        self.assertTrue(any(r.startswith("streak") for r in reasons), reasons)
        # a day with no trades in between ends the streak
        reasons, _ = self.gates(st, today="2026-10-20", max_drawdown_pct=0)
        self.assertEqual(reasons, [])
        # one winning day in the window ends it too
        st = state(trip("2026-10-15", 100, 101), trip("2026-10-16", 100, 99))
        reasons, _ = self.gates(st, max_drawdown_pct=0)
        self.assertEqual(reasons, [])

    def test_drawdown_from_the_peak_blocks_until_you_restart(self):
        # +100 then -300: equity ends at -200, a $300 drop from the +100 peak, against 5 % of $5,000 = $250
        st = state(trip("2026-09-01", 100, 110), trip("2026-09-02", 100, 70))
        reasons, facts = self.gates(st, max_consecutive_loss_days=0)
        self.assertEqual(facts["drawdown_usd"], 300.0)
        self.assertTrue(any(r.startswith("drawdown") for r in reasons), reasons)
        reasons, _ = self.gates(st, max_consecutive_loss_days=0, drawdown_since="2026-09-03")
        self.assertEqual(reasons, [])

    def test_limits_set_to_zero_are_off(self):
        st = state(trip("2026-10-19", 100, 50))
        reasons, _ = self.gates(st, max_daily_loss_usd=0, max_weekly_loss_usd=0, max_drawdown_pct=0,
                                max_consecutive_loss_days=0)
        self.assertEqual(reasons, [])


class LiveLock(unittest.TestCase):
    def lock(self, st, **kw):
        c = dict(check.DEFAULTS)
        c.update(safe(paper=False, **kw))
        return check.live_lock(check.closed_trades(st, c), c)

    def test_paper_accounts_are_never_locked(self):
        self.assertEqual(check.live_lock([], {"paper": True}), [])

    def test_live_with_no_record_is_locked_and_says_what_is_missing(self):
        why = self.lock({"orders": []})
        self.assertEqual(len(why), 1)
        for word in ("0 paper trading days", "0 closed paper trades", "profit factor"):
            self.assertIn(word, why[0])

    def test_live_unlocks_only_with_a_real_profitable_paper_record(self):
        fills = []
        for i in range(12):                       # 12 days, 3 trades a day: 36 wins of +0.60 a share
            day = "2026-09-%02d" % (i + 1)
            for k in range(3):
                fills.extend(trip(day, 100, 100.6, qty=10, n=k * 2))
        st = state(fills)
        self.assertTrue(self.lock(st))
        self.assertEqual(self.lock(st, live_min_paper_days=10, live_min_paper_trades=30), [])

    def test_a_losing_paper_record_stays_locked_even_with_enough_trades(self):
        fills = []
        for i in range(30):
            day = "2026-08-%02d" % (i + 1)
            fills.extend(trip(day, 100, 99.8, qty=10))
        why = self.lock(state(fills), live_min_paper_trades=20)
        self.assertTrue(why)
        self.assertIn("paper profit", why[0])

    def test_unmarked_fills_never_earn_the_lock(self):
        fills = []
        for i in range(40):
            fills.extend(trip("2026-08-%02d" % (i % 28 + 1), 100, 101, paper=None, n=i % 20))
        why = self.lock(state(fills), live_min_paper_days=1, live_min_paper_trades=1)
        self.assertTrue(why)

    def test_live_fills_do_not_count_as_paper(self):
        st = state(trip("2026-09-01", 100, 110, paper=False))
        self.assertTrue(self.lock(st, live_min_paper_days=1, live_min_paper_trades=1))


def tsla_order(qty=3, target=373.0):
    return {"symbol": "TSLA", "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "358.32", "plan": {"entry": 364.18, "stop": 358.32, "target": target}}


GUARD_OK = {"symbols": {"TSLA": {"verdict": "OK", "reasons": [], "checked_at": "2026-10-19T14:14:00Z"}}}
TM_OK = {"plan": {"symbol": "TSLA", "date": TODAY}, "costs": {"edge_ok": True, "edge_reasons": []}}


def run(orders, cfg=None, st=None, guard=GUARD_OK, tmr=TM_OK, now=NOW):
    return check.check(orders, cfg or safe(), None, None, st, None, check.parse_now(now), None, guard, tmr)


class TheGatesInTheCheck(unittest.TestCase):
    def verdict(self, **kw):
        res = run(**kw)
        return res["results"][0]

    def test_a_clean_small_trade_with_good_data_and_an_edge_passes_the_shipped_config(self):
        r = self.verdict(orders=[tsla_order()])
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_no_guard_record_blocks(self):
        r = self.verdict(orders=[tsla_order()], guard=None)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("data") for x in r["reasons"]), r["reasons"])

    def test_a_bad_guard_record_blocks(self):
        g = copy.deepcopy(GUARD_OK)
        g["symbols"]["TSLA"].update(verdict="BAD", reasons=["fresh: old"])
        r = self.verdict(orders=[tsla_order()], guard=g)
        self.assertEqual(r["verdict"], "BLOCK")

    def test_an_old_guard_record_blocks(self):
        r = self.verdict(orders=[tsla_order()], now="2026-10-19 10:20")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("older than" in x for x in r["reasons"]), r["reasons"])

    def test_no_time_machine_run_blocks(self):
        r = self.verdict(orders=[tsla_order()], tmr=None)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("edge") for x in r["reasons"]), r["reasons"])

    def test_a_time_machine_run_for_another_day_or_symbol_blocks(self):
        for plan in ({"symbol": "TSLA", "date": "2026-10-16"}, {"symbol": "AAPL", "date": TODAY}):
            r = self.verdict(orders=[tsla_order()], tmr={"plan": plan, "costs": {"edge_ok": True}})
            self.assertEqual(r["verdict"], "BLOCK", plan)

    def test_no_edge_after_costs_blocks_and_says_why(self):
        tmr = {"plan": {"symbol": "TSLA", "date": TODAY},
               "costs": {"edge_ok": False, "edge_reasons": ["the history averages -0.133 R a trade after costs"]}}
        r = self.verdict(orders=[tsla_order()], tmr=tmr)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("-0.133" in x for x in r["reasons"]), r["reasons"])

    def test_an_old_time_machine_file_without_costs_blocks(self):
        r = self.verdict(orders=[tsla_order()], tmr={"plan": {"symbol": "TSLA", "date": TODAY}})
        self.assertEqual(r["verdict"], "BLOCK")

    def test_the_shipped_limits_are_tighter_than_the_old_ones(self):
        r = self.verdict(orders=[tsla_order(qty=8)])        # the reel's 8 shares: $46.88 at risk vs $25
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("loss") for x in r["reasons"]), r["reasons"])
        r = self.verdict(orders=[tsla_order(target=370.04)])  # 1R target vs the 1.5R minimum
        self.assertTrue(any(x.startswith("reward:risk") for x in r["reasons"]), r["reasons"])

    def test_a_realized_streak_blocks_entries(self):
        st = state(trip("2026-10-15", 100, 99), trip("2026-10-16", 100, 99))
        r = self.verdict(orders=[tsla_order()], st=st)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("streak") for x in r["reasons"]), r["reasons"])

    def test_the_live_lock_blocks_a_live_config(self):
        r = self.verdict(orders=[tsla_order()], cfg=safe(paper=False))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("live lock") for x in r["reasons"]), r["reasons"])

    def test_the_gates_never_block_an_exit(self):
        pf = {"cash": 3000.0, "positions": [{"ticker": "TSLA", "shares": 3, "lots": [{"shares": 3, "cost": 1090}]}]}
        sell = {"symbol": "TSLA", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
        st = state(trip("2026-10-15", 100, 99), trip("2026-10-16", 100, 99))
        res = check.check([sell], safe(paper=False), pf, {"TSLA": 364.0}, st, None, NOW_UTC, None, None, None)
        r = res["results"][0]
        self.assertEqual(r["kind"], "exit")
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_the_switches_turn_each_gate_off(self):
        r = self.verdict(orders=[tsla_order()], cfg=safe(data_guard=False, require_edge=False), guard=None, tmr=None)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_unset_keys_fall_back_to_the_safe_defaults(self):
        # a config that says nothing about the gates is protected by them
        res = check.check([tsla_order()], {"budget": 5000}, None, None, None, None, NOW_UTC)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        reasons = " ".join(r["reasons"])
        self.assertIn("data:", reasons)
        self.assertIn("edge:", reasons)


class StatusAndBrokerLog(unittest.TestCase):
    def test_status_runs_without_an_orders_file(self):
        d = tempfile.mkdtemp()
        try:
            cfgp = os.path.join(d, "c.json")
            stp = os.path.join(d, "broker.json")
            with open(cfgp, "w") as fh:
                json.dump(safe(), fh)
            with open(stp, "w") as fh:
                json.dump(state(trip("2026-10-15", 100, 99), trip("2026-10-16", 100, 99)), fh)
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.main(["--status", "--config", cfgp, "--state", stp, "--now", NOW])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("ENTRIES ARE BLOCKED", text)
            self.assertIn("streak", text)
            self.assertIn("paper record for the live lock", text)
        finally:
            shutil.rmtree(d)

    def test_fills_are_logged_with_the_account_they_came_from(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "broker.json")
            now = ticket.parse_now("2026-10-19 10:20")
            ticket.log_fill(p, now, TODAY, "TSLA", "buy", 3, 364.2, paper=True)
            ticket.log_fill(p, now, TODAY, "TSLA", "sell", 3, 370.0, paper=False)
            with open(p) as fh:
                recs = json.load(fh)["orders"]
            self.assertEqual([r["paper"] for r in recs], [True, False])
            self.assertFalse(check.closed_trades({"orders": recs}, NO_COST)[0]["paper"])
        finally:
            shutil.rmtree(d)


# ---------------------------------------------------------------- Time Machine costs

def hist(rs, entry=100.0, stop=99.0):
    return {"rule": {"or_minutes": 15}, "trades": [
        {"date": "2026-09-%02d" % (i + 1), "entry": entry, "stop": stop, "R": r} for i, r in enumerate(rs)]}


PLAN = {"entry": 100.0, "risk_per_share": 1.0}
MC_OK = {"avg_R": 0.30}


class TimeMachineCosts(unittest.TestCase):
    def test_cost_in_r(self):
        # 2 x 0.005 + 2 x 3 bps of 100 = 0.01 + 0.06 = 0.07 a share, on 1.00 of risk
        self.assertAlmostEqual(tm.cost_r(100.0, 1.0, 0.005, 3), 0.07, places=6)
        self.assertAlmostEqual(tm.cost_r(100.0, 2.0, 0.005, 3), 0.035, places=6)

    def test_a_real_edge_after_costs_is_ok(self):
        c = tm.after_costs(PLAN, MC_OK, hist([1, -1, 1, 1, -1, 1, 1, -1, 1, 1] * 3))   # +0.4 R before costs
        self.assertTrue(c["edge_ok"], c["edge_reasons"])
        self.assertAlmostEqual(c["history_net_avg_R"], 0.4 - 0.07, places=3)
        self.assertEqual(c["history_trades"], 30)
        self.assertIn("Edge after costs", tm.edge_verdict(c))

    def test_costs_turn_a_thin_edge_into_none(self):
        c = tm.after_costs(PLAN, {"avg_R": 0.08}, hist([1, -1] * 15 + [1] * 2))        # ~+0.06 before costs
        self.assertFalse(c["edge_ok"])
        self.assertTrue(any("replays" in r for r in c["edge_reasons"]))
        self.assertIn("NO EDGE", tm.edge_verdict(c))

    def test_too_few_history_trades_is_not_an_edge(self):
        c = tm.after_costs(PLAN, MC_OK, hist([1, 1, 1, 1, 1]))
        self.assertFalse(c["edge_ok"])
        self.assertTrue(any("too few" in r for r in c["edge_reasons"]))

    def test_a_history_that_only_worked_early_is_not_an_edge(self):
        rs = [1] * 24 + [-1] * 9           # strong overall, the last third lost
        c = tm.after_costs(PLAN, MC_OK, hist(rs))
        self.assertGreater(c["history_net_avg_R"], 0.05)
        self.assertFalse(c["edge_ok"])
        self.assertTrue(any("most recent" in r for r in c["edge_reasons"]), c["edge_reasons"])

    def test_a_setup_the_history_cannot_replay_is_not_confirmed(self):
        c = tm.after_costs(PLAN, MC_OK, {"rule": None, "trades": []})
        self.assertFalse(c["edge_ok"])

    def test_zero_costs_do_not_change_the_raw_numbers(self):
        c = tm.after_costs(PLAN, MC_OK, hist([1, -1] * 20), fee=0, slip_bps=0, min_net_r=0.0)
        self.assertEqual(c["cost_R"], 0.0)
        self.assertEqual(c["mc_net_avg_R"], 0.30)

    def test_the_reel_trade_has_no_edge_after_costs(self):
        # the README's TSLA trade: 13 targets, 15 stops, 5 neither in 33 real sessions
        with gzip.open(os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "rt", encoding="utf-8") as fh:
            text = fh.read()
        plan = tm.normalize_plan({
            "created_by": "trader-exit", "symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb",
            "entry": 364.18, "entry_time": "10:15 AM", "entry_bar": 8, "interval": "5m",
            "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
            "stop": 358.32, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
            "target": 370.04, "target_r": 1.0, "target_label": "1R (one times the risk)",
            "risk_per_share": 5.86, "reward_risk": 1.0, "flat_by": "15:55"})
        doc = tm.time_machine(text, plan, tm.parse_now("2026-10-19 10:50"))
        self.assertFalse(doc["costs"]["edge_ok"])
        self.assertEqual(doc["history"]["triggered"], 33)
        self.assertLess(doc["costs"]["history_net_avg_R"], doc["history"]["avg_R"])
        self.assertTrue(doc["edge_verdict"].startswith("NO EDGE"))
        self.assertIn("NO EDGE", "\n".join(tm.report(doc)))
        # and trader-risk would block an entry on it
        self.assertTrue(check.edge_reasons(doc, "TSLA", "2026-10-19", {"require_edge": True}))

    def test_costs_come_from_the_config_file_and_flags(self):
        d = tempfile.mkdtemp()
        try:
            cfgp = os.path.join(d, "c.json")
            with open(cfgp, "w") as fh:
                json.dump({"fee_per_share_usd": 0.02, "slippage_bps": 10, "min_net_expectancy_r": 0.1}, fh)
            base = ["run", "--symbol", "TSLA", "--date", "2026-10-19", "--entry", "364.18", "--stop", "358.32",
                    "--target", "370.04", "--entry-time", "10:15 AM", "--now", "2026-10-19 10:50", "--replays", "200",
                    "--offline", os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "--json"]
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(tm.main(base + ["--config", cfgp]), 0)
            k = json.loads(out.getvalue())["costs"]
            self.assertEqual((k["fee_per_share_usd"], k["slippage_bps"], k["min_net_expectancy_r"]), (0.02, 10.0, 0.1))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(tm.main(base + ["--config", cfgp, "--fee-per-share", "0", "--slippage-bps", "0"]), 0)
            k = json.loads(out.getvalue())["costs"]
            self.assertEqual((k["fee_per_share_usd"], k["slippage_bps"]), (0.0, 0.0))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
