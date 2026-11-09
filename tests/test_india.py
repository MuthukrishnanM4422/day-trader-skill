"""India (NSE) profile tests: the shared lib, the courtroom agents, the
manual order sheet and the dashboard. All offline."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

mpl = load("lib", "market_profile")
ticket = load("trader-broker", "ticket")
judge = load("trader-judge", "judge")
ev = load("trader-judge", "evidence_lib")
dash = load("trader-dashboard", "dashboard")

DT = datetime.datetime
UTC_MON = DT(2026, 10, 19)          # a Monday
NSE_HOL = sorted(mpl.NSE_HOLIDAYS)[0]


class Profile(unittest.TestCase):
    def test_market_aliases(self):
        for name in ("IN", "India", "nse"):
            self.assertEqual(mpl.code(name), "IN", name)
        for name in ("US", "nyse", "", None):
            self.assertEqual(mpl.code(name), "US", name)
        self.assertRaises(ValueError, mpl.code, "JP")

    def test_ist_clock_and_sessions(self):
        self.assertEqual(mpl.to_market(DT(2026, 10, 19, 4, 0), "IN"), DT(2026, 10, 19, 9, 30))
        self.assertEqual(mpl.to_market(DT(2026, 10, 19, 4, 0), "US"), DT(2026, 10, 19, 0, 0))
        local = DT(2026, 10, 19, 10, 0)
        self.assertEqual(mpl.market_to_utc(local, "IN"), local - datetime.timedelta(hours=5.5))
        self.assertEqual(mpl.session_times("IN", datetime.date(2026, 10, 19)),
                         (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(mpl.session_times("IN", datetime.date.fromisoformat(NSE_HOL)))
        ok, why = mpl.is_trading_day("IN", datetime.date.fromisoformat(NSE_HOL))
        self.assertFalse(ok)
        self.assertIn("holiday", why)
        self.assertEqual(mpl.tz_short("IN"), "IST")
        self.assertEqual(mpl.bars_per_session("IN", "5m"), 75)
        st, detail = mpl.market_status("IN", DT(2026, 10, 19, 5, 0))   # 10:30 IST
        self.assertEqual(st, "open")

    def test_the_ist_date_trap(self):
        """The NSE session runs overnight in New York. 20:00 UTC is 01:30 IST
        the next day but only 4 PM the same day in New York: a rule or a plan
        dated on the IST date must not be treated as a day older."""
        now = DT(2026, 10, 19, 20, 0)
        self.assertEqual(mpl.today("US", now), datetime.date(2026, 10, 19))
        self.assertEqual(mpl.today("IN", now), datetime.date(2026, 10, 20))
        # market open in IST is still late night in New York
        self.assertEqual(mpl.market_status("IN", DT(2026, 10, 19, 4, 0))[0], "open")
        self.assertEqual(mpl.to_market(DT(2026, 10, 19, 4, 0), "US").date(),
                         datetime.date(2026, 10, 19))

    def test_tick_and_symbols(self):
        self.assertEqual(mpl.tick("IN"), 0.05)
        self.assertEqual(mpl.tick_round(144.52, "IN"), 144.50)
        self.assertEqual(mpl.tick_round(144.53, "IN"), 144.55)
        self.assertEqual(mpl.tick_floor(144.53, "IN"), 144.50)
        self.assertEqual(mpl.tick_ceil(144.52, "IN"), 144.55)
        for sym, ok in (("M&M", True), ("RELIANCE", True), ("RELIANCE.NS", True),
                        ("^NSEI", True), ("RELIANCE.BO", False), ("TCS-FUT", False),
                        ("SBIN25DEC1450CE", False), ("", False)):
            got, _ = mpl.valid_symbol("IN", sym)
            self.assertEqual(got, ok, sym)
        self.assertEqual(mpl.yahoo_symbol("IN", "RELIANCE"), "RELIANCE.NS")
        self.assertEqual(mpl.yahoo_symbol("IN", "RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(mpl.yahoo_symbol("IN", "^NSEI"), "^NSEI")
        self.assertEqual(mpl.bare_symbol("RELIANCE.NS"), "RELIANCE")
        self.assertEqual(mpl.yahoo_symbol("US", "BRK.B"), "BRK-B")

    def test_rupee_formatting(self):
        self.assertEqual(mpl.fmt_money("IN", 1234567.89), "Rs 12,34,567.89")
        self.assertEqual(mpl.fmt_money("IN", 500), "Rs 500.00")
        self.assertEqual(mpl.fmt_money("US", 1234567.89), "$1234567.89")
        self.assertEqual(mpl.money_word("IN"), "rupees")

    def test_india_intraday_costs(self):
        c = mpl.india_intraday_costs(100000, 100000)
        self.assertEqual(c["brokerage"], 40.0)            # 0.03% = 30, capped at Rs 20 a side
        self.assertAlmostEqual(c["stt"], 25.0, 4)         # 0.025% sell side only
        self.assertAlmostEqual(c["txn"], 6.14, 4)
        self.assertAlmostEqual(c["sebi"], 0.2, 4)
        self.assertAlmostEqual(c["stamp"], 3.0, 4)
        self.assertAlmostEqual(c["gst"], 0.18 * (40 + 6.14 + 0.2), 4)
        self.assertAlmostEqual(c["total"], 82.6812, 3)
        # the whole pipeline's round trip = the model + slippage
        rt = mpl.round_trip_cost("IN", 10, 1000, 1100, {"slippage_bps": 5})
        expect = mpl.india_intraday_costs(10000, 11000)["total"] + 5e-4 * 10 * 2100
        self.assertAlmostEqual(rt, expect, 4)
        self.assertGreater(rt, 0)
        self.assertEqual(mpl.cost_model_name("IN"), "india_intraday")
        self.assertEqual(mpl.cost_model_name("US"), "us_default")

    def test_inr_config_aliases(self):
        doc = {"max_loss_per_trade_inr": 500, "budget_inr": 50000, "other": 1}
        mpl.apply_inr_aliases(doc, "IN")
        self.assertEqual(doc["max_loss_per_trade_usd"], 500)
        self.assertEqual(doc["budget_usd"], 50000)
        doc2 = {"max_loss_per_trade_inr": 500}
        mpl.apply_inr_aliases(doc2, "US")                  # untouched outside IN
        self.assertNotIn("max_loss_per_trade_usd", doc2)


class Court(unittest.TestCase):
    """The Judge rules on the bull/bear files; a veto always means NO-BUY."""
    NOW = DT(2026, 10, 19, 14, 15)     # naive UTC
    CAND = {"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "plan_fp": "abc123"}

    def case(self, score, age_min=0, fp="abc123", exp=None, vetoes=None, sym="TSLA"):
        doc = {"created_by": "trader-bull", "score": score, "symbol": sym,
               "date": "2026-10-19", "plan_fp": fp,
               "generated_at": ev.iso(self.NOW - datetime.timedelta(minutes=age_min)),
               "items": [], "vetoes": vetoes or [], "context": {}}
        if exp is not None:
            doc["context"]["replay"] = {"avg_R_net": exp}
        return doc

    def decide(self, bull, bear):
        return judge.decide(bull, bear, self.CAND, {}, now_utc=self.NOW)

    def test_buy_wait_nobuy(self):
        self.assertEqual(self.decide(self.case(70, exp=0.10), self.case(20))["verdict"], "BUY")
        self.assertEqual(self.decide(self.case(60, exp=0.10), self.case(40))["verdict"], "WAIT")
        self.assertEqual(self.decide(self.case(50), self.case(50))["verdict"], "NO-BUY")

    def test_a_veto_always_blocks(self):
        bear = self.case(20, vetoes=[{"id": "event", "reason": "results on 2026-10-19 inside the window"}])
        d = self.decide(self.case(90, exp=0.30), bear)
        self.assertEqual(d["verdict"], "NO-BUY")
        self.assertTrue(any(r.startswith("veto:") for r in d["rule_reasons"]))

    def test_no_edge_blocks(self):
        d = self.decide(self.case(90, exp=-0.10), self.case(10, exp=-0.10))
        self.assertEqual(d["verdict"], "NO-BUY")
        self.assertTrue(any("expectancy" in r for r in d["rule_reasons"]))

    def test_stale_or_mismatched_files_block(self):
        d = self.decide(self.case(90, age_min=30, exp=0.10), self.case(10))
        self.assertEqual(d["verdict"], "NO-BUY")
        self.assertTrue(any("minutes old" in r for r in d["rule_reasons"]))
        d = self.decide(self.case(90, fp="other", exp=0.10), self.case(10))
        self.assertEqual(d["verdict"], "NO-BUY")
        self.assertTrue(any("different plan" in r for r in d["rule_reasons"]))

    def test_evidence_scoring(self):
        items = [ev.item("a", "claim a", "v", 40, 1.0, "x.json", "fact"),
                 ev.item("b", "claim b", "v", 40, 0.5, "y.json", "fact")]
        self.assertEqual(ev.score(items), 60)
        self.assertEqual(ev.score(items + [ev.item("c", "c", "v", 100, 1.0, "z", "fact")]), 100)
        self.assertEqual(ev.score([ev.item("d", "d", "v", 10, -1.0, "z", "fact")]), 0)
        self.assertEqual(ev.item("w", "w", "v", 10, 5.0, "s", "k")["strength"], 1.0)


class ManualSheet(unittest.TestCase):
    """Under market IN the broker ticket is a sheet you place yourself."""
    ORDER = {"side": "buy", "symbol": "RELIANCE", "qty": 10, "type": "market",
             "order_class": "bracket", "stop_loss_stop_price": 1440.0,
             "take_profit_limit_price": 1470.0,
             "plan": {"entry": 1450.0, "stop": 1440.0, "flat_by": "15:10"}}

    def setUp(self):
        ticket.set_market("IN")

    def tearDown(self):
        ticket.set_market("US")

    def test_order_sheet_lines(self):
        lines = "\n".join(ticket.order_sheet(self.ORDER))
        self.assertIn("ORDER SHEET", lines)
        self.assertIn("MIS", lines)
        self.assertIn("SL-M", lines)
        self.assertIn("trigger price: 1440.00", lines)
        self.assertIn("LIMIT  price: 1470.00", lines)
        self.assertIn("CANCEL THE OTHER", lines)
        self.assertIn("15:20", lines)
        self.assertIn("Rs 100.00", lines)          # 10 shares x Rs 10 risk
        self.assertTrue(ticket._manual({}))
        self.assertEqual(ticket._broker({}), "manual")

    def test_still_needs_the_risk_pass(self):
        ready, label, _ = ticket.status_of(self.ORDER, None, {"orders": []}, "2026-10-19")
        self.assertFalse(ready)
        self.assertIn("run check.py", label)
        ready, label, _ = ticket.status_of(
            self.ORDER, {"date": "2026-10-19",
                         "results": [{"fingerprint": ticket.fingerprint(self.ORDER),
                                      "verdict": "PASS"}]}, {"orders": []}, "2026-10-19")
        self.assertTrue(ready)
        self.assertIn("yes", label)


class Page(unittest.TestCase):
    def test_dashboard_builds_self_contained_html(self):
        tmp = tempfile.mkdtemp()
        try:
            dash.set_market("IN")
            out, snap = dash.build(root=tmp, market="IN", now_utc=DT(2026, 10, 19, 5, 0))
            self.assertTrue(os.path.isfile(out))
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn("<!DOCTYPE html", html[:64] + html.lower()[:64].split("<!doctype html")[0])
            self.assertIn("<script", html)
            self.assertNotIn("__DATA__", html)
            self.assertNotIn("https://", json.dumps(snap.get("meta", {})))
            self.assertEqual(snap.get("market"), "IN")
            self.assertIsInstance(dash.preview_steps(snap), list)
        finally:
            dash.set_market("US")

    def test_embed_data_escapes_html(self):
        blob = dash.embed_data({"title": "</script><img>"})
        self.assertNotIn("</script>", blob)
        self.assertIn("\\u003c", blob)


if __name__ == "__main__":
    unittest.main()
