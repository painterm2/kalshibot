"""Gold and silver price markets: one pass of the commodities strategy.

Kalshi lists three kinds of gold and silver markets, all settled on Pyth prices:
  KXGOLD15M / KXSILVER15M  will the price at the end of a 15-minute window be at
                           least the price at its start (floor_strike)?
  KXGOLDH / KXSILVERH      will the price at the top of the hour be above a strike?
  KXGOLDD / KXSILVERD      will the 5pm New York price be above a strike?

They settle on Pyth's 24/7 "index" prices, which the owner's Pyth key can't read.
It can read Pyth's spot prices (XAU/USD, XAG/USD), which track the index while
metals markets are open. Each 15-minute market's floor_strike is the index price at
the window's start, so comparing it with the spot price at that moment measures the
gap (basis) between the two; the model adds it to the current spot price.

The model: the settlement price is lognormal around the current price, with a
spread from the last hour's realized volatility (never below a floor), shrinking
with the time left. Like the weather model, it is blended with the market's own
price and only trades where it disagrees with the market by more than fees.
Outside metals trading hours Pyth's spot price stops updating, and the bot sits out.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from kalshibot import bot
from kalshibot.kalshi import KalshiClient, Market
from kalshibot.ledger import Ledger
from kalshibot.risk import OrderProposal, check_order, size_order, trading_fee, usable_capital
from kalshibot.rules import Rules
from kalshibot.weather import _normal_cdf, market_mid

HERMES = "https://hermes.pyth.network"
Fetch = Callable[[str, dict | None], dict]


@dataclass(frozen=True)
class Asset:
    name: str
    feed_id: str  # Pyth spot feed the owner's key can read
    series: tuple[str, ...]
    # Floor on annualized volatility, so a quiet hour never makes the model overconfident.
    min_annual_vol: float


ASSETS = (
    Asset("Gold", "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
          ("KXGOLD15M", "KXGOLDH", "KXGOLDD"), 0.12),
    Asset("Silver", "f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e",
          ("KXSILVER15M", "KXSILVERH", "KXSILVERD"), 0.22),
)

# A spot price older than this means metals markets are closed (or Pyth is down): sit out.
MAX_STALENESS_SECONDS = 120
# Volatility: log returns of spot prices sampled every VOL_STEP minutes over VOL_LOOKBACK minutes.
VOL_LOOKBACK_MINUTES = 60
VOL_STEP_MINUTES = 5
MINUTES_PER_YEAR = 365 * 24 * 60
# Leave the last minutes of a market alone: traders watching tick-by-tick prices know more.
MIN_MINUTES_LEFT = 3
# How much to trust the model over the market, and the biggest gap still worth trusting.
MODEL_WEIGHT = 0.5
MAX_DISAGREEMENT = 0.20
# Spot and index should differ by a few dollars at most; more means a mismatched feed.
MAX_BASIS_FRACTION = 0.003


@dataclass(frozen=True)
class Quote:
    price: float
    publish_time: datetime


def requests_fetch(url: str, params: dict | None = None) -> dict:
    """GET from Pyth. The environment's network secret adds the Authorization header."""
    import requests
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def spot(asset: Asset, at: datetime | None = None, fetch: Fetch = requests_fetch) -> Quote:
    """Pyth spot price now, or as of `at`."""
    path = "latest" if at is None else str(int(at.timestamp()))
    data = fetch(f"{HERMES}/v2/updates/price/{path}", {"ids[]": asset.feed_id, "parsed": "true"})
    p = data["parsed"][0]["price"]
    return Quote(int(p["price"]) * 10 ** int(p["expo"]),
                 datetime.fromtimestamp(int(p["publish_time"]), tz=timezone.utc))


# Pyth rate-limits each IP (a 429 hit on 2026-10-12 with fresh lookups every minute), so
# volatility is recomputed at most every VOL_CACHE_MINUTES and the spot price at each
# 15-minute market's start is looked up once.
VOL_CACHE_MINUTES = 10
_vol_cache: dict[str, tuple[datetime, float]] = {}
_open_spot_cache: dict[tuple[str, datetime], float] = {}


def sigma_per_minute(asset: Asset, now: datetime, fetch: Fetch = requests_fetch) -> float:
    """Standard deviation of log price change per minute, from the last hour, with a floor."""
    cached = _vol_cache.get(asset.name)
    if cached and timedelta(0) <= now - cached[0] < timedelta(minutes=VOL_CACHE_MINUTES):
        return cached[1]
    sigma = _sigma_per_minute(asset, now, fetch)
    _vol_cache[asset.name] = (now, sigma)
    return sigma


def _sigma_per_minute(asset: Asset, now: datetime, fetch: Fetch) -> float:
    floor = asset.min_annual_vol / math.sqrt(MINUTES_PER_YEAR)
    steps = VOL_LOOKBACK_MINUTES // VOL_STEP_MINUTES
    try:
        prices = [spot(asset, now - timedelta(minutes=VOL_STEP_MINUTES * k), fetch).price
                  for k in range(steps, -1, -1)]
        returns = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
        realized = statistics.pstdev(returns) / math.sqrt(VOL_STEP_MINUTES) if len(returns) >= 4 else 0.0
    except Exception:
        realized = 0.0
    return max(realized, floor)


def prob_above(price: float, strike: float, sigma_min: float, minutes_left: float) -> float:
    """P(settlement price > strike), lognormal with no drift."""
    if minutes_left <= 0:
        return 1.0 if price > strike else 0.0
    spread = sigma_min * math.sqrt(minutes_left)
    return 1 - _normal_cdf(math.log(strike / price) / spread)


def yes_probability(market: Market, price: float, sigma_min: float, minutes_left: float) -> float | None:
    kind, lo, hi = market.strike_type, market.floor_strike, market.cap_strike
    above = lambda k: prob_above(price, k, sigma_min, minutes_left)
    if kind in ("greater", "greater_or_equal") and lo:
        return above(lo)
    if kind in ("less", "less_or_equal") and hi:
        return 1 - above(hi)
    if kind == "between" and lo and hi:
        return max(0.0, above(lo) - above(hi))
    return None


def blended(model_p: float, market: Market) -> float | None:
    mid = market_mid(market)
    if mid is None:
        return model_p
    if abs(model_p - mid) > MAX_DISAGREEMENT:
        return None
    return MODEL_WEIGHT * model_p + (1 - MODEL_WEIGHT) * mid


@dataclass(frozen=True)
class Candidate:
    market: Market
    side: str
    price: float
    win_probability: float
    model_probability: float
    edge: float
    category: str
    asset: str
    index_price: float
    minutes_left: float
    sigma_min: float

    def details(self) -> dict:
        return {"asset": self.asset, "index_price": round(self.index_price, 4),
                "minutes_left": round(self.minutes_left, 1), "sigma_per_min": round(self.sigma_min, 7)}


def basis(asset: Asset, fifteen_min: list[Market], now: datetime, fetch: Fetch) -> float | None:
    """Index minus spot, from the latest 15-minute market that has started (None if there isn't one)."""
    started = [m for m in fifteen_min if m.open_time and m.open_time <= now and m.floor_strike]
    if not started:
        return None
    m = max(started, key=lambda m: m.open_time)
    key = (asset.name, m.open_time)
    if key not in _open_spot_cache:
        _open_spot_cache[key] = spot(asset, m.open_time, fetch).price
    return m.floor_strike - _open_spot_cache[key]


def find_candidates(rules: Rules, client: KalshiClient, now: datetime, log,
                    fetch: Fetch = requests_fetch) -> list[Candidate]:
    """Best bet per event across gold and silver, highest edge first."""
    best: dict[str, Candidate] = {}
    excluded = {c.lower() for c in rules.excluded_categories}
    for asset in ASSETS:
        try:
            quote = spot(asset, None, fetch)
        except Exception as e:
            log(f"{asset.name}: skipped, couldn't read Pyth price ({e})")
            continue
        age = (now - quote.publish_time).total_seconds()
        if age > MAX_STALENESS_SECONDS:
            log(f"{asset.name}: Pyth price is {age / 60:.0f} min old (metals market closed?); sitting out")
            continue
        markets: dict[str, list[Market]] = {}
        categories: dict[str, str] = {}
        for series in asset.series:
            try:
                categories[series] = client.series_category(series)
                markets[series] = [m for m in client.markets(series) if m.status in ("active", "open", "")]
            except Exception as e:
                log(f"{series}: skipped, couldn't load markets ({e})")
        try:
            gap = basis(asset, markets.get(asset.series[0], []), now, fetch)
        except Exception as e:
            log(f"{asset.name}: skipped, couldn't measure index-spot gap ({e})")
            continue
        if gap is None:
            log(f"{asset.name}: no 15-minute market open to measure the index-spot gap; sitting out")
            continue
        if abs(gap) > MAX_BASIS_FRACTION * quote.price:
            log(f"{asset.name}: index and spot differ by {gap:+.2f}, too much to trust; sitting out")
            continue
        index_now = quote.price + gap
        sigma = sigma_per_minute(asset, now, fetch)
        for series, rows in markets.items():
            category = categories.get(series, "")
            if category.strip().lower() in excluded:
                continue
            for m in rows:
                minutes_left = (m.close_time - now).total_seconds() / 60
                if minutes_left < MIN_MINUTES_LEFT:
                    continue
                model_p = yes_probability(m, index_now, sigma, minutes_left)
                if model_p is None:
                    continue
                p_yes = blended(model_p, m)
                if p_yes is None:
                    continue
                for side, win_p, mp in (("yes", p_yes, model_p), ("no", 1 - p_yes, 1 - model_p)):
                    price = m.ask(side)
                    if price is None or not 0 < price < 1:
                        continue
                    edge = win_p - price - trading_fee(rules, 1, price)
                    cand = Candidate(m, side, price, win_p, mp, edge, category, asset.name,
                                     index_now, minutes_left, sigma)
                    if m.event_ticker not in best or edge > best[m.event_ticker].edge:
                        best[m.event_ticker] = cand
    return sorted(best.values(), key=lambda c: c.edge, reverse=True)


def run_once(rules: Rules, client: KalshiClient, mode: str, now: datetime, log=print,
             ledger: Ledger | None = None, fetch: Fetch = requests_fetch,
             kill_switch_dir: Path = bot.REPO_ROOT) -> int:
    """One pass: settle, scan, size, check, and (maybe) trade. Returns orders placed."""
    if mode not in bot.MODES:
        raise ValueError(f"mode must be one of {bot.MODES}")
    ledger = ledger or bot.ledger_for(mode, kill_switch_dir, "commodities")
    others = bot.other_ledgers("commodities", mode, kill_switch_dir)
    if mode == "dry-run" and not ledger.contributions():
        month = now.astimezone(ZoneInfo(rules.timezone)).strftime("%Y-%m")
        ledger.fund(month, rules.monthly_budget, now)
        log(f"paper ledger: starting with ${rules.monthly_budget:.2f} of paper money for {month}")

    bot.settle(client, ledger, now, log)
    state = bot.account_state(rules, client, ledger, mode, now, others)
    bot.enforce_total_loss_stop(rules, state, mode, kill_switch_dir, now, log)
    if bot.cash_out(rules, client, ledger, state, mode, now, log, kill_switch_dir):
        state = bot.account_state(rules, client, ledger, mode, now, others)
    log(f"[commodities {mode}] usable ${usable_capital(rules, state):.2f}, open {len(state.open_positions)}, "
        f"orders today {state.orders_today}, realized today ${state.realized_pnl_today:+.2f}")

    placed = 0
    for cand in find_candidates(rules, client, now, log, fetch):
        if cand.edge < rules.min_edge_per_contract:
            break
        m = cand.market
        header = (f"{m.ticker} {cand.side.upper()} @ ${cand.price:.2f}: model {cand.model_probability:.0%}, "
                  f"blended {cand.win_probability:.0%}, edge ${cand.edge:.3f} "
                  f"({cand.asset} {cand.index_price:.2f}, {cand.minutes_left:.0f} min left)")
        contracts = size_order(rules, state, cand.price, cand.win_probability)
        proposal = OrderProposal(m.ticker, m.event_ticker, cand.price, max(contracts, 1),
                                 cand.win_probability, m.close_time, cand.category)
        decision = check_order(rules, state, proposal, now, live=mode == "live", kill_switch_dir=kill_switch_dir)
        if contracts <= 0 or not decision.allowed:
            log(f"  skip  {header} -- {'; '.join(decision.reasons) or 'Kelly size rounds to 0 contracts'}")
            continue
        filled, cost = bot.execute(client, ledger, mode, cand, contracts, rules, now, log)
        log(f"  {'PAPER' if mode == 'dry-run' else 'BUY':5} {header} -- {filled}/{contracts} filled, cost ${cost:.2f}")
        placed += 1
        state = bot.account_state(rules, client, ledger, mode, now, others)
    return placed


def run_loop(rules: Rules, client: KalshiClient, mode: str, minutes: float, every_seconds: float,
             log=print, fetch: Fetch = requests_fetch, sleep=None, clock=None) -> None:
    """Run passes every `every_seconds` for `minutes`. A failed pass stops the loop in live mode
    (something unexpected touched real money) and is logged and skipped in dry-run."""
    import time
    sleep = sleep or time.sleep
    clock = clock or (lambda: datetime.now(timezone.utc))
    end = clock() + timedelta(minutes=minutes)
    while True:
        try:
            run_once(rules, client, mode, clock(), log, fetch=fetch)
        except Exception as e:
            log(f"pass failed: {type(e).__name__}: {e}")
            if mode == "live":
                raise
        if clock() + timedelta(seconds=every_seconds) >= end:
            return
        sleep(every_seconds)
