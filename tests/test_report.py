import unittest
from datetime import datetime, timezone

from kalshibot.report import build_report
from kalshibot.rules import load_rules

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def buy(ticker, side, cost, wp, ts="2026-10-01T15:00:00+00:00", contracts=2):
    return {"type": "buy", "ts": ts, "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
            "side": side, "contracts": contracts, "price": cost / contracts, "cost": cost, "win_probability": wp}


def settle(ticker, side, payout, pnl, ts="2026-10-02T15:00:00+00:00", contracts=2):
    return {"type": "settle", "ts": ts, "ticker": ticker, "side": side, "contracts": contracts,
            "payout": payout, "pnl": pnl}


class ReportTest(unittest.TestCase):
    def test_week_summary(self):
        entries = [
            {"type": "fund", "ts": "2026-09-29T20:00:00+00:00", "month": "2026-09", "amount": 100.0},
            buy("KXHIGHNY-26OCT02-B74.5", "no", 1.10, 0.70),
            settle("KXHIGHNY-26OCT02-B74.5", "no", 2.0, 0.90),
            buy("KXHIGHCHI-26OCT02-T69", "yes", 0.80, 0.50),
            settle("KXHIGHCHI-26OCT02-T69", "yes", 0.0, -0.80),
            buy("KXHIGHLAX-26OCT06-B84.5", "yes", 0.30, 0.20, ts="2026-10-05T11:00:00+00:00"),
        ]
        text = build_report(entries, load_rules(), NOW, cash_balance=97.5)
        self.assertIn("**+$0.10**", text)
        self.assertIn("2 (1 won, 1 lost)", text)
        self.assertIn("expected to win **60%**", text)
        self.assertIn("won **50%**", text)
        self.assertIn("| Chicago (Midway) | 1 | -$0.80 |", text)
        self.assertIn("1, $0.30 at risk", text)
        self.assertIn("$97.50", text)

    def test_old_settlements_left_out_of_the_week(self):
        entries = [buy("KXHIGHNY-26SEP20-B74.5", "no", 1.0, 0.7, ts="2026-09-19T15:00:00+00:00"),
                   settle("KXHIGHNY-26SEP20-B74.5", "no", 2.0, 1.0, ts="2026-09-20T15:00:00+00:00")]
        text = build_report(entries, load_rules(), NOW)
        self.assertIn("P&L this week (settled, after fees) | **+$0.00**", text)
        self.assertIn("P&L since going live | +$1.00", text)


if __name__ == "__main__":
    unittest.main()
