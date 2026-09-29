import base64
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshibot import bot, weather
from kalshibot.kalshi import KalshiClient, Market, sign
from kalshibot.ledger import Ledger
from kalshibot.rules import load_rules

# 11am New York on the 29th: today's high is still to come.
NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()


def market(ticker, strike_type, floor=None, cap=None, yes_bid=40, yes_ask=42, event="KXHIGHNY-26SEP30", **extra):
    raw = {
        "ticker": ticker, "event_ticker": event, "status": "active",
        "close_time": "2026-10-01T04:59:00Z", "strike_type": strike_type,
        "floor_strike": floor, "cap_strike": cap, "yes_bid": yes_bid, "yes_ask": yes_ask,
    }
    raw.update(extra)
    return raw


class FakeTransport:
    """Stands in for Kalshi's HTTP API."""

    def __init__(self, markets_by_series, positions=(), resting=(), settled=None):
        self.markets_by_series = markets_by_series
        self.positions = list(positions)
        self.resting = list(resting)
        self.settled = settled or {}
        self.all_markets = {m["ticker"]: m for rows in markets_by_series.values() for m in rows}
        self.orders = []
        self.headers = []

    def __call__(self, method, url, headers, params, body):
        self.headers.append(headers)
        path = url.split("/trade-api/v2", 1)[1]
        if method == "POST" and path == "/portfolio/orders":
            self.orders.append(body)
            return 201, {"order": {"order_id": f"o{len(self.orders)}", "status": "executed"}}
        if path == "/portfolio/fills":
            body = self.orders[int(params["order_id"][1:]) - 1]
            return 200, {"fills": [{"count": body["count"], f"{body['side']}_price": body[f"{body['side']}_price"]}]}
        if path == "/portfolio/balance":
            return 200, {"balance": 12345}
        if path == "/portfolio/positions":
            return 200, {"market_positions": self.positions}
        if path == "/portfolio/orders":
            return 200, {"orders": self.resting}
        if path.startswith("/series/"):
            return 200, {"series": {"category": "Climate and Weather"}}
        if path == "/markets":
            return 200, {"markets": self.markets_by_series.get(params["series_ticker"], [])}
        if path.startswith("/markets/"):
            ticker = path.rsplit("/", 1)[1]
            if ticker in self.all_markets:
                return 200, {"market": {**self.all_markets[ticker], "result": self.settled.get(ticker, "")}}
        return 404, {"error": "not found"}


def fake_nws(high_by_day):
    """NWS forecast for every station, and no observations yet."""
    def fetch(url, params=None):
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/forecast"}}
        if url.endswith("/forecast"):
            return {"properties": {"periods": [
                {"isDaytime": True, "temperature": t, "temperatureUnit": "F", "startTime": f"{d}T06:00:00-04:00"}
                for d, t in high_by_day.items()]}}
        if "/observations" in url:
            return {"features": []}
        raise AssertionError(url)
    return fetch


class SigningTest(unittest.TestCase):
    def test_signature_is_pss_over_timestamp_method_path(self):
        client = KalshiClient("demo", "key-id", PEM)
        sig = sign(client._key, "1700000000000", "get", "/trade-api/v2/portfolio/balance?limit=1")
        client._key.public_key().verify(
            base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/balance",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_requests_carry_auth_headers(self):
        transport = FakeTransport({})
        KalshiClient("demo", "key-id", PEM, transport).balance()
        headers = transport.headers[0]
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "key-id")
        self.assertIn("KALSHI-ACCESS-SIGNATURE", headers)

    def test_repr_hides_key(self):
        self.assertNotIn("key-id", repr(KalshiClient("demo", "key-id", PEM)))


class MarketTest(unittest.TestCase):
    def test_cents_and_dollar_prices(self):
        a = Market.from_api(market("A", "greater", 80, yes_bid=40, yes_ask=42))
        b = Market.from_api(market("B", "greater", 80, yes_bid=None, yes_ask=None,
                                   yes_bid_dollars="0.4000", yes_ask_dollars="0.4200"))
        for m in (a, b):
            self.assertAlmostEqual(m.yes_ask, 0.42)
            self.assertAlmostEqual(m.no_ask, 0.60)

    def test_event_date(self):
        self.assertEqual(weather.event_date("KXHIGHNY-26SEP30"), date(2026, 9, 30))
        self.assertIsNone(weather.event_date("KXHIGHNY-XYZ"))


class ModelTest(unittest.TestCase):
    def p(self, strike_type, floor=None, cap=None, mean=75.0, sigma=3.0, observed=None):
        return weather.yes_probability(Market.from_api(market("M", strike_type, floor, cap)), mean, sigma, observed)

    def test_buckets_cover_everything_once(self):
        total = self.p("less", cap=70) + self.p("greater", floor=79)
        total += sum(self.p("between", lo, lo + 1) for lo in range(70, 79, 2))
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_centered_forecast(self):
        self.assertAlmostEqual(self.p("greater", floor=74, mean=74.5), 0.5, places=9)
        self.assertGreater(self.p("between", 74, 75), self.p("between", 80, 81))

    def test_observed_high_is_a_floor(self):
        self.assertEqual(self.p("greater", floor=70, observed=72), 1.0)
        self.assertEqual(self.p("less", cap=72, observed=72), 0.0)
        self.assertGreater(self.p("greater", floor=76, observed=75), self.p("greater", floor=76))

    def test_blend_pulls_toward_market(self):
        m = Market.from_api(market("M", "greater", 80, yes_bid=40, yes_ask=42))
        self.assertAlmostEqual(weather.blended_probability(0.60, m, weight=0.5), 0.505)

    def test_big_disagreement_with_market_is_skipped(self):
        m = Market.from_api(market("M", "greater", 80, yes_bid=40, yes_ask=42))
        self.assertIsNone(weather.blended_probability(0.80, m))

    def test_climate_day_uses_standard_time(self):
        start = weather.standard_day_start(weather.STATIONS[0], date(2026, 7, 1))
        self.assertEqual(start.utcoffset(), timedelta(hours=-5))


class BotTest(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.logs = []
        # NWS says 85F tomorrow; the market prices "81 or above" at only 72c.
        self.nyc = [
            market("KXHIGHNY-26SEP30-T80", "greater", floor=80, yes_bid=70, yes_ask=72),
            market("KXHIGHNY-26SEP30-B84.5", "between", 84, 85, yes_bid=20, yes_ask=22),
        ]
        self.fetch = fake_nws({"2026-09-29": 80, "2026-09-30": 85})

    def run_bot(self, transport, mode="dry-run", ledger=None):
        ledger = ledger or Ledger(self.dir / f"ledger-{mode}.jsonl")
        client = KalshiClient("prod" if mode != "demo" else "demo", "key-id", PEM, transport)
        bot.run_once(self.rules, client, mode, NOW, self.logs.append, ledger, self.fetch, self.dir)
        return ledger

    def test_dry_run_paper_trades_and_never_orders(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc})
        ledger = self.run_bot(transport)
        self.assertEqual(transport.orders, [])
        buys = [e for e in ledger.entries() if e["type"] == "buy"]
        self.assertEqual(len(buys), 1)  # one position per event
        self.assertEqual((buys[0]["ticker"], buys[0]["side"]), ("KXHIGHNY-26SEP30-T80", "yes"))
        self.assertLessEqual(buys[0]["cost"], self.rules.max_risk_per_trade)
        self.assertTrue(buys[0]["simulated"])

    def test_dry_run_respects_limits_across_runs(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc})
        ledger = self.run_bot(transport)
        self.run_bot(transport, ledger=ledger)
        self.assertEqual(sum(e["type"] == "buy" for e in ledger.entries()), 1)
        self.assertTrue(any("already 1 open position" in line for line in self.logs))

    def test_live_mode_blocked_while_live_trading_off(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc})
        ledger = Ledger(self.dir / "ledger-live.jsonl")
        ledger.fund("2026-09", 100.0, NOW)
        self.run_bot(transport, "live", ledger)
        self.assertEqual(transport.orders, [])
        self.assertTrue(any("live trading is disabled" in line for line in self.logs))

    def test_demo_places_ioc_order_and_records_fill(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc})
        ledger = Ledger(self.dir / "ledger-demo.jsonl")
        ledger.fund("2026-09", 100.0, NOW)
        self.run_bot(transport, "demo", ledger)
        self.assertEqual(len(transport.orders), 1)
        order = transport.orders[0]
        self.assertEqual((order["action"], order["side"], order["yes_price"]), ("buy", "yes", 72))
        self.assertEqual(order["time_in_force"], "immediate_or_cancel")
        self.assertEqual(ledger.open_positions()[0].contracts, order["count"])

    def test_skips_owner_markets(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc[:1]},
                                  positions=[{"ticker": "KXHIGHNY-26SEP30-T80", "position": -3}])
        ledger = self.run_bot(transport)
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))
        self.assertTrue(any("owner holds a position" in line for line in self.logs))

    def test_same_day_markets_are_left_alone(self):
        today = [market("KXHIGHNY-26SEP29-T79", "greater", floor=79, yes_bid=45, yes_ask=47, event="KXHIGHNY-26SEP29")]
        ledger = self.run_bot(FakeTransport({"KXHIGHNY": today}))
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))

    def test_kill_switch(self):
        (self.dir / self.rules.kill_switch_file).touch()
        ledger = self.run_bot(FakeTransport({"KXHIGHNY": self.nyc}))
        self.assertFalse(any(e["type"] == "buy" for e in ledger.entries()))

    def test_settlement_books_pnl(self):
        transport = FakeTransport({"KXHIGHNY": self.nyc})
        ledger = self.run_bot(transport)
        cost = ledger.open_positions()[0].max_loss
        contracts = ledger.open_positions()[0].contracts
        transport.settled["KXHIGHNY-26SEP30-T80"] = "yes"
        transport.markets_by_series = {}
        self.run_bot(transport, ledger=ledger)
        settle = [e for e in ledger.entries() if e["type"] == "settle"][0]
        self.assertAlmostEqual(settle["pnl"], round(contracts - cost, 2))
        self.assertEqual(ledger.open_positions(), [])


if __name__ == "__main__":
    unittest.main()
