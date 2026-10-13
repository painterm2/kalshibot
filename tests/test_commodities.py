import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kalshibot import bot, commodities
from kalshibot.kalshi import KalshiClient, Market
from kalshibot.ledger import Ledger
from kalshibot.rules import load_rules
from tests.test_weather_bot import PEM, FakeTransport

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)  # Monday 2pm New York, metals open
OPEN = NOW - timedelta(minutes=5)
GOLD = commodities.ASSETS[0]


def fifteen_min(strike=4000.0, yes_bid=70, yes_ask=72, close=NOW + timedelta(minutes=10)):
    return {"ticker": "KXGOLD15M-26OCT121415-15", "event_ticker": "KXGOLD15M-26OCT121415", "status": "active",
            "open_time": OPEN.isoformat(), "close_time": close.isoformat(), "strike_type": "greater_or_equal",
            "floor_strike": strike, "yes_bid": yes_bid, "yes_ask": yes_ask, "exchange_index": 2}


def fake_pyth(now_price=4001.5, open_price=3999.0, publish_time=NOW):
    """Spot gold: open_price at the 15-minute window's start, now_price otherwise."""
    def fetch(url, params=None):
        if params["ids[]"] != GOLD.feed_id:
            raise RuntimeError("feed not in this test")
        when = url.rsplit("/", 1)[1]
        if when == "latest":
            price, ts = now_price, publish_time
        else:
            ts = datetime.fromtimestamp(int(when), tz=timezone.utc)
            price = open_price if ts == OPEN else now_price
        return {"parsed": [{"price": {"price": str(round(price * 1000)), "expo": -3,
                                      "publish_time": int(ts.timestamp())}}]}
    return fetch


class CommoditiesTransport(FakeTransport):
    def __call__(self, method, url, headers, params, body):
        path = url.split("/trade-api/v2", 1)[1]
        if path.startswith("/series/"):
            return 200, {"series": {"category": "Financials", "exchange_index": 2}}
        return super().__call__(method, url, headers, params, body)


def clear_caches():
    commodities._vol_cache.clear()
    commodities._open_spot_cache.clear()


class ModelTest(unittest.TestCase):
    def setUp(self):
        clear_caches()

    def test_at_the_money_is_a_coin_flip(self):
        self.assertAlmostEqual(commodities.prob_above(4000, 4000, 2e-4, 10), 0.5, places=6)

    def test_further_above_the_strike_is_likelier(self):
        near = commodities.prob_above(4001, 4000, 2e-4, 10)
        far = commodities.prob_above(4004, 4000, 2e-4, 10)
        self.assertGreater(far, near)
        self.assertGreater(near, 0.5)

    def test_less_time_means_more_certainty(self):
        self.assertGreater(commodities.prob_above(4002, 4000, 2e-4, 2),
                           commodities.prob_above(4002, 4000, 2e-4, 30))

    def test_volatility_never_below_floor(self):
        flat = lambda url, params=None: {"parsed": [{"price": {"price": "4000000", "expo": -3,
                                                               "publish_time": int(NOW.timestamp())}}]}
        sigma = commodities.sigma_per_minute(GOLD, NOW, flat)
        self.assertAlmostEqual(sigma, GOLD.min_annual_vol / (365 * 24 * 60) ** 0.5)


class RunTest(unittest.TestCase):
    def setUp(self):
        clear_caches()
        self.rules = load_rules(strategy="commodities")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.logs = []

    def run_pass(self, transport, fetch, mode="dry-run", ledger=None):
        ledger = ledger or bot.ledger_for(mode, self.dir, "commodities")
        client = KalshiClient("prod" if mode != "demo" else "demo", "key-id", PEM, transport)
        commodities.run_once(self.rules, client, mode, NOW, self.logs.append, ledger, fetch, self.dir)
        return ledger

    def test_rules(self):
        self.assertEqual(self.rules.daily_loss_limit, 5.00)
        self.assertEqual(load_rules().daily_loss_limit, 5.00)  # weather's own section

    def test_paper_trades_a_mispriced_market(self):
        # Index started at 4000 (spot 3999, so index runs $1 above spot); spot is now 4001.5,
        # so the index is ~4002.5 with 10 minutes left: YES is worth more than the 72c ask.
        transport = CommoditiesTransport({"KXGOLD15M": [fifteen_min()]})
        ledger = self.run_pass(transport, fake_pyth())
        self.assertEqual(transport.orders, [])
        buys = [e for e in ledger.entries() if e["type"] == "buy"]
        self.assertEqual(len(buys), 1, self.logs)
        self.assertEqual((buys[0]["side"], buys[0]["asset"]), ("yes", "Gold"))
        self.assertAlmostEqual(buys[0]["index_price"], 4002.5, places=2)
        self.assertLessEqual(buys[0]["cost"], self.rules.max_risk_per_trade)

    def test_fairly_priced_market_is_left_alone(self):
        transport = CommoditiesTransport({"KXGOLD15M": [fifteen_min(yes_bid=86, yes_ask=88)]})
        ledger = self.run_pass(transport, fake_pyth())
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))

    def test_sits_out_when_pyth_price_is_stale(self):
        transport = CommoditiesTransport({"KXGOLD15M": [fifteen_min()]})
        ledger = self.run_pass(transport, fake_pyth(publish_time=NOW - timedelta(hours=40)))
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))
        self.assertTrue(any("sitting out" in line for line in self.logs))

    def test_sits_out_without_a_15_minute_market_to_measure_the_gap(self):
        hourly = dict(fifteen_min(), ticker="KXGOLDH-26OCT1215-T4000", event_ticker="KXGOLDH-26OCT1215",
                      strike_type="greater")
        ledger = self.run_pass(CommoditiesTransport({"KXGOLDH": [hourly]}), fake_pyth())
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))

    def test_last_minutes_are_left_alone(self):
        late = fifteen_min(close=NOW + timedelta(minutes=2))
        ledger = self.run_pass(CommoditiesTransport({"KXGOLD15M": [late]}), fake_pyth())
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))

    def test_live_blocked_while_commodities_live_trading_off(self):
        from dataclasses import replace
        self.rules = replace(self.rules, live_trading=False)
        transport = CommoditiesTransport({"KXGOLD15M": [fifteen_min()]})
        ledger = bot.ledger_for("live", self.dir, "commodities")
        ledger.fund("2026-10", 100.0, NOW)
        self.run_pass(transport, fake_pyth(), "live", ledger)
        self.assertEqual(transport.orders, [])
        self.assertTrue(any("live trading is disabled" in line for line in self.logs))

    def test_weather_losses_count_toward_the_total_stop(self):
        weather = bot.ledger_for("dry-run", self.dir)
        weather.append({"type": "settle", "ts": NOW.isoformat(), "ticker": "W", "side": "yes",
                        "contracts": 10, "payout": 0.0, "pnl": -9.80})
        ledger = self.run_pass(CommoditiesTransport({"KXGOLD15M": [fifteen_min()]}), fake_pyth())
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))
        self.assertTrue(any("total P&L" in line or "Kelly" in line for line in self.logs), self.logs)

    def test_loop_keeps_going_after_a_failed_paper_pass(self):
        calls = []
        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("network blip")
        original = commodities.run_once
        commodities.run_once = boom
        self.addCleanup(setattr, commodities, "run_once", original)
        t = [NOW]  # time only moves when the loop sleeps
        def sleep(seconds):
            t[0] += timedelta(seconds=seconds)
        commodities.run_loop(self.rules, None, "dry-run", minutes=3, every_seconds=60,
                             log=self.logs.append, sleep=sleep, clock=lambda: t[0])
        self.assertEqual(len(calls), 3)  # at 0, 1 and 2 minutes; a failed pass doesn't end the loop


if __name__ == "__main__":
    unittest.main()


class CacheTest(unittest.TestCase):
    def test_volatility_is_reused_for_ten_minutes(self):
        clear_caches()
        calls = []
        def fetch(url, params=None):
            calls.append(url)
            return {"parsed": [{"price": {"price": "4000000", "expo": -3, "publish_time": int(NOW.timestamp())}}]}
        commodities.sigma_per_minute(GOLD, NOW, fetch)
        first = len(calls)
        commodities.sigma_per_minute(GOLD, NOW + timedelta(minutes=5), fetch)
        self.assertEqual(len(calls), first)
        commodities.sigma_per_minute(GOLD, NOW + timedelta(minutes=11), fetch)
        self.assertGreater(len(calls), first)


class LoopLockTest(unittest.TestCase):
    def test_only_one_loop_per_mode(self):
        from kalshibot import __main__ as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = cli.LOG_DIR
        cli.LOG_DIR = Path(tmp.name)
        self.addCleanup(setattr, cli, "LOG_DIR", original)
        with cli.loop_lock("live") as first:
            self.assertTrue(first)
            with cli.loop_lock("live") as second:
                self.assertFalse(second)
            self.assertTrue(cli.loop_pid_file("live").exists())  # the loser doesn't remove the winner's lock
        self.assertFalse(cli.loop_pid_file("live").exists())
        cli.loop_pid_file("live").write_text("999999")  # a loop that died
        with cli.loop_lock("live") as again:
            self.assertTrue(again)
