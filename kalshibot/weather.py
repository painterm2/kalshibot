"""Weather model for Kalshi's daily high temperature markets.

Each market settles on the high temperature (whole degrees F) in the National
Weather Service's Daily Climate Report for one station. The model treats the
settlement high as normal around the NWS point forecast for that station, with
a spread that widens with lead time. On the day itself, the highest temperature
already observed is a floor: the day's high can't come in below it.

The spreads are deliberately wider than NWS's typical error. An overconfident
model is the most likely way to lose money, and the probability is then blended
with the market's own price (MODEL_WEIGHT) so the bot only bets where it
disagrees with the market by a lot.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from kalshibot.kalshi import Market


@dataclass(frozen=True)
class Station:
    series: str
    name: str
    station_id: str
    lat: float
    lon: float
    tz: str


STATIONS = (
    Station("KXHIGHNY", "New York (Central Park)", "KNYC", 40.7789, -73.9692, "America/New_York"),
    Station("KXHIGHCHI", "Chicago (Midway)", "KMDW", 41.7868, -87.7522, "America/Chicago"),
    Station("KXHIGHMIA", "Miami", "KMIA", 25.7959, -80.2870, "America/New_York"),
    Station("KXHIGHAUS", "Austin (Bergstrom)", "KAUS", 30.1945, -97.6699, "America/Chicago"),
    Station("KXHIGHDEN", "Denver", "KDEN", 39.8466, -104.6562, "America/Denver"),
    Station("KXHIGHLAX", "Los Angeles (LAX)", "KLAX", 33.9382, -118.3866, "America/Los_Angeles"),
    Station("KXHIGHPHIL", "Philadelphia", "KPHL", 39.8733, -75.2268, "America/New_York"),
)

# Standard deviation of (settlement high - NWS forecast high) in degrees F, by
# days ahead. NWS day-1 max temperature errors average about 2-3F; these are wider on purpose.
SIGMA_BY_LEAD_DAYS = {0: 2.5, 1: 3.0, 2: 3.5}
# How much to trust the model over the market price. 1.0 ignores the market.
MODEL_WEIGHT = 0.6
# Skip a market when the model and the market mid differ by more than this. A gap
# that big almost always means the market knows something the model doesn't.
MAX_DISAGREEMENT = 0.25
# Days ahead the bot trades. Same-day markets (0) are left alone: by the afternoon,
# traders watching minute-by-minute readings know the high, and a morning forecast can't compete.
TRADE_LEAD_DAYS = (1, 2)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event_ticker: str) -> date | None:
    """KXHIGHNY-26SEP30 -> 2026-09-30."""
    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not match or match.group(2) not in _MONTHS:
        return None
    year, month, day = match.groups()
    return date(2000 + int(year), _MONTHS[month], int(day))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_high_at_least(value: float, mean: float, sigma: float) -> float:
    """P(rounded high >= value) for an integer value, with a continuity correction."""
    return 1 - _normal_cdf((value - 0.5 - mean) / sigma)


def yes_probability(market: Market, mean: float, sigma: float, observed_floor: int | None = None) -> float | None:
    """Chance the market settles YES, or None if its strikes can't be read.

    Kalshi's strikes: "greater" is high > floor_strike, "less" is high <
    cap_strike, "between" is floor_strike <= high <= cap_strike.
    """
    def at_least(v: float) -> float:
        if observed_floor is not None and v <= observed_floor:
            return 1.0
        p = prob_high_at_least(v, mean, sigma)
        if observed_floor is not None:
            p /= prob_high_at_least(observed_floor, mean, sigma) or 1e-12
        return min(1.0, p)

    kind, lo, hi = market.strike_type, market.floor_strike, market.cap_strike
    if kind == "greater" and lo is not None:
        p = at_least(math.floor(lo) + 1)
    elif kind == "less" and hi is not None:
        p = 1 - at_least(math.ceil(hi))
    elif kind == "between" and lo is not None and hi is not None:
        p = at_least(math.ceil(lo)) - at_least(math.floor(hi) + 1)
    else:
        return None
    return min(1.0, max(0.0, p))


def market_mid(market: Market) -> float | None:
    if market.yes_bid is None or market.yes_ask is None or market.yes_ask <= 0:
        return None
    return (market.yes_bid + market.yes_ask) / 2


def blended_probability(model_p: float, market: Market, weight: float = MODEL_WEIGHT) -> float | None:
    """Pull the model toward the market mid. None when they disagree too much to trust the model."""
    mid = market_mid(market)
    if mid is None:
        return model_p
    if abs(model_p - mid) > MAX_DISAGREEMENT:
        return None
    return weight * model_p + (1 - weight) * mid


@dataclass(frozen=True)
class Forecast:
    station: Station
    day: date
    high_f: float
    # Highest temperature observed so far that day (whole F), if the day has started.
    observed_max_f: int | None
    lead_days: int

    @property
    def sigma(self) -> float:
        return SIGMA_BY_LEAD_DAYS.get(self.lead_days, 4.0)


# Weather data: api.weather.gov (no key needed, but it wants a User-Agent).

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "kalshibot (github.com/painterm2/kalshibot)"
Fetch = Callable[[str, dict | None], dict]


def requests_fetch(url: str, params: dict | None = None) -> dict:
    import requests

    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def standard_day_start(station: Station, day: date) -> datetime:
    """Midnight local *standard* time: the NWS climate day ignores daylight saving."""
    tz = ZoneInfo(station.tz)
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
    std_offset = noon.utcoffset() - (noon.dst() or timedelta(0))
    return datetime(day.year, day.month, day.day, tzinfo=timezone(std_offset))


def forecast_highs(station: Station, fetch: Fetch = requests_fetch) -> dict[date, float]:
    """NWS daytime forecast highs (F) by local date."""
    point = fetch(f"{NWS_BASE}/points/{station.lat},{station.lon}", None)
    periods = fetch(point["properties"]["forecast"], None)["properties"]["periods"]
    highs: dict[date, float] = {}
    for period in periods:
        if not period.get("isDaytime"):
            continue
        temp = float(period["temperature"])
        if period.get("temperatureUnit", "F") == "C":
            temp = temp * 9 / 5 + 32
        highs.setdefault(datetime.fromisoformat(period["startTime"]).date(), temp)
    return highs


def observed_max(station: Station, day: date, now: datetime, fetch: Fetch = requests_fetch) -> int | None:
    """Highest temperature (whole F) reported at the station so far on the climate day."""
    start = standard_day_start(station, day)
    if now < start:
        return None
    data = fetch(f"{NWS_BASE}/stations/{station.station_id}/observations", {"start": start.isoformat()})
    temps = []
    for feature in data.get("features", []):
        props = feature["properties"]
        value = (props.get("temperature") or {}).get("value")
        if value is None or datetime.fromisoformat(props["timestamp"]) >= start + timedelta(days=1):
            continue
        temps.append(value * 9 / 5 + 32)
    # Hourly reports round and can miss the peak between reports, so back off 1F.
    return math.floor(max(temps)) - 1 if temps else None


def build_forecast(station: Station, day: date, now: datetime, fetch: Fetch = requests_fetch,
                   highs: dict[date, float] | None = None) -> Forecast | None:
    highs = forecast_highs(station, fetch) if highs is None else highs
    today = now.astimezone(ZoneInfo(station.tz)).date()
    lead = (day - today).days
    if lead not in TRADE_LEAD_DAYS:
        return None
    observed = observed_max(station, day, now, fetch) if lead == 0 else None
    if day not in highs:
        # The NWS drops today's daytime period in the evening. Traders with minute-by-minute
        # data know the high by then, so the bot sits those markets out.
        return None
    return Forecast(station, day, highs[day], observed, lead)
