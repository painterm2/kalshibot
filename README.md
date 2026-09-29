# kalshibot

A bot that trades short-term markets on Kalshi, within strict risk limits.

- `RULES.md` explains the trading rules. `rules.toml` holds their values.
- `kalshibot/risk.py` checks every order against the rules and sizes bets.
- `kalshibot/kalshi.py` talks to Kalshi's API, signing requests with your key.
- `kalshibot/weather.py` prices the daily high temperature markets from NWS forecasts.
- `kalshibot/bot.py` runs one pass: settle finished bets, find edges, check, trade.
- `kalshibot/ledger.py` is the bot's own record of its trades, kept apart from yours.

## Setup

    pip install -r requirements.txt
    export KALSHI_KEY_ID=...        # the key's id
    export KALSHI_PRIVATE_KEY=...   # the PEM contents

The bot needs to reach `api.elections.kalshi.com` (live), `demo-api.kalshi.co` (demo)
and `api.weather.gov` (forecasts).

## Running

    python3 -m kalshibot check-key          # is the key accepted? (read-only)
    python3 -m kalshibot run                 # dry run: real markets, paper trades, no orders
    python3 -m kalshibot status              # the paper ledger's positions and P&L
    python3 -m kalshibot run --mode demo     # real orders on the demo exchange
    python3 -m kalshibot fund 100            # record money you gave the bot (live ledger)
    python3 -m kalshibot run --mode live     # real money; refused until live_trading = true

A dry run writes what it would have bought to `ledger-dry-run.jsonl` and settles those
paper bets against real results on later runs, so a few days of dry runs give a track
record before any money is at risk. Each mode keeps its own ledger.

Run the tests with `python3 -m unittest`. They need Python 3.11+ and `requirements.txt`.
