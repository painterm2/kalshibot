"""One pass of the weather strategy: settle, scan, size, check, and (maybe) trade.

Modes:
  dry-run  Reads real markets and forecasts, places no orders. Trades it would
           have made go into a paper ledger at the ask price, and settle
           against real results, so the paper record shows how the model does.
  demo     Places real orders on Kalshi's demo exchange (play money).
  live     Places real-money orders. check_order() refuses these unless
           live_trading = true in rules.toml.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshibot import weather
from kalshibot.kalshi import KalshiClient, Market, contract_count, price_dollars
from kalshibot.ledger import Ledger
from kalshibot.risk import AccountState, OrderProposal, check_order, size_order, trading_fee, usable_capital
from kalshibot.rules import Rules

REPO_ROOT = Path(__file__).resolve().parent.parent
MODES = ("dry-run", "demo", "live")


@dataclass(frozen=True)
class Candidate:
    market: Market
    side: str
    price: float
    win_probability: float
    model_probability: float
    edge: float  # per contract, after the one-contract fee
    forecast: weather.Forecast
    category: str


def ledger_for(mode: str, directory: Path = REPO_ROOT) -> Ledger:
    return Ledger(directory / f"ledger-{mode}.jsonl")


def owner_tickers(client: KalshiClient, ledger: Ledger, mode: str) -> frozenset[str]:
    """Markets where the owner has contracts or resting orders of their own."""
    bot_net = {} if mode == "dry-run" else ledger.net_contracts()
    owned = {t for t, n in client.positions().items() if abs(n - bot_net.get(t, 0)) > 1e-9}
    # The bot's orders are immediate-or-cancel and never rest, so any resting order is the owner's.
    owned |= {o["ticker"] for o in client.resting_orders()}
    return frozenset(owned)


def account_state(rules: Rules, client: KalshiClient, ledger: Ledger, mode: str, now: datetime) -> AccountState:
    owned = owner_tickers(client, ledger, mode)
    if mode == "dry-run":
        # Paper cash: what the bot was given, plus what it made, minus what's tied up.
        paper = ledger.state(0.0, now, rules.timezone, owned)
        cash = sum(paper.contributions.values()) + paper.realized_pnl_lifetime - sum(
            p.max_loss for p in paper.open_positions)
        return ledger.state(max(cash, 0.0), now, rules.timezone, owned)
    return ledger.state(client.balance(), now, rules.timezone, owned)


def settle(client: KalshiClient, ledger: Ledger, now: datetime, log) -> None:
    for pos in ledger.open_positions():
        try:
            market = client.market(pos.ticker)
        except Exception as e:
            log(f"couldn't check {pos.ticker} for settlement ({e}); it stays open")
            continue
        if market.result not in ("yes", "no"):
            continue
        payout = float(pos.contracts) if market.result == pos.side else 0.0
        pnl = round(payout - pos.max_loss, 2)
        ledger.append({"type": "settle", "ts": now.isoformat(), "ticker": pos.ticker, "side": pos.side,
                       "contracts": pos.contracts, "payout": payout, "pnl": pnl})
        log(f"settled {pos.ticker} {pos.side.upper()} x{pos.contracts}: {market.result.upper()}, P&L ${pnl:+.2f}")


def find_candidates(rules: Rules, client: KalshiClient, now: datetime, log,
                    fetch: weather.Fetch = weather.requests_fetch) -> list[Candidate]:
    """Best bet per event across the weather series, highest edge first."""
    best: dict[str, Candidate] = {}
    for station in weather.STATIONS:
        try:
            category = client.series_category(station.series)
            markets = client.markets(station.series)
        except Exception as e:  # one bad series shouldn't stop the others
            log(f"{station.series}: skipped, couldn't load markets ({e})")
            continue
        if category.strip().lower() in {c.lower() for c in rules.excluded_categories}:
            continue
        if not markets:
            continue
        try:
            highs = weather.forecast_highs(station, fetch)
        except Exception as e:
            log(f"{station.series}: skipped, couldn't load NWS forecast ({e})")
            continue

        by_event: dict[str, list[Market]] = defaultdict(list)
        for m in markets:
            by_event[m.event_ticker].append(m)
        for event, event_markets in by_event.items():
            day = weather.event_date(event)
            forecast = weather.build_forecast(station, day, now, fetch, highs) if day else None
            if forecast is None:
                continue
            for m in event_markets:
                model_p = weather.yes_probability(m, forecast.high_f, forecast.sigma, forecast.observed_max_f)
                if model_p is None:
                    continue
                p_yes = weather.blended_probability(model_p, m)
                if p_yes is None:
                    continue
                for side, win_p in (("yes", p_yes), ("no", 1 - p_yes)):
                    price = m.ask(side)
                    if price is None or not 0 < price < 1:
                        continue
                    edge = win_p - price - trading_fee(rules, 1, price)
                    cand = Candidate(m, side, price, win_p, model_p if side == "yes" else 1 - model_p,
                                     edge, forecast, category)
                    if event not in best or edge > best[event].edge:
                        best[event] = cand
    return sorted(best.values(), key=lambda c: c.edge, reverse=True)


def execute(client: KalshiClient, ledger: Ledger, mode: str, cand: Candidate, contracts: int,
            rules: Rules, now: datetime) -> tuple[int, float]:
    """Place (or paper-trade) the order. Returns contracts filled and total cost."""
    m = cand.market
    if mode == "dry-run":
        filled, avg_price, order_id = contracts, cand.price, None
    else:
        order = client.buy(m.ticker, cand.side, contracts, cand.price)
        order_id = order.get("order_id")
        fills = client.fills(order_id) if order_id else []
        filled = int(sum(contract_count(f, "count") for f in fills))
        spent = sum(contract_count(f, "count") * (price_dollars(f, f"{cand.side}_price") or cand.price) for f in fills)
        avg_price = spent / filled if filled else cand.price
    cost = round(filled * avg_price + (trading_fee(rules, filled, avg_price) if filled else 0), 2)
    ledger.append({
        "type": "buy", "ts": now.isoformat(), "ticker": m.ticker, "event_ticker": m.event_ticker,
        "side": cand.side, "contracts": filled, "requested": contracts, "price": round(avg_price, 4),
        "cost": cost, "order_id": order_id, "simulated": mode == "dry-run",
    })
    return filled, cost


def run_once(rules: Rules, client: KalshiClient, mode: str, now: datetime, log=print,
             ledger: Ledger | None = None, fetch: weather.Fetch = weather.requests_fetch,
             kill_switch_dir: Path = REPO_ROOT) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    ledger = ledger or ledger_for(mode)
    live = mode == "live"
    if mode == "dry-run" and not ledger.contributions():
        month = now.astimezone(ZoneInfo(rules.timezone)).strftime("%Y-%m")
        ledger.fund(month, rules.monthly_budget, now)
        log(f"paper ledger: starting with ${rules.monthly_budget:.2f} of paper money for {month}")

    log(f"mode={mode} exchange={client.env}")
    settle(client, ledger, now, log)
    state = account_state(rules, client, ledger, mode, now)
    log(f"usable capital ${usable_capital(rules, state):.2f}, open positions {len(state.open_positions)}, "
        f"orders today {state.orders_today}, realized today ${state.realized_pnl_today:+.2f}")

    candidates = find_candidates(rules, client, now, log, fetch)
    placed = 0
    for cand in candidates:
        if cand.edge < rules.min_edge_per_contract:
            break
        m, f = cand.market, cand.forecast
        header = (f"{m.ticker} buy {cand.side.upper()} @ ${cand.price:.2f}: model {cand.model_probability:.0%}, "
                  f"blended {cand.win_probability:.0%}, edge ${cand.edge:.3f} "
                  f"(forecast {f.high_f:.0f}F ±{f.sigma:g}"
                  + (f", observed ≥{f.observed_max_f}F" if f.observed_max_f is not None else "") + ")")
        contracts = size_order(rules, state, cand.price, cand.win_probability)
        proposal = OrderProposal(m.ticker, m.event_ticker, cand.price, max(contracts, 1),
                                 cand.win_probability, m.close_time, cand.category)
        decision = check_order(rules, state, proposal, now, live=live, kill_switch_dir=kill_switch_dir)
        if contracts <= 0 or not decision.allowed:
            why = "; ".join(decision.reasons) or "Kelly size rounds to 0 contracts"
            log(f"  skip  {header} -- {why}")
            continue
        filled, cost = execute(client, ledger, mode, cand, contracts, rules, now)
        verb = "PAPER" if mode == "dry-run" else "BUY"
        log(f"  {verb:5} {header} -- {filled}/{contracts} filled, cost ${cost:.2f}")
        placed += 1
        state = account_state(rules, client, ledger, mode, now)
    if not candidates or candidates[0].edge < rules.min_edge_per_contract:
        log("no market clears the minimum edge right now")
    log(f"done: {placed} order(s) {'simulated' if mode == 'dry-run' else 'placed'}")
