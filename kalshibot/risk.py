"""Risk manager: every order must pass check_order() or check_sell() before it is sent.

Money is handled in dollars. Kalshi contracts pay $1.00 if they win and $0.00
if they lose, so buying one contract at price p risks p (plus fees) to win 1 - p.
New positions are always opened by buying YES or NO. Selling only ever closes
contracts the bot itself bought.

The account is shared with the owner's own trading. Everything in AccountState
except cash_balance and owner_tickers covers the bot's own trades only, taken
from the bot's ledger and not from the account totals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kalshibot.rules import Rules


@dataclass(frozen=True)
class Position:
    ticker: str
    event_ticker: str
    # Most this position can still lose: what was paid for it (incl. fees)
    # minus anything already recovered by partial sells.
    max_loss: float
    side: str = "yes"
    contracts: int = 1


@dataclass
class AccountState:
    # Cash sitting in the Kalshi account right now.
    cash_balance: float
    # Money you added, keyed by "YYYY-MM". Anything over the monthly budget
    # in a month is ignored: the bot never trades with it.
    contributions: dict[str, float]
    # The bot's realized P&L (after fees) over its lifetime, this month, and today.
    realized_pnl_lifetime: float
    realized_pnl_month: float
    realized_pnl_today: float
    # Positions the bot opened (from its own ledger, never the account totals).
    open_positions: list[Position] = field(default_factory=list)
    orders_today: int = 0
    # Markets where the owner holds contracts of their own. The bot stays out
    # of them, because Kalshi nets YES and NO in a market and a bot order
    # there could close or change the owner's position.
    owner_tickers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OrderProposal:
    ticker: str
    event_ticker: str
    # Price per contract of the side being bought, in dollars (0.01 to 0.99).
    price: float
    contracts: int
    # The strategy's estimate that the side being bought wins.
    win_probability: float
    close_time: datetime


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]


def trading_fee(rules: Rules, contracts: int, price: float) -> float:
    """Kalshi taker fee, rounded up to the next cent."""
    cents = rules.taker_fee_rate * contracts * price * (1 - price) * 100
    return math.ceil(round(cents, 6)) / 100


def order_cost(rules: Rules, contracts: int, price: float) -> float:
    """Total the order can lose: the premium paid plus fees."""
    return round(contracts * price + trading_fee(rules, contracts, price), 2)


def usable_capital(rules: Rules, state: AccountState) -> float:
    """Money the bot may trade with: capped contributions plus what it has made.

    Never more than the cash actually in the account.
    """
    contributed = sum(min(amount, rules.monthly_budget) for amount in state.contributions.values())
    return max(0.0, min(state.cash_balance, contributed + state.realized_pnl_lifetime))


def daily_risk_used(state: AccountState) -> float:
    """Today's realized losses plus the worst case of everything still open.

    Keeping this under the daily limit means no day's realized losses can
    exceed the limit, even if every open position goes to zero.
    """
    realized_loss_today = max(0.0, -state.realized_pnl_today)
    return realized_loss_today + sum(p.max_loss for p in state.open_positions)


def size_order(rules: Rules, state: AccountState, price: float, win_probability: float) -> int:
    """Number of contracts to buy: fractional Kelly, then clipped by every cap."""
    if not 0 < price < 1 or win_probability <= price:
        return 0
    kelly = (win_probability - price) / (1 - price)
    stake = rules.kelly_fraction * kelly * usable_capital(rules, state)
    cap = min(
        stake,
        rules.max_risk_per_trade,
        rules.daily_loss_limit - daily_risk_used(state),
    )
    contracts = math.floor(cap / price)
    while contracts > 0 and order_cost(rules, contracts, price) > cap:
        contracts -= 1
    return max(contracts, 0)


def check_order(
    rules: Rules,
    state: AccountState,
    order: OrderProposal,
    now: datetime,
    live: bool,
    kill_switch_dir: Path | str = ".",
) -> Decision:
    """Return whether the order is allowed, and every rule it breaks if not."""
    reasons: list[str] = []
    cost = order_cost(rules, order.contracts, order.price)
    fee = trading_fee(rules, order.contracts, order.price)

    if (Path(kill_switch_dir) / rules.kill_switch_file).exists():
        reasons.append("kill switch is on")
    if live and not rules.live_trading:
        reasons.append("live trading is disabled in rules.toml")
    if order.contracts <= 0:
        reasons.append("order has no contracts")
    if state.orders_today >= rules.max_orders_per_day:
        reasons.append(f"already placed {state.orders_today} orders today")

    if -state.realized_pnl_month >= rules.monthly_loss_limit:
        reasons.append("monthly loss limit reached; trading paused until next month")
    if daily_risk_used(state) + cost > rules.daily_loss_limit + 1e-9:
        reasons.append(
            f"would put ${daily_risk_used(state) + cost:.2f} at risk today "
            f"(limit ${rules.daily_loss_limit:.2f})"
        )
    if cost > rules.max_risk_per_trade + 1e-9:
        reasons.append(f"order risks ${cost:.2f} (per-trade limit ${rules.max_risk_per_trade:.2f})")
    if cost > usable_capital(rules, state) + 1e-9:
        reasons.append(f"order costs ${cost:.2f} but only ${usable_capital(rules, state):.2f} is usable")

    if not rules.min_price <= order.price <= rules.max_price:
        reasons.append(f"price {order.price:.2f} outside {rules.min_price:.2f}-{rules.max_price:.2f}")
    hours_left = (order.close_time - now).total_seconds() / 3600
    if hours_left <= 0:
        reasons.append("market already closed")
    elif hours_left > rules.max_hours_to_close:
        reasons.append(f"market closes in {hours_left:.0f}h (limit {rules.max_hours_to_close:g}h)")
    same_event = sum(1 for p in state.open_positions if p.event_ticker == order.event_ticker)
    if same_event >= rules.max_positions_per_event:
        reasons.append(f"already {same_event} open position(s) in {order.event_ticker}")
    if order.ticker in state.owner_tickers:
        reasons.append(f"owner holds a position in {order.ticker}")

    if order.contracts > 0:
        edge = order.win_probability - order.price - fee / order.contracts
        if edge < rules.min_edge_per_contract:
            reasons.append(
                f"expected edge ${edge:.3f}/contract after fees "
                f"(minimum ${rules.min_edge_per_contract:.3f})"
            )

    return Decision(allowed=not reasons, reasons=tuple(reasons))


def check_sell(
    rules: Rules,
    state: AccountState,
    ticker: str,
    side: str,
    contracts: int,
    live: bool,
    kill_switch_dir: Path | str = ".",
) -> Decision:
    """Allow a sell only of contracts the bot bought and still holds.

    Loss limits don't apply: selling can only shrink the bot's risk.
    """
    reasons: list[str] = []
    held = sum(p.contracts for p in state.open_positions if p.ticker == ticker and p.side == side)

    if (Path(kill_switch_dir) / rules.kill_switch_file).exists():
        reasons.append("kill switch is on")
    if live and not rules.live_trading:
        reasons.append("live trading is disabled in rules.toml")
    if state.orders_today >= rules.max_orders_per_day:
        reasons.append(f"already placed {state.orders_today} orders today")
    if contracts <= 0:
        reasons.append("order has no contracts")
    elif contracts > held:
        reasons.append(f"bot holds only {held} {side.upper()} in {ticker}; it never sells the owner's contracts")

    return Decision(allowed=not reasons, reasons=tuple(reasons))
