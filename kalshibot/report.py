"""Weekly performance report, built from the bot's ledger as Markdown."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from kalshibot import weather
from kalshibot.rules import Rules

LIVE_SINCE = date(2026, 9, 29)
GROWTH_CHECK_DAYS = 14

_CITY = {s.series: s.name for s in weather.STATIONS}


def _city(ticker: str) -> str:
    series = ticker.split("-")[0]
    return _CITY.get(series, series)


def _money(x: float) -> str:
    return f"-${-x:.2f}" if x < 0 else f"${x:.2f}"


def _signed(x: float) -> str:
    return f"+${x:.2f}" if x >= 0 else f"-${-x:.2f}"


def build_report(entries: list[dict], rules: Rules, now: datetime, days: int = 7,
                 cash_balance: float | None = None) -> str:
    tz = ZoneInfo(rules.timezone)
    local_now = now.astimezone(tz)
    start = local_now - timedelta(days=days)

    def when(e: dict) -> datetime:
        return datetime.fromisoformat(e["ts"]).astimezone(tz)

    # Each settlement closes the bot's buys in that market and side; pair them up.
    buys_open: dict[tuple, list[dict]] = defaultdict(list)
    settled: list[dict] = []
    for e in entries:
        key = (e.get("ticker"), e.get("side"))
        if e["type"] == "buy" and e["contracts"] > 0:
            buys_open[key].append(e)
        elif e["type"] == "settle":
            buys = buys_open.pop(key, [])
            settled.append({**e, "buys": buys, "when": when(e)})
    open_buys = [b for bs in buys_open.values() for b in bs]

    week = [s for s in settled if s["when"] >= start]
    week_pnl = sum(s["pnl"] for s in week)
    life_pnl = sum(s["pnl"] for s in settled)
    wins = sum(1 for s in week if s["payout"] > 0)
    staked = sum(e["cost"] for e in entries if e["type"] == "buy" and when(e) >= start)
    week_risked = sum(b["cost"] for s in week for b in s["buys"])

    lines = [
        f"# kalshibot weekly report: {start.date():%b %d} to {local_now.date():%b %d, %Y}",
        "",
        "## Summary",
        "",
        "| | |",
        "|---|---|",
        f"| P&L this week (settled, after fees) | **{_signed(week_pnl)}** |",
        f"| P&L since going live | {_signed(life_pnl)} |",
        f"| Bets settled this week | {len(week)} ({wins} won, {len(week) - wins} lost) |",
        f"| Return on money risked this week | "
        + (f"{week_pnl / week_risked:+.0%} |" if week_risked else "n/a |"),
        f"| Money put into new bets this week | {_money(staked)} |",
        f"| Open bets | {len(open_buys)}, {_money(sum(b['cost'] for b in open_buys))} at risk |",
    ]
    if cash_balance is not None:
        lines.append(f"| Account cash now (shared with you) | {_money(cash_balance)} |")

    # Does the model's confidence match reality?
    scored = [(b["win_probability"], s["payout"] > 0) for s in settled for b in s["buys"] if "win_probability" in b]
    lines += ["", "## Is the model right?", ""]
    if scored:
        expected = sum(p for p, _ in scored) / len(scored)
        actual = sum(won for _, won in scored) / len(scored)
        lines.append(f"Since going live, the bot expected to win **{expected:.0%}** of its bets and won "
                     f"**{actual:.0%}** ({len(scored)} bets).")
        if len(scored) < 30:
            lines.append("That's too few bets to judge yet; a real read needs roughly 30 or more.")
        elif actual < expected - 0.10:
            lines.append("The bot is winning noticeably less often than it expects: its forecasts are "
                         "overconfident. Consider widening the model's error bands or pausing it.")
        else:
            lines.append("Its confidence is roughly in line with results.")
    else:
        lines.append("No settled bets with recorded odds yet.")

    by_city: dict[str, list[float]] = defaultdict(list)
    for s in week:
        by_city[_city(s["ticker"])].append(s["pnl"])
    if by_city:
        lines += ["", "## By city this week", "", "| City | Bets | P&L |", "|---|---|---|"]
        for city, pnls in sorted(by_city.items(), key=lambda kv: sum(kv[1])):
            lines.append(f"| {city} | {len(pnls)} | {_signed(sum(pnls))} |")

    if week:
        lines += ["", "## Settled this week", "", "| Settled | Market | Bet | Cost | Result | P&L |",
                  "|---|---|---|---|---|---|"]
        for s in sorted(week, key=lambda s: s["when"]):
            cost = sum(b["cost"] for b in s["buys"])
            result = "won" if s["payout"] > 0 else "lost"
            lines.append(f"| {s['when']:%a %b %d} | {s['ticker']} | {s['side'].upper()} x{s['contracts']} | "
                         f"{_money(cost)} | {result} | {_signed(s['pnl'])} |")

    if open_buys:
        lines += ["", "## Open bets", "", "| Placed | Market | Bet | Cost |", "|---|---|---|---|"]
        for b in sorted(open_buys, key=when):
            lines.append(f"| {when(b):%a %b %d} | {b['ticker']} | {b['side'].upper()} x{b['contracts']} | "
                         f"{_money(b['cost'])} |")

    # Progress toward the one-time growth step in RULES.md.
    check_day = LIVE_SINCE + timedelta(days=GROWTH_CHECK_DAYS)
    growth_pnl = sum(s["pnl"] for s in settled if LIVE_SINCE <= s["when"].date() < check_day)
    lines += ["", "## Growth step", ""]
    if local_now.date() < check_day:
        lines.append(f"On {check_day:%b %d}, if the first 14 days are profitable, the daily limit goes from "
                     f"$3 to $5 and more cities are added. 14-day P&L so far: {_signed(growth_pnl)}.")
    else:
        lines.append(f"The 14-day check window closed on {check_day:%b %d} with P&L {_signed(growth_pnl)}. "
                     "See RULES.md for what changed.")
    lines.append("")
    return "\n".join(lines)
