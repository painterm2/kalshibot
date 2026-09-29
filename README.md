# kalshibot

A bot that trades short-term markets on Kalshi, within strict risk limits.

- `RULES.md` explains the trading rules. `rules.toml` holds their values.
- `kalshibot/risk.py` checks every order against the rules and sizes bets.

Run the tests with `python3 -m unittest`. They need Python 3.11+ and no other dependencies.
