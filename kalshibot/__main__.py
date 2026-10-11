"""Command line: python3 -m kalshibot <command>

  check-key          Check the API key against Kalshi's demo and live exchanges (read-only).
  run [--mode M]     One pass of the weather strategy. M is dry-run (default), demo or live.
  commodities [--mode M] [--loop-minutes N] [--every S] [--background]
                     Gold and silver: one pass, or passes every S seconds for N minutes.
                     --background detaches the loop (log in logs/, at most one loop per mode).
  fund AMOUNT        Record money you gave the bot this month (demo/live ledgers).
  status [--mode M]  The bot's positions and P&L from its ledger.
  report [--days N]  Performance report (Markdown) from the live ledger; --out saves it to a file.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pathlib import Path

from kalshibot import bot, commodities, report
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


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def loop_pid_file(mode: str) -> Path:
    return LOG_DIR / f"commodities-{mode}.pid"


def loop_running(mode: str) -> int | None:
    """PID of the running commodities loop for this mode, or None."""
    try:
        pid = int(loop_pid_file(mode).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


class loop_lock:
    """At most one commodities loop per mode: a second one would double every bet."""

    def __init__(self, mode: str):
        self.mode = mode
        self.ok = False

    def __enter__(self) -> bool:
        LOG_DIR.mkdir(exist_ok=True)
        path = loop_pid_file(self.mode)
        if path.exists() and not loop_running(self.mode):
            path.unlink(missing_ok=True)  # left over from a loop that died
        try:  # O_EXCL: if two loops start at once, only one gets the file
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        self.ok = True
        return True

    def __exit__(self, *exc) -> None:
        if self.ok and loop_running(self.mode) == os.getpid():
            loop_pid_file(self.mode).unlink(missing_ok=True)


def start_background_loop(mode: str) -> int:
    """Re-run this command without --background, detached, logging to logs/commodities-<mode>.log."""
    import subprocess
    import sys
    pid = loop_running(mode)
    if pid:
        print(f"a {mode} commodities loop is already running (pid {pid}); not starting another")
        return 0
    LOG_DIR.mkdir(exist_ok=True)
    argv = [a for a in sys.argv[1:] if a != "--background"]
    log = open(LOG_DIR / f"commodities-{mode}.log", "a")
    proc = subprocess.Popen([sys.executable, "-m", "kalshibot", *argv], stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            cwd=Path(__file__).resolve().parent.parent)
    print(f"started {mode} commodities loop in the background (pid {proc.pid}); log: logs/commodities-{mode}.log "
          "(it exits at once if another loop got there first)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshibot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-key")
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=bot.MODES, default="dry-run")
    run.add_argument("--exchange", choices=sorted(BASE_URLS), help="override the exchange to read from")
    com = sub.add_parser("commodities")
    com.add_argument("--mode", choices=bot.MODES, default="dry-run")
    com.add_argument("--loop-minutes", type=float, default=0)
    com.add_argument("--every", type=float, default=60)
    com.add_argument("--background", action="store_true")
    fund = sub.add_parser("fund")
    fund.add_argument("amount", type=float)
    fund.add_argument("--mode", choices=("demo", "live"), default="live")
    fund.add_argument("--strategy", choices=bot.STRATEGIES, default="weather")
    status = sub.add_parser("status")
    status.add_argument("--mode", choices=bot.MODES, default="dry-run")
    status.add_argument("--strategy", choices=bot.STRATEGIES, default="weather")
    rep = sub.add_parser("report")
    rep.add_argument("--mode", choices=bot.MODES, default="live")
    rep.add_argument("--days", type=int, default=7)
    rep.add_argument("--out", type=Path, help="also write the report to this file")
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
    if args.command == "commodities":
        crules = load_rules(strategy="commodities")
        exchange = os.environ.get("KALSHI_ENV") or DEFAULT_EXCHANGE[args.mode]
        client = KalshiClient.from_env(exchange)
        if args.loop_minutes > 0:
            if args.background:
                return start_background_loop(args.mode)
            with loop_lock(args.mode) as ok:
                if not ok:
                    print(f"a {args.mode} commodities loop is already running; not starting another")
                    return 0
                commodities.run_loop(crules, client, args.mode, args.loop_minutes, args.every,
                                     log=lambda *a: print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a,
                                                          flush=True))
        else:
            commodities.run_once(crules, client, args.mode, now)
        return 0
    if args.command == "fund":
        month = now.astimezone(ZoneInfo(rules.timezone)).strftime("%Y-%m")
        bot.ledger_for(args.mode, strategy=args.strategy).fund(month, args.amount, now)
        print(f"recorded ${args.amount:.2f} for {month} in the {args.mode} ledger "
              f"(the bot counts at most ${rules.monthly_budget:.2f} a month)")
        return 0
    if args.command == "status":
        ledger = bot.ledger_for(args.mode, strategy=args.strategy)
        state = ledger.state(0.0, now, rules.timezone, frozenset())
        contributed = sum(min(a, rules.monthly_budget) for a in state.contributions.values())
        print(f"{args.strategy} {args.mode}: contributed ${contributed:.2f}, realized P&L lifetime ${state.realized_pnl_lifetime:+.2f}, "
              f"month ${state.realized_pnl_month:+.2f}, today ${state.realized_pnl_today:+.2f}")
        for p in state.open_positions:
            print(f"  open {p.ticker} {p.side.upper()} x{p.contracts}, at risk ${p.max_loss:.2f}")
        return 0
    if args.command == "report":
        try:
            cash = KalshiClient.from_env("prod").balance() if args.mode == "live" else None
        except Exception:
            cash = None
        text = report.build_report(bot.ledger_for(args.mode).entries(), rules, now, args.days, cash)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        print(text)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
