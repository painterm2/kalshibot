"""Command line: python3 -m kalshibot <command>

  check-key          Check the API key against Kalshi's demo and live exchanges (read-only).
  run [--mode M]     One pass of the weather strategy. M is dry-run (default), demo or live.
  fund AMOUNT        Record money you gave the bot this month (demo/live ledgers).
  status [--mode M]  The bot's positions and P&L from its ledger.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kalshibot import bot
from kalshibot.kalshi import BASE_URLS, KalshiClient, KalshiError
from kalshibot.rules import load_rules

# Exchange each mode reads from. Dry runs read live markets but never trade.
DEFAULT_EXCHANGE = {"dry-run": "prod", "demo": "demo", "live": "prod"}


def check_key() -> int:
    ok = False
    for env in BASE_URLS:
        try:
            balance = KalshiClient.from_env(env).balance()
            print(f"{env}: key accepted, cash balance ${balance:.2f}")
            ok = True
        except KalshiError as e:
            print(f"{env}: key rejected ({e})")
        except Exception as e:
            print(f"{env}: couldn't reach Kalshi ({type(e).__name__})")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshibot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-key")
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=bot.MODES, default="dry-run")
    run.add_argument("--exchange", choices=sorted(BASE_URLS), help="override the exchange to read from")
    fund = sub.add_parser("fund")
    fund.add_argument("amount", type=float)
    fund.add_argument("--mode", choices=("demo", "live"), default="live")
    status = sub.add_parser("status")
    status.add_argument("--mode", choices=bot.MODES, default="dry-run")
    args = parser.parse_args(argv)

    rules = load_rules()
    now = datetime.now(timezone.utc)

    if args.command == "check-key":
        return check_key()
    if args.command == "run":
        exchange = args.exchange or os.environ.get("KALSHI_ENV") or DEFAULT_EXCHANGE[args.mode]
        if args.mode == "demo" and exchange != "demo":
            parser.error("demo mode trades on the demo exchange only")
        if args.mode == "live" and exchange != "prod":
            parser.error("live mode trades on the live exchange only")
        bot.run_once(rules, KalshiClient.from_env(exchange), args.mode, now)
        return 0
    if args.command == "fund":
        month = now.astimezone(ZoneInfo(rules.timezone)).strftime("%Y-%m")
        bot.ledger_for(args.mode).fund(month, args.amount, now)
        print(f"recorded ${args.amount:.2f} for {month} in the {args.mode} ledger "
              f"(the bot counts at most ${rules.monthly_budget:.2f} a month)")
        return 0
    if args.command == "status":
        ledger = bot.ledger_for(args.mode)
        state = ledger.state(0.0, now, rules.timezone, frozenset())
        contributed = sum(min(a, rules.monthly_budget) for a in state.contributions.values())
        print(f"{args.mode}: contributed ${contributed:.2f}, realized P&L lifetime ${state.realized_pnl_lifetime:+.2f}, "
              f"month ${state.realized_pnl_month:+.2f}, today ${state.realized_pnl_today:+.2f}")
        for p in state.open_positions:
            print(f"  open {p.ticker} {p.side.upper()} x{p.contracts}, at risk ${p.max_loss:.2f}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
