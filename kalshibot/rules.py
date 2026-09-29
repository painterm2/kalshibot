"""Load and validate the trading rules from rules.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules.toml"


@dataclass(frozen=True)
class Rules:
    monthly_budget: float
    daily_loss_limit: float
    monthly_loss_limit: float
    timezone: str
    kelly_fraction: float
    max_risk_per_trade: float
    min_edge_per_contract: float
    max_hours_to_close: float
    min_price: float
    max_price: float
    max_positions_per_event: int
    taker_fee_rate: float
    live_trading: bool
    kill_switch_file: str
    max_orders_per_day: int

    def __post_init__(self) -> None:
        if self.monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive")
        if not 0 < self.daily_loss_limit <= self.monthly_budget:
            raise ValueError("daily_loss_limit must be positive and at most monthly_budget")
        if not 0 < self.monthly_loss_limit <= self.monthly_budget:
            raise ValueError("monthly_loss_limit must be positive and at most monthly_budget")
        if not 0 < self.max_risk_per_trade <= self.daily_loss_limit:
            raise ValueError("max_risk_per_trade must be positive and at most daily_loss_limit")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0 < self.min_price < self.max_price < 1:
            raise ValueError("need 0 < min_price < max_price < 1")
        if self.max_hours_to_close <= 0:
            raise ValueError("max_hours_to_close must be positive")


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> Rules:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Rules(
        monthly_budget=raw["funding"]["monthly_budget"],
        daily_loss_limit=raw["losses"]["daily_loss_limit"],
        monthly_loss_limit=raw["losses"]["monthly_loss_limit"],
        timezone=raw["losses"]["timezone"],
        kelly_fraction=raw["sizing"]["kelly_fraction"],
        max_risk_per_trade=raw["sizing"]["max_risk_per_trade"],
        min_edge_per_contract=raw["sizing"]["min_edge_per_contract"],
        max_hours_to_close=raw["markets"]["max_hours_to_close"],
        min_price=raw["markets"]["min_price"],
        max_price=raw["markets"]["max_price"],
        max_positions_per_event=raw["markets"]["max_positions_per_event"],
        taker_fee_rate=raw["fees"]["taker_fee_rate"],
        live_trading=raw["safety"]["live_trading"],
        kill_switch_file=raw["safety"]["kill_switch_file"],
        max_orders_per_day=raw["safety"]["max_orders_per_day"],
    )
