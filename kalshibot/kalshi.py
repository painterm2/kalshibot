"""Minimal Kalshi Trade API v2 client.

Requests are signed with the account's RSA key: the signature is RSA-PSS
(SHA-256) over timestamp_ms + METHOD + path, where path excludes the query
string. The key id and PEM are read from the KALSHI_KEY_ID and
KALSHI_PRIVATE_KEY environment variables and are never logged.

Prices come back either as integer cents (yes_ask) or, in newer responses,
as dollar strings (yes_ask_dollars). Market normalizes both to dollars.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


class KalshiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kalshi API error {status}: {message}")
        self.status = status


def price_dollars(raw: dict, name: str) -> float | None:
    """Read a price field as dollars, accepting `name_dollars` or cents in `name`."""
    if raw.get(f"{name}_dollars") not in (None, ""):
        return float(raw[f"{name}_dollars"])
    if raw.get(name) is not None:
        return raw[name] / 100
    return None


def contract_count(raw: dict, name: str) -> float:
    """Read a contract count, accepting `name_fp` (fixed-point string) or an int."""
    if raw.get(f"{name}_fp") not in (None, ""):
        return float(raw[f"{name}_fp"])
    return float(raw.get(name) or 0)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    status: str
    close_time: datetime
    # "greater", "less" or "between"; strikes apply to the settlement value.
    strike_type: str
    floor_strike: float | None
    cap_strike: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    result: str
    title: str

    @classmethod
    def from_api(cls, raw: dict) -> "Market":
        yes_bid, yes_ask = price_dollars(raw, "yes_bid"), price_dollars(raw, "yes_ask")
        no_bid, no_ask = price_dollars(raw, "no_bid"), price_dollars(raw, "no_ask")
        # A NO ask is the other side of a YES bid, and vice versa.
        if no_ask is None and yes_bid is not None:
            no_ask = round(1 - yes_bid, 4)
        if no_bid is None and yes_ask is not None:
            no_bid = round(1 - yes_ask, 4)
        return cls(
            ticker=raw["ticker"],
            event_ticker=raw["event_ticker"],
            status=raw.get("status", ""),
            close_time=parse_time(raw["close_time"]),
            strike_type=raw.get("strike_type", ""),
            floor_strike=raw.get("floor_strike"),
            cap_strike=raw.get("cap_strike"),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            result=raw.get("result") or "",
            title=raw.get("title", ""),
        )

    def ask(self, side: str) -> float | None:
        return self.yes_ask if side == "yes" else self.no_ask


def load_private_key(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


# transport(method, url, headers, params, json_body) -> (status, parsed json)
Transport = Callable[[str, str, dict, dict | None, dict | None], tuple[int, Any]]


def requests_transport(method, url, headers, params, json_body):
    import requests

    resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=20)
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:200]}
    return resp.status_code, body


class KalshiClient:
    def __init__(
        self,
        env: str,
        key_id: str,
        private_key_pem: str,
        transport: Transport = requests_transport,
    ):
        if env not in BASE_URLS:
            raise ValueError(f"env must be one of {sorted(BASE_URLS)}")
        self.env = env
        self.base_url = BASE_URLS[env]
        self._key_id = key_id
        self._key = load_private_key(private_key_pem)
        self._transport = transport

    @classmethod
    def from_env(cls, env: str, transport: Transport = requests_transport) -> "KalshiClient":
        key_id = os.environ.get("KALSHI_KEY_ID", "").strip()
        pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip().replace("\\n", "\n")
        if not key_id or not pem:
            raise RuntimeError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY must be set")
        return cls(env, key_id, pem, transport)

    def __repr__(self) -> str:
        return f"KalshiClient(env={self.env!r})"

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        full_path = "/trade-api/v2" + path
        timestamp = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": sign(self._key, timestamp, method, full_path),
            "Content-Type": "application/json",
        }
        status, data = self._transport(method, self.base_url + path, headers, params, body)
        if status >= 400:
            message = data.get("error", data) if isinstance(data, dict) else data
            raise KalshiError(status, str(message)[:300])
        return data

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            page = self.request("GET", path, {**params, **({"cursor": cursor} if cursor else {})})
            items.extend(page.get(key) or [])
            cursor = page.get("cursor")
            if not cursor:
                return items

    # Read-only calls.

    def balance(self, exchange_index: int | None = None) -> float:
        """Cash in the account. Kalshi splits cash into one pot per exchange, and an order
        can only spend the pot of its market's exchange, so pass `exchange_index` to read that pot."""
        data = self.request("GET", "/portfolio/balance")
        if exchange_index is not None and data.get("balance_breakdown") is not None:
            for pot in data["balance_breakdown"]:
                if pot.get("exchange_index") == exchange_index:
                    return float(pot.get("balance") or 0)
            return 0.0
        return price_dollars(data, "balance") or 0.0

    def positions(self) -> dict[str, float]:
        """Net contracts per market for the whole account: +YES, -NO."""
        rows = self._paged("/portfolio/positions", "market_positions", {"limit": 200})
        return {r["ticker"]: contract_count(r, "position") for r in rows if contract_count(r, "position")}

    def resting_orders(self) -> list[dict]:
        return self._paged("/portfolio/orders", "orders", {"status": "resting", "limit": 200})

    def series_category(self, series_ticker: str) -> str:
        return self.request("GET", f"/series/{series_ticker}")["series"].get("category", "")

    def series_exchange_index(self, series_ticker: str) -> int | None:
        """Which exchange (and so which cash pot) the series trades on."""
        return self.request("GET", f"/series/{series_ticker}")["series"].get("exchange_index")

    def markets(self, series_ticker: str, status: str = "open") -> list[Market]:
        rows = self._paged("/markets", "markets", {"series_ticker": series_ticker, "status": status, "limit": 200})
        return [Market.from_api(r) for r in rows]

    def market(self, ticker: str) -> Market:
        return Market.from_api(self.request("GET", f"/markets/{ticker}")["market"])

    def fills(self, order_id: str) -> list[dict]:
        return self._paged("/portfolio/fills", "fills", {"order_id": order_id, "limit": 200})

    # The only call that moves money. bot.py gates it behind check_order().

    def buy(self, ticker: str, side: str, contracts: int, price: float, client_order_id: str | None = None) -> dict:
        """Immediate-or-cancel limit buy of YES or NO: fills what it can at `price` or better, never rests.

        The V2 order endpoint quotes everything on the YES book: buying YES at p is a
        bid at p, and buying NO at q is an ask (a YES sell) at 1 - q. The bot only
        opens positions in markets where the account holds nothing, so an ask there
        always opens a NO position rather than closing a YES one.
        """
        if side not in ("yes", "no"):
            raise ValueError("side must be yes or no")
        yes_price = price if side == "yes" else 1 - price
        body = {
            "ticker": ticker,
            "side": "bid" if side == "yes" else "ask",
            "count": str(contracts),
            "price": f"{yes_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        return self.request("POST", "/portfolio/events/orders", body=body)
