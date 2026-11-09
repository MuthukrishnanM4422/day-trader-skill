import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

guard = load("trader-guard", "guard")
check = load("trader-risk", "check")

NOW = "2026-10-19 10:15"                      # Monday, New York
NOW_UTC = guard.parse_now(NOW)                # 14:15 UTC (EDT)
SESSION = (datetime.datetime(2026, 10, 19, 13, 30), datetime.datetime(2026, 10, 19, 20, 0))
CSV = "Symbol,Date,Time,Open,High,Low,Close,Volume\n%s.US,%s,%s,100.0,101.0,99.0,%s,5000\n"


def ts(dt):
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds())


def chart(sym="TSLA", now=NOW_UTC, minutes=30, last_ago=0, price=100.0, vol=1000, currency="USD",
          session=SESSION, bars=None):
    """A Yahoo 1-minute chart JSON text: `minutes` bars ending `last_ago` minutes before now."""
    if bars is None:
        bars = []
        for i in range(minutes):
            start = now - datetime.timedelta(minutes=last_ago + minutes - 1 - i)
            p = price + (i % 3) * 0.05
            bars.append((ts(start), p, p + 0.2, p - 0.2, p + 0.05, vol))
    q = {k: [b[j] for b in bars] for j, k in ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume"))}
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"symbol": sym, "currency": currency, "dataGranularity": "1m",
                 "currentTradingPeriod": {"regular": {"start": ts(session[0]), "end": ts(session[1])}}},
        "timestamp": [b[0] for b in bars], "indicators": {"quote": [q]}}]}})


def stooq(price, date="2026-10-19", sym="TSLA"):
    return CSV % (sym, date, "10:14:00", price)


def cfg(**kw):
    c = {"data_guard_max_age_min": 3, "data_guard_tolerance_pct": 1.0, "require_second_source": True}
    c.update(kw)
    return c


def judge(ytext, stext, now=NOW_UTC, **kw):
    return guard.judge("TSLA", ytext, stext, now, cfg(**kw))


def has(rec, word):
    return any(r.startswith(word) for r in rec["reasons"])


class DataChecks(unittest.TestCase):
    def test_good_data_with_a_matching_second_source_is_ok(self):
        r = judge(chart(), stooq("100.15"))
        self.assertEqual(r["verdict"], "OK", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertLess(r["diff_pct"], 0.2)
        self.assertEqual(r["checked_at"], "2026-10-19T14:15:00Z")

    def test_the_recorded_price_is_the_higher_of_the_two_sources(self):
        r = judge(chart(), stooq("100.60"))
        self.assertEqual(r["verdict"], "OK")
        self.assertEqual(r["price"], 100.6)
        r = judge(chart(), stooq("99.60"), data_guard_tolerance_pct=1.0)
        self.assertEqual(r["price"], r["yahoo_price"])

    def test_a_stale_last_bar_is_bad(self):
        r = judge(chart(last_ago=12), stooq("100.1"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "fresh"), r["reasons"])

    def test_a_bar_just_inside_the_limit_is_fine(self):
        self.assertEqual(judge(chart(last_ago=3), stooq("100.1"))["verdict"], "OK")

    def test_a_closed_market_is_bad(self):
        later = guard.parse_now("2026-10-19 17:00")
        r = judge(chart(now=later), stooq("100.1"), now=later)
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "market"), r["reasons"])

    def test_a_jump_between_bars_is_bad(self):
        base = guard.parse_now("2026-10-19 10:00")
        bars = [(ts(base + datetime.timedelta(minutes=i)), p, p + 0.1, p - 0.1, p, 500)
                for i, p in enumerate([100, 100, 100, 135, 135, 135])]
        bars = [(t, o, h, l, c, v) for t, o, h, l, c, v in bars]
        now = base + datetime.timedelta(minutes=5)
        r = judge(chart(bars=bars), stooq("135"), now=now)
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(any("jump" in x for x in r["reasons"]), r["reasons"])

    def test_zero_volume_at_the_end_is_a_halt(self):
        base = NOW_UTC - datetime.timedelta(minutes=9)
        bars = [(ts(base + datetime.timedelta(minutes=i)), 100 + i * 0.01, 100.3, 99.9, 100.1,
                 0 if i >= 7 else 800) for i in range(10)]
        r = judge(chart(bars=bars), stooq("100.1"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "halt"), r["reasons"])

    def test_a_frozen_feed_is_bad(self):
        base = NOW_UTC - datetime.timedelta(minutes=9)
        bars = [(ts(base + datetime.timedelta(minutes=i)), 100, 100, 100, 100, 0) for i in range(10)]
        r = judge(chart(bars=bars), stooq("100"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(any("frozen" in x for x in r["reasons"]), r["reasons"])

    def test_a_missing_or_negative_price_is_bad(self):
        base = NOW_UTC - datetime.timedelta(minutes=4)
        bars = [(ts(base + datetime.timedelta(minutes=i)), 100, 100.2, 99.8, 100.05, 900) for i in range(5)]
        bars[2] = (bars[2][0], 100, 100.2, -1, 100.05, 900)
        r = judge(chart(bars=bars), stooq("100"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "sane bars"), r["reasons"])
        bars[2] = (bars[2][0], None, None, None, None, None)
        r = judge(chart(bars=bars), stooq("100"))
        self.assertEqual(r["verdict"], "BAD")

    def test_a_close_outside_the_bar_is_bad(self):
        base = NOW_UTC - datetime.timedelta(minutes=2)
        bars = [(ts(base + datetime.timedelta(minutes=i)), 100, 100.2, 99.8, 100.05, 900) for i in range(3)]
        bars[1] = (bars[1][0], 100, 100.2, 99.8, 101.5, 900)
        r = judge(chart(bars=bars), stooq("100"))
        self.assertEqual(r["verdict"], "BAD")

    def test_a_foreign_currency_or_the_wrong_symbol_is_bad(self):
        self.assertEqual(judge(chart(currency="EUR"), stooq("100.1"))["verdict"], "BAD")
        self.assertEqual(judge(chart(sym="AAPL"), stooq("100.1"))["verdict"], "BAD")

    def test_the_second_source_must_agree(self):
        r = judge(chart(), stooq("103.00"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "second source"), r["reasons"])
        self.assertGreater(r["diff_pct"], 1.0)
        # inside a looser tolerance it passes
        self.assertEqual(judge(chart(), stooq("101.00"), data_guard_tolerance_pct=2.0)["verdict"], "OK")

    def test_a_second_source_from_another_day_is_bad(self):
        r = judge(chart(), stooq("100.1", date="2026-10-16"))
        self.assertEqual(r["verdict"], "BAD")
        self.assertTrue(has(r, "second source"), r["reasons"])

    def test_no_second_source_is_bad_unless_you_allow_one_source(self):
        for text in (None, "Symbol,Date,Time,Open,High,Low,Close,Volume\nTSLA.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"):
            r = guard.judge("TSLA", chart(), text, NOW_UTC, cfg(), "Stooq is down")
            self.assertEqual(r["verdict"], "BAD")
            self.assertTrue(has(r, "second source"), r["reasons"])
        r = guard.judge("TSLA", chart(), None, NOW_UTC, cfg(require_second_source=False), "Stooq is down")
        self.assertEqual(r["verdict"], "OK")
        self.assertTrue(r["warnings"])
        # but a real disagreement still blocks, even with one source allowed
        r = guard.judge("TSLA", chart(), stooq("105"), NOW_UTC, cfg(require_second_source=False))
        self.assertEqual(r["verdict"], "BAD")

    def test_garbage_from_yahoo_is_bad_never_ok(self):
        for text in ("not json", json.dumps({"chart": {"error": {"description": "Not Found"}, "result": None}}),
                     json.dumps({"chart": {"result": []}})):
            r = judge(text, stooq("100"))
            self.assertEqual(r["verdict"], "BAD")
            self.assertNotIn("price", r)

    def test_no_bars_is_bad(self):
        self.assertEqual(judge(chart(bars=[]), stooq("100"))["verdict"], "BAD")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "state", "data_guard.json")
        self.y = os.path.join(self.d, "y.json")
        self.s = os.path.join(self.d, "s.csv")
        self.cfgp = os.path.join(self.d, "none.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_guard(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = guard.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def files(self, ytext, stext):
        with open(self.y, "w") as fh:
            fh.write(ytext)
        with open(self.s, "w") as fh:
            fh.write(stext)

    def args(self, *extra):
        return ["data", "TSLA", "--yahoo-file", self.y, "--stooq-file", self.s, "--now", NOW,
                "--out", self.out, "--config", self.cfgp] + list(extra)

    def test_ok_records_a_verdict_trader_risk_accepts(self):
        self.files(chart(), stooq("100.1"))
        code, text, _ = self.run_guard(*self.args())
        self.assertEqual(code, 0)
        self.assertIn("OK    TSLA", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["symbols"]["TSLA"]["verdict"], "OK")
        self.assertEqual(check.data_reasons(doc, "TSLA", {"data_guard": True, "data_guard_max_age_min": 3},
                                            NOW_UTC + datetime.timedelta(minutes=2)), [])

    def test_bad_exits_1_and_trader_risk_says_why(self):
        self.files(chart(last_ago=20), stooq("100.1"))
        code, text, _ = self.run_guard(*self.args())
        self.assertEqual(code, 1)
        self.assertIn("BAD   TSLA", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        why = check.data_reasons(doc, "TSLA", {"data_guard": True, "data_guard_max_age_min": 3}, NOW_UTC)
        self.assertEqual(len(why), 1)
        self.assertIn("NOT OK", why[0])

    def test_no_write_leaves_no_file_and_records_merge(self):
        self.files(chart(), stooq("100.1"))
        self.run_guard(*self.args("--no-write"))
        self.assertFalse(os.path.exists(self.out))
        self.run_guard(*self.args())
        with open(self.out) as fh:
            doc = json.load(fh)
        doc["symbols"]["AAPL"] = {"verdict": "OK", "reasons": [], "checked_at": "2026-10-19T14:00:00Z"}
        with open(self.out, "w") as fh:
            json.dump(doc, fh)
        self.run_guard(*self.args())
        with open(self.out) as fh:
            self.assertEqual(sorted(json.load(fh)["symbols"]), ["AAPL", "TSLA"])

    def test_show_prints_the_record(self):
        self.files(chart(), stooq("100.1"))
        self.run_guard(*self.args())
        code, text, _ = self.run_guard("show", "--out", self.out, "--now", NOW)
        self.assertEqual(code, 0)
        self.assertIn("TSLA", text)

    def test_a_network_failure_is_bad_not_a_crash(self):
        def boom(url, timeout=20):
            raise guard.GuardError("could not reach finance.yahoo.com: offline")
        old = guard._get
        guard._get = boom
        try:
            code, text, _ = self.run_guard("data", "TSLA", "--now", NOW, "--out", self.out, "--config", self.cfgp)
        finally:
            guard._get = old
        self.assertEqual(code, 1)
        self.assertIn("BAD", text)
        with open(self.out) as fh:
            self.assertEqual(json.load(fh)["symbols"]["TSLA"]["verdict"], "BAD")


class RiskReadsTheGuard(unittest.TestCase):
    def rec(self, **kw):
        r = {"verdict": "OK", "reasons": [], "checked_at": "2026-10-19T14:14:00Z"}
        r.update(kw)
        return {"symbols": {"TSLA": r}}

    def test_off_means_off(self):
        self.assertEqual(check.data_reasons(None, "TSLA", {"data_guard": False}, NOW_UTC), [])

    def test_missing_old_or_bad_all_block(self):
        c = {"data_guard": True, "data_guard_max_age_min": 3}
        self.assertEqual(len(check.data_reasons(None, "TSLA", c, NOW_UTC)), 1)
        self.assertEqual(len(check.data_reasons({"symbols": {}}, "TSLA", c, NOW_UTC)), 1)
        self.assertEqual(check.data_reasons(self.rec(), "TSLA", c, NOW_UTC), [])
        self.assertEqual(len(check.data_reasons(self.rec(), "TSLA", c, NOW_UTC + datetime.timedelta(minutes=4))), 1)
        self.assertEqual(len(check.data_reasons(self.rec(verdict="BAD", reasons=["fresh: x"]), "TSLA", c, NOW_UTC)), 1)
        self.assertEqual(len(check.data_reasons(self.rec(checked_at=None), "TSLA", c, NOW_UTC)), 1)
        self.assertEqual(len(check.data_reasons(self.rec(), "AAPL", c, NOW_UTC)), 1)


class Files(unittest.TestCase):
    def test_guard_stays_standard_library_only(self):
        with open(os.path.join(ROOT, "trader-guard", "guard.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("import requests", "import numpy", "import pandas", "subprocess", "eval(", "exec("):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
