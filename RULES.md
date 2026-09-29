# Trading rules

The bot's job is to grow money over the long run by making many small, short-term bets
that each have a real edge. These rules keep a bad day, a bug, or a wrong model from
doing lasting damage. The numbers live in `rules.toml`. `kalshibot/risk.py` enforces
them, and every order has to pass `check_order()` before it is sent.

## Money in and out

| Rule | Value | Why |
|---|---|---|
| Monthly budget | $100 | The most new money the bot counts per calendar month. If you deposit more, the bot ignores the extra. |
| Deposits and withdrawals | never | The bot only trades. Moving money in or out is always done by you. Its API key should have trading permission only. |
| Your own trades | untouched | The account is shared with your own trading. The bot only sells contracts it bought itself, and it tracks them in its own ledger. It never trades a market where you hold a position, because Kalshi nets YES and NO within a market and a bot order there could close yours. Its budget and loss limits count only its own trades. |
| Winnings | compound | Profits stay in the bankroll and raise future bet sizes. Long-term growth depends on this. |

## Loss limits

| Rule | Value | Why |
|---|---|---|
| Daily loss limit | $3 | A hard limit. Today's realized losses **plus the worst case of every open position** can never go over $3. Even if every open bet loses, the day cannot finish more than $3 down. |
| Monthly loss limit | $100 | If the month's realized losses reach $100, the bot stops until the 1st. This protects winnings carried over from earlier months. |
| Day boundary | midnight, America/New_York | |

**Why $3/day?** It's a cautious start while the bot proves itself. It's 3% of the monthly
budget, and a full month of losing days (up to about $90) stays under the $100 monthly
limit. The tradeoff is that the hard daily limit also caps how much can be riding at once
at $3, so only one or two bets fit at a time. Once you have a few weeks of results,
raising it to $5-10 lets profitable strategies scale.

## Bet sizing

| Rule | Value | Why |
|---|---|---|
| Kelly fraction | 0.25 | Kelly sizing gives the fastest long-term growth *if* the probability estimates are right. Quarter Kelly keeps most of that growth with much smaller swings, and it forgives estimates that are too optimistic. Those are the most likely failure. |
| Max risk per trade | $2.50 | Caps any single bet. With a $3 daily limit, one full-size bet uses most of the day's budget. |
| Minimum edge | $0.03/contract after fees | Kalshi fees are large relative to small bets. A bet that only looks good before fees is a losing bet. |

## Which markets

| Rule | Value | Why |
|---|---|---|
| Time to close | 48 hours or less | Short-term bets only. Money turns over quickly, and results arrive quickly enough to judge the strategy. |
| Price range | $0.05 to $0.95 | At extreme prices, fees and cent rounding eat most of the edge. Longshots are also where people misjudge the odds most. |
| Excluded categories | Sports | You trade sports yourself, so the bot stays out entirely. A market with no category is skipped too, so nothing gets through by accident. |
| Positions per event | 1 | Markets in the same event usually move together. Two bets there are really one bigger bet. |

## Safety switches

| Rule | Value | Why |
|---|---|---|
| Live trading | on (since 2026-09-29) | Set `live_trading = false` in `rules.toml` to stop real-money orders. |
| Kill switch | a file named `STOP` | Create it and the bot places no orders at all, buys or sells. Delete it to resume. |
| Max orders per day | 20 | Catches runaway loops and bugs. |

## Schedule

Not scheduled yet: the bot trades only when someone runs `python3 -m kalshibot run --mode live`.
Each run settles finished bets and places new ones. Commit `ledger-live.jsonl` after every
run so the bot's record survives between runs. The growth step below needs someone to run it.

## Growth step

Once, after the bot has traded live for 14 days (from 2026-09-29), the scheduled run checks
its realized P&L over those 14 days, after fees. If it is above $0:

- `daily_loss_limit` goes from $3 to $5. `max_risk_per_trade` stays at $2.50.
- The bot adds more Kalshi daily high temperature markets: any other city whose series
  settles on an NWS Daily Climate Report, priced by the same model, with tests.

If P&L is $0 or below, nothing changes and the run reports the result instead. Any growth
beyond this one step is your call, not the bot's.
