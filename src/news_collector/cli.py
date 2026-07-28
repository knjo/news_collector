"""Command-line interface for news collection jobs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from news_collector.cnyes_pipeline import run_backfill
from news_collector.config import Settings


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news-collector",
        description="Collect and analyze point-in-time news evidence.",
    )
    subparsers = parser.add_subparsers(dest="command")
    backfill = subparsers.add_parser(
        "cnyes-backfill",
        help="Backfill Anue Taiwan-stock news and calculate daily topic heat.",
    )
    backfill.add_argument("--days", type=int, default=60)
    backfill.add_argument(
        "--end-date",
        type=_date,
        help="Use a completed historical end date (YYYY-MM-DD); default is now.",
    )
    backfill.add_argument(
        "--output-start-date",
        type=_date,
        help="Warm up from the collected start, but publish daily tables from this date.",
    )
    backfill.add_argument("--data-dir", type=Path, default=Path("data"))
    backfill.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse existing normalized dates and crawl only missing dates.",
    )
    backfill.add_argument(
        "--delay-seconds",
        type=float,
        default=0.3,
        help="Delay between API pages.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "cnyes-backfill":
        parser.print_help()
        return

    output_dir, manifest, report = run_backfill(
        days=args.days,
        data_dir=args.data_dir,
        end_date=args.end_date,
        output_start_date=args.output_start_date,
        reuse_existing=args.reuse_existing,
        delay_seconds=args.delay_seconds,
        settings=Settings(),
    )
    print(f"data: {output_dir}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("\nTop themes:")
    for index, row in enumerate(report, start=1):
        heat = f"{row['heat_index_7d']:.0f}" if row["heat_index_7d"] is not None else "NEW"
        stocks = ", ".join(row["top_stocks"]) or "-"
        print(
            f"{index:>2}. {row['theme_label']} | status={row['theme_status']} "
            f"| stories={row['story_count']} | stocks={row['stock_count']} "
            f"| heat7d={heat} | {stocks}"
        )
