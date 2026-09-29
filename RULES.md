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
| Winnings | compound | Profits stay in the bankroll and raise future bet sizes. Long-term growth depends on this. |

## Loss limits

| Rule | Value | Why |
|---|---|---|
| Daily loss limit | $5 | A hard limit. Today's realized losses **plus the worst case of every open position** can never go over $5. Even if every open bet loses, the day cannot finish more than $5 down. |
| Monthly loss limit | $100 | If the month's realized losses reach $100, the bot stops until the 1st. This protects winnings carried over from earlier months. |
| Day boundary | midnight, America/New_York | |

**Is $5/day right?** It's a sensible place to start. It's 5% of the monthly budget, and
at $5 a day, a full month of losing days (up to about $150) would still hit the $100
monthly limit first. The tradeoff is that the hard daily limit also caps how much can be
riding at once at $5. That's fine while the bot proves itself. Once you have a few weeks
of results, raising it to about $10 lets profitable strategies scale.

## Bet sizing

| Rule | Value | Why |
|---|---|---|
| Kelly fraction | 0.25 | Kelly sizing gives the fastest long-term growth *if* the probability estimates are right. Quarter Kelly keeps most of that growth with much smaller swings, and it forgives estimates that are too optimistic. Those are the most likely failure. |
| Max risk per trade | $2 | No single bet can use up most of a day's loss budget. |
| Minimum edge | $0.03/contract after fees | Kalshi fees are large relative to small bets. A bet that only looks good before fees is a losing bet. |

## Which markets

| Rule | Value | Why |
|---|---|---|
| Time to close | 48 hours or less | Short-term bets only. Money turns over quickly, and results arrive quickly enough to judge the strategy. |
| Price range | $0.05 to $0.95 | At extreme prices, fees and cent rounding eat most of the edge. Longshots are also where people misjudge the odds most. |
| Positions per event | 1 | Markets in the same event usually move together. Two bets there are really one bigger bet. |

## Safety switches

| Rule | Value | Why |
|---|---|---|
| Live trading | off | The bot runs on Kalshi's demo environment until you set `live_trading = true`. |
| Kill switch | a file named `STOP` | Create it and the bot places no new orders. Delete it to resume. |
| Max orders per day | 20 | Catches runaway loops and bugs. |
