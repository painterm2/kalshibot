"""The bot's own record of its money and trades, one JSON object per line.

The Kalshi account is shared with the owner, so the bot never infers its
positions or P&L from account totals. Everything it knows about itself comes
from this file. Entry types:

  fund    money the owner gave the bot:          month, amount
  buy     an order the bot placed (maybe unfilled): ticker, event_ticker, side, contracts, price, cost
  settle  a bot position that settled:           ticker, side, contracts, payout, pnl

Each mode (dry run, demo, live) keeps its own file so paper trades never mix
with real ones.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshibot.risk import AccountState, Position


@dataclass
class Ledger:
    path: Path

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    def fund(self, month: str, amount: float, now: datetime) -> None:
        self.append({"type": "fund", "ts": now.isoformat(), "month": month, "amount": amount})

    def contributions(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for e in self.entries():
            if e["type"] == "fund":
                totals[e["month"]] += e["amount"]
        return dict(totals)

    def open_positions(self) -> list[Position]:
        """Bot positions that haven't settled, one per (ticker, side)."""
        held: dict[tuple[str, str], dict] = {}
        for e in self.entries():
            key = (e.get("ticker"), e.get("side"))
            if e["type"] == "buy" and e["contracts"] > 0:
                pos = held.setdefault(key, {"event": e["event_ticker"], "contracts": 0, "cost": 0.0})
                pos["contracts"] += e["contracts"]
                pos["cost"] += e["cost"]
            elif e["type"] == "settle":
                held.pop(key, None)
        return [
            Position(ticker, p["event"], round(p["cost"], 2), side, p["contracts"])
            for (ticker, side), p in held.items()
        ]

    def state(self, cash_balance: float, now: datetime, tz: str, owner_tickers: frozenset[str]) -> AccountState:
        local = now.astimezone(ZoneInfo(tz))
        pnl_life = pnl_month = pnl_today = 0.0
        orders_today = 0
        for e in self.entries():
            ts = datetime.fromisoformat(e["ts"]).astimezone(ZoneInfo(tz))
            same_month = (ts.year, ts.month) == (local.year, local.month)
            if e["type"] == "settle":
                pnl_life += e["pnl"]
                pnl_month += e["pnl"] if same_month else 0
                pnl_today += e["pnl"] if ts.date() == local.date() else 0
            elif e["type"] == "buy" and ts.date() == local.date():
                orders_today += 1
        return AccountState(
            cash_balance=cash_balance,
            contributions=self.contributions(),
            realized_pnl_lifetime=round(pnl_life, 2),
            realized_pnl_month=round(pnl_month, 2),
            realized_pnl_today=round(pnl_today, 2),
            open_positions=self.open_positions(),
            orders_today=orders_today,
            owner_tickers=owner_tickers,
        )

    def net_contracts(self) -> dict[str, float]:
        """The bot's open contracts per market, signed like Kalshi's: +YES, -NO."""
        net: dict[str, float] = defaultdict(float)
        for p in self.open_positions():
            net[p.ticker] += p.contracts if p.side == "yes" else -p.contracts
        return dict(net)
