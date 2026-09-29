import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kalshibot.risk import (
    AccountState,
    OrderProposal,
    Position,
    check_order,
    check_sell,
    order_cost,
    size_order,
    trading_fee,
    usable_capital,
)
from kalshibot.rules import load_rules

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def fresh_state(**overrides):
    state = AccountState(
        cash_balance=100.0,
        contributions={"2026-09": 100.0},
        realized_pnl_lifetime=0.0,
        realized_pnl_month=0.0,
        realized_pnl_today=0.0,
    )
    return replace(state, **overrides)


def order(**overrides):
    base = OrderProposal(
        ticker="MKT-A",
        event_ticker="EVT-A",
        price=0.40,
        contracts=4,
        win_probability=0.55,
        close_time=NOW + timedelta(hours=6),
        category="Climate and Weather",
    )
    return replace(base, **overrides)


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check(self, state, proposal, live=False):
        return check_order(self.rules, state, proposal, NOW, live=live, kill_switch_dir=self.tmp.name)

    def test_good_order_is_allowed(self):
        decision = self.check(fresh_state(), order())
        self.assertTrue(decision.allowed, decision.reasons)

    def test_fee_rounds_up_to_cent(self):
        # 0.07 * 4 * 0.4 * 0.6 = 0.0672 -> 0.07
        self.assertEqual(trading_fee(self.rules, 4, 0.40), 0.07)
        self.assertEqual(order_cost(self.rules, 4, 0.40), 1.67)

    def test_contributions_over_budget_are_ignored(self):
        state = fresh_state(cash_balance=500.0, contributions={"2026-09": 500.0})
        self.assertEqual(usable_capital(self.rules, state), 100.0)

    def test_winnings_compound(self):
        state = fresh_state(cash_balance=130.0, realized_pnl_lifetime=30.0)
        self.assertEqual(usable_capital(self.rules, state), 130.0)

    def test_daily_limit_counts_open_positions(self):
        state = fresh_state(open_positions=[Position("MKT-B", "EVT-B", 1.0), Position("MKT-C", "EVT-C", 1.0)])
        decision = self.check(state, order())  # 2.00 + 1.67 > 3.00
        self.assertFalse(decision.allowed)
        self.assertTrue(any("at risk today" in r for r in decision.reasons))

    def test_daily_limit_counts_realized_losses(self):
        decision = self.check(fresh_state(realized_pnl_today=-2.0), order())
        self.assertFalse(decision.allowed)

    def test_gains_today_do_not_raise_the_limit(self):
        state = fresh_state(realized_pnl_today=20.0, open_positions=[Position("MKT-B", "EVT-B", 4.0)])
        self.assertFalse(self.check(state, order()).allowed)

    def test_monthly_loss_limit(self):
        decision = self.check(fresh_state(realized_pnl_month=-100.0), order())
        self.assertFalse(decision.allowed)

    def test_per_trade_limit(self):
        self.assertFalse(self.check(fresh_state(), order(contracts=8)).allowed)

    def test_long_dated_market_rejected(self):
        self.assertFalse(self.check(fresh_state(), order(close_time=NOW + timedelta(days=5))).allowed)

    def test_extreme_price_rejected(self):
        self.assertFalse(self.check(fresh_state(), order(price=0.97, win_probability=0.999, contracts=1)).allowed)

    def test_thin_edge_rejected(self):
        self.assertFalse(self.check(fresh_state(), order(win_probability=0.42)).allowed)

    def test_one_position_per_event(self):
        state = fresh_state(open_positions=[Position("MKT-A2", "EVT-A", 1.0)])
        self.assertFalse(self.check(state, order()).allowed)

    def test_kill_switch(self):
        Path(self.tmp.name, self.rules.kill_switch_file).touch()
        self.assertFalse(self.check(fresh_state(), order()).allowed)

    def test_live_orders_refused_while_live_trading_off(self):
        self.rules = replace(self.rules, live_trading=False)
        self.assertFalse(self.check(fresh_state(), order(), live=True).allowed)

    def test_order_cap(self):
        self.assertFalse(self.check(fresh_state(orders_today=20), order()).allowed)

    def test_sports_rejected(self):
        for category in ("Sports", "sports", " SPORTS "):
            decision = self.check(fresh_state(), order(category=category))
            self.assertFalse(decision.allowed, category)
            self.assertTrue(any("off limits" in r for r in decision.reasons))

    def test_missing_category_rejected(self):
        self.assertFalse(self.check(fresh_state(), order(category="")).allowed)

    def test_stays_out_of_owner_markets(self):
        decision = self.check(fresh_state(owner_tickers=frozenset({"MKT-A"})), order())
        self.assertFalse(decision.allowed)
        self.assertTrue(any("owner" in r for r in decision.reasons))

    def sell(self, state, ticker="MKT-B", side="yes", contracts=3):
        return check_sell(self.rules, state, ticker, side, contracts, live=False, kill_switch_dir=self.tmp.name)

    def test_can_sell_own_contracts(self):
        state = fresh_state(open_positions=[Position("MKT-B", "EVT-B", 1.5, side="yes", contracts=3)])
        self.assertTrue(self.sell(state).allowed)

    def test_cannot_sell_more_than_bot_holds(self):
        # Owner may hold more in this market, but the bot only owns 3.
        state = fresh_state(
            open_positions=[Position("MKT-B", "EVT-B", 1.5, side="yes", contracts=3)],
            owner_tickers=frozenset({"MKT-B"}),
        )
        self.assertFalse(self.sell(state, contracts=4).allowed)

    def test_cannot_sell_owner_only_market(self):
        state = fresh_state(owner_tickers=frozenset({"MKT-Z"}))
        self.assertFalse(self.sell(state, ticker="MKT-Z", contracts=1).allowed)

    def test_cannot_sell_other_side(self):
        state = fresh_state(open_positions=[Position("MKT-B", "EVT-B", 1.5, side="yes", contracts=3)])
        self.assertFalse(self.sell(state, side="no", contracts=1).allowed)

    def test_sell_allowed_past_daily_loss_limit(self):
        state = fresh_state(
            realized_pnl_today=-5.0,
            open_positions=[Position("MKT-B", "EVT-B", 1.5, side="yes", contracts=3)],
        )
        self.assertTrue(self.sell(state).allowed)

    def test_sizing_respects_caps(self):
        state = fresh_state()
        contracts = size_order(self.rules, state, 0.40, 0.60)
        self.assertGreater(contracts, 0)
        self.assertLessEqual(order_cost(self.rules, contracts, 0.40), self.rules.max_risk_per_trade)
        self.assertTrue(self.check(state, order(contracts=contracts, win_probability=0.60)).allowed)

    def test_sizing_zero_without_edge(self):
        self.assertEqual(size_order(self.rules, fresh_state(), 0.50, 0.50), 0)

    def test_sizing_zero_when_daily_budget_used(self):
        state = fresh_state(open_positions=[Position("MKT-B", "EVT-B", 5.0)])
        self.assertEqual(size_order(self.rules, state, 0.40, 0.70), 0)


if __name__ == "__main__":
    unittest.main()
