#!/usr/bin/env python3
"""Twitter analytics utility CLI.

Provides:
- eval_history raw_metrics backfill to canonical schema
- daily time-series reports for followers/replies/posts
- optional terminal TUI dashboard
- csv/json exports for pandas workflows
"""

from __future__ import annotations

import argparse
import csv
import curses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import backfill_eval_metrics, get_conn, get_daily_analytics_series
from db import ANALYTICS_HANDLE, get_account_stats_snapshot_handles


def _fmt_int(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value)}"
    except (TypeError, ValueError):
        return "-"


def _print_table(rows: list[dict], out=sys.stdout) -> None:
    cols = [
        ("date", 10),
        ("followers", 9),
        ("f_growth", 8),
        ("replies", 7),
        ("likes", 5),
        ("eng_total", 9),
        ("orig_posts", 10),
        ("posts_total", 10),
    ]
    header = " ".join(name.ljust(width) for name, width in cols)
    print(header, file=out)
    print("-" * len(header), file=out)
    for r in rows:
        vals = [
            str(r.get("date", "")),
            _fmt_int(r.get("followers")),
            _fmt_int(r.get("follower_growth")),
            _fmt_int(r.get("replies")),
            _fmt_int(r.get("likes_only")),
            _fmt_int(r.get("engagements_total")),
            _fmt_int(r.get("original_posts")),
            _fmt_int(r.get("posts_total")),
        ]
        print(
            " ".join(v.ljust(width) for v, (_, width) in zip(vals, cols)),
            file=out,
        )


def _to_csv(rows: list[dict], out=sys.stdout) -> None:
    if not rows:
        return
    cols = [
        "date",
        "followers",
        "follower_growth",
        "engagements_total",
        "replies",
        "likes_only",
        "posts_total",
        "original_posts",
        "thread_roots",
        "thread_replies",
    ]
    writer = csv.DictWriter(out, fieldnames=cols)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in cols})


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {}
    total_replies = sum(int(r.get("replies") or 0) for r in rows)
    total_likes = sum(int(r.get("likes_only") or 0) for r in rows)
    total_posts = sum(int(r.get("posts_total") or 0) for r in rows)
    followers = [r.get("followers") for r in rows if r.get("followers") is not None]
    follower_start = followers[0] if followers else None
    follower_end = followers[-1] if followers else None
    follower_delta_window = (
        int(follower_end) - int(follower_start)
        if follower_start is not None and follower_end is not None
        else None
    )
    prev_followers = followers[-2] if len(followers) >= 2 else None
    follower_delta_24h = (
        int(follower_end) - int(prev_followers)
        if follower_end is not None and prev_followers is not None
        else None
    )
    return {
        "days": len(rows),
        "total_replies": total_replies,
        "total_likes_only": total_likes,
        "total_posts": total_posts,
        "follower_start": follower_start,
        "follower_end": follower_end,
        "follower_delta_window": follower_delta_window,
        "follower_delta_24h": follower_delta_24h,
    }


def cmd_backfill(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        result = backfill_eval_metrics(conn, dry_run=args.dry_run, limit=args.limit)
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(
        f"[{mode}] scanned={result['scanned']} updated={result['updated']}",
        flush=True,
    )
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        handles = get_account_stats_snapshot_handles(conn, limit=5)
        if handles and ANALYTICS_HANDLE not in handles:
            print(
                "WARNING: analytics handle "
                f"'{ANALYTICS_HANDLE}' has no snapshots. Available handles: {', '.join(handles)}. "
                "Set TWITTER_ACCOUNT_USERNAME correctly.",
                file=sys.stderr,
                flush=True,
            )
        rows = get_daily_analytics_series(conn, days=args.days)

    if args.last and args.last > 0:
        rows = rows[-args.last:]

    if args.format == "json":
        print(json.dumps(rows, indent=2))
    elif args.format == "csv":
        _to_csv(rows)
    else:
        summary = _summary(rows)
        if summary:
            print(
                "Summary: "
                f"days={summary['days']} "
                f"followers {summary['follower_start']} -> {summary['follower_end']} "
                f"(24h_delta={summary['follower_delta_24h']}, window_delta={summary['follower_delta_window']}) "
                f"replies={summary['total_replies']} likes_only={summary['total_likes_only']} posts={summary['total_posts']}",
                flush=True,
            )
            print("", flush=True)
        _print_table(rows)
    return 0


def _draw_tui(stdscr, rows: list[dict], days: int) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = f"Twitter Analytics TUI (last {days}d) - q to quit, r to refresh"
        stdscr.addnstr(0, 0, title, max(1, w - 1))

        s = _summary(rows)
        line1 = (
            f"Followers: {_fmt_int(s.get('follower_end'))} "
            f"(24h Δ={_fmt_int(s.get('follower_delta_24h'))}, "
            f"window Δ={_fmt_int(s.get('follower_delta_window'))})"
        )
        line2 = (
            f"Replies: {_fmt_int(s.get('total_replies'))}   "
            f"Likes-only: {_fmt_int(s.get('total_likes_only'))}   "
            f"Posts: {_fmt_int(s.get('total_posts'))}"
        )
        stdscr.addnstr(2, 0, line1, max(1, w - 1))
        stdscr.addnstr(3, 0, line2, max(1, w - 1))

        # Show trailing rows that fit
        visible = max(1, h - 7)
        tail = rows[-visible:]
        header = "date       followers f_grow replies likes eng_total orig_posts posts_total"
        stdscr.addnstr(5, 0, header, max(1, w - 1))
        y = 6
        for r in tail:
            line = (
                f"{r['date']:<10} "
                f"{_fmt_int(r.get('followers')):<9} "
                f"{_fmt_int(r.get('follower_growth')):<6} "
                f"{_fmt_int(r.get('replies')):<7} "
                f"{_fmt_int(r.get('likes_only')):<5} "
                f"{_fmt_int(r.get('engagements_total')):<9} "
                f"{_fmt_int(r.get('original_posts')):<10} "
                f"{_fmt_int(r.get('posts_total')):<10}"
            )
            if y < h:
                stdscr.addnstr(y, 0, line, max(1, w - 1))
            y += 1

        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("r"), ord("R")):
            with get_conn() as conn:
                rows = get_daily_analytics_series(conn, days=days)
        time.sleep(0.1)


def cmd_tui(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        handles = get_account_stats_snapshot_handles(conn, limit=5)
        if handles and ANALYTICS_HANDLE not in handles:
            print(
                "WARNING: analytics handle "
                f"'{ANALYTICS_HANDLE}' has no snapshots. Available handles: {', '.join(handles)}. "
                "Set TWITTER_ACCOUNT_USERNAME correctly.",
                file=sys.stderr,
                flush=True,
            )
        rows = get_daily_analytics_series(conn, days=args.days)
    curses.wrapper(_draw_tui, rows, args.days)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Twitter analytics + eval metric maintenance CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python analytics_cli.py backfill-evals --dry-run\n"
            "  uv run python analytics_cli.py backfill-evals\n"
            "  uv run python analytics_cli.py daily --days 60 --format table\n"
            "  uv run python analytics_cli.py daily --days 90 --format csv > twitter_daily.csv\n"
            "  uv run python analytics_cli.py daily --days 90 --format json > twitter_daily.json\n"
            "  uv run python analytics_cli.py tui --days 30\n"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill-evals", help="Backfill eval_history.raw_metrics to canonical schema")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p_backfill.add_argument("--limit", type=int, default=None, help="Process only N most recent rows")
    p_backfill.set_defaults(func=cmd_backfill)

    p_daily = sub.add_parser("daily", help="Show per-day followers/replies/posts analytics")
    p_daily.add_argument("--days", type=int, default=30, help="Lookback window in days")
    p_daily.add_argument("--last", type=int, default=0, help="Show only last N rows")
    p_daily.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="Output format (csv/json are pandas-friendly)",
    )
    p_daily.set_defaults(func=cmd_daily)

    p_tui = sub.add_parser("tui", help="Interactive terminal dashboard (curses)")
    p_tui.add_argument("--days", type=int, default=30, help="Lookback window in days")
    p_tui.set_defaults(func=cmd_tui)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
