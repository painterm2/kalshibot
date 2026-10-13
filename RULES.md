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
| Cash pots | moved as needed | Kalshi keeps your cash in one pot per exchange (weather is "Default", commodities "Crypto & Commodities", sports "Tennis, Baseball, Basketball"), and an order can only spend its market's pot. The Kalshi app moves cash between pots for you; the API doesn't. So before each order the bot moves just that order's cost into the market's pot from whichever pot has cash. You approved this on 2026-10-11. It never moves money in or out of the account, every order still passes the daily and total loss limits first, and if a transfer doesn't land exactly, the run stops before ordering. |
| Your own trades | untouched | The account is shared with your own trading. The bot only sells contracts it bought itself, and it tracks them in its own ledger. It never trades a market where you hold a position, because Kalshi nets YES and NO within a market and a bot order there could close yours. Its budget and loss limits count only its own trades. |
| Winnings | compound | Profits stay in the bankroll and raise future bet sizes. Long-term growth depends on this. |

## Loss limits

| Rule | Value | Why |
|---|---|---|
| Daily loss limit | $8 (was $3 at launch, $5 after the growth step) | A hard limit. Today's realized losses **plus the worst case of every open position** can never go over $8. Even if every open bet loses, the day cannot finish more than $8 down. |
| Monthly loss limit | $100 | If the month's realized losses reach $100, the bot stops until the 1st. This protects winnings carried over from earlier months. |
| Day boundary | midnight, America/New_York | |

**Why $8/day?** The bot started at $3 while it proved itself, the growth step raised it
to $5 after a profitable first 14 days, and you raised it to $8 on 2026-10-13. The hard daily limit also caps how much can be riding
at once, so two or three bets fit at a time. The monthly $100 limit still applies, and so
does the -$10 total loss stop.

## Bet sizing

| Rule | Value | Why |
|---|---|---|
| Kelly fraction | 0.25 | Kelly sizing gives the fastest long-term growth *if* the probability estimates are right. Quarter Kelly keeps most of that growth with much smaller swings, and it forgives estimates that are too optimistic. Those are the most likely failure. |
| Max risk per trade | $4.00 | Caps any single bet, so the extra daily room can go to bigger bets as well as more cities. Two full-size bets fit in the $8 daily limit. Raised from $2.50 on 2026-10-13 with the $8 limit. |
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
| Total loss stop | -$10 | Your rule: if the bot's total P&L across all strategies ever reaches -$10, everything stops. Orders are refused if every open bet losing would take the total below -$10, and once realized P&L hits -$10 the bot creates `STOP`. Only you can delete it. |

## Schedule

The "kalshibot live run (3x daily, runner)" routine wakes the "kalshibot runner" session at
9:47am, 1:47pm and 5:47pm New York time. Each run settles finished bets, places new ones,
checks the account's positions against the bot's ledger (creating `STOP` on any mismatch),
and commits `ledger-live.jsonl` so the bot's record survives between runs. Pause it in
claude.ai under Routines, or stop trading with `STOP` / `live_trading = false`.

## Sharing the cash with you

You can use the account's cash for your own trading whenever you like. The bot counts at
most $100 a month as its own, and never bets more than the cash actually in the account,
so if you spend some, it just has less to work with. With the $8 daily limit it rarely
has more than a few dollars tied up at once.

## Growth step

Once, after the bot has traded live for 14 days (from 2026-09-29), the scheduled run checks
its realized P&L over those 14 days, after fees. If it is above $0:

- `daily_loss_limit` goes from $3 to $5. `max_risk_per_trade` stays at $2.50.
- The bot adds more Kalshi daily high temperature markets: any other city whose series
  settles on an NWS Daily Climate Report, priced by the same model, with tests.

If P&L is $0 or below, nothing changes and the run reports the result instead. Any growth
beyond this one step is your call, not the bot's.

Done 2026-10-13: 14-day P&L was +$5.67 (18 settled bets), so the daily loss limit is now $5
and 17 more cities were added: Atlanta, Boston, Dallas/Fort Worth, Washington DC, Newark,
Houston (Hobby), Las Vegas, Minneapolis, New Orleans, Oklahoma City, Phoenix, San Diego,
San Antonio, Louisville, Seattle, San Francisco and Trenton. Each one's market rules name
the station's NWS Daily Climate Report (CLI code), the same wording as the original seven
cities, checked against the Kalshi API that day.

## Commodities (gold and silver)

Added 2026-10-11 at your request. `kalshibot/commodities.py` trades Kalshi's gold and
silver price markets (15-minute, hourly and daily), checking every minute while metals
markets are open. Its settings are the `[commodities]` section of `rules.toml`;
everything not listed there follows the rules above.

| Rule | Value | Why |
|---|---|---|
| Daily loss limit | $5 | Your number. Separate from weather's $8, so the worst day for both together is $13, though the -$10 total loss stop still caps everything. |
| Max risk per trade | $1.00 | Small bets, many of them, so no single 15-minute coin flip matters much. |
| Max orders per day | 60 | The 15-minute markets turn over all day. |
| Total loss stop | -$10 | Shared with weather: the two strategies' P&L is added together. |
| Live trading | **on** (since 2026-10-11) | You switched it to real money before any paper results. The limits above still hold. Set `live_trading = false` under `[commodities]` in `rules.toml` to go back to paper. |
| Price source | Pyth spot gold and silver | Your Pyth key covers spot gold and silver only. Kalshi settles on Pyth's 24/7 index prices, which the key can't read, so the bot measures the gap from each 15-minute market's starting price. |
| Hours | metals market hours | Outside them Pyth's spot price stops updating and the bot sits out. |
| Last minutes | skipped | It doesn't trade a market in its final 3 minutes, when traders watching every tick know more. |
