"""Tests for point-in-time broad parent and subtheme membership."""

from __future__ import annotations

from datetime import date, timedelta

from news_collector.analysis.broad_themes import (
    MIN_SUBTHEME_MEMBERS,
    build_broad_daily_members,
)


def _build_with_stock_count(stock_count: int) -> list[dict[str, object]]:
    first_day = date(2026, 7, 1)
    second_day = first_day + timedelta(days=1)
    article_themes = [
        {
            "published_date": day,
            "theme_label": "CCL",
            "story_id": f"story-{day.isoformat()}",
        }
        for day in (first_day, second_day)
    ]
    evidence = [
        {
            "evidence_date": day,
            "theme_id": 100,
            "quote_code": f"{1000 + stock_number}",
            "stock_name": f"公司{stock_number}",
            "story_id": f"story-{day.isoformat()}",
            "story_score": 2.0,
        }
        for day in (first_day, second_day)
        for stock_number in range(stock_count)
    ]
    return build_broad_daily_members(
        article_themes=article_themes,
        evidence=evidence,
        theme_catalog=[{"theme_id": 100, "theme_label": "CCL"}],
        analysis_dates=[first_day, second_day],
        trade_dates={
            first_day: second_day,
            second_day: second_day + timedelta(days=1),
        },
    )


def test_subtheme_is_split_when_at_least_five_members_qualify() -> None:
    rows = _build_with_stock_count(MIN_SUBTHEME_MEMBERS)
    second_day_rows = [row for row in rows if row["evidence_date"] == date(2026, 7, 2)]

    assert {row["theme_name"] for row in second_day_rows} == {"PCB", "CCL"}
    assert sum(row["theme_name"] == "PCB" for row in second_day_rows) == MIN_SUBTHEME_MEMBERS
    assert sum(row["theme_name"] == "CCL" for row in second_day_rows) == MIN_SUBTHEME_MEMBERS


def test_subtheme_stays_merged_with_only_four_qualified_members() -> None:
    rows = _build_with_stock_count(4)
    second_day_rows = [row for row in rows if row["evidence_date"] == date(2026, 7, 2)]

    assert {row["theme_name"] for row in second_day_rows} == {"PCB"}


def test_unmapped_formal_theme_becomes_an_independent_parent() -> None:
    first_day = date(2026, 7, 1)
    second_day = first_day + timedelta(days=1)
    rows = build_broad_daily_members(
        article_themes=[
            {
                "published_date": day,
                "theme_label": "電動車",
                "story_id": f"story-{day.isoformat()}",
            }
            for day in (first_day, second_day)
        ],
        evidence=[
            {
                "evidence_date": day,
                "theme_id": 100,
                "quote_code": "2201",
                "stock_name": "甲公司",
                "story_id": f"story-{day.isoformat()}",
                "story_score": 2.0,
            }
            for day in (first_day, second_day)
        ],
        theme_catalog=[{"theme_id": 100, "theme_label": "電動車"}],
        analysis_dates=[first_day, second_day],
        trade_dates={
            first_day: second_day,
            second_day: second_day + timedelta(days=1),
        },
    )

    assert {row["theme_name"] for row in rows} == {"電動車"}


def test_dynamic_parent_keeps_more_than_thirty_members() -> None:
    first_day = date(2026, 7, 1)
    second_day = first_day + timedelta(days=1)
    stock_count = 35
    rows = build_broad_daily_members(
        article_themes=[
            {
                "published_date": day,
                "theme_label": "電動車",
                "story_id": f"story-{day.isoformat()}",
            }
            for day in (first_day, second_day)
        ],
        evidence=[
            {
                "evidence_date": day,
                "theme_id": 100,
                "quote_code": f"{2000 + stock_number}",
                "stock_name": f"公司{stock_number}",
                "story_id": f"story-{day.isoformat()}",
                "story_score": 2.0,
            }
            for day in (first_day, second_day)
            for stock_number in range(stock_count)
        ],
        theme_catalog=[{"theme_id": 100, "theme_label": "電動車"}],
        analysis_dates=[first_day, second_day],
        trade_dates={
            first_day: second_day,
            second_day: second_day + timedelta(days=1),
        },
    )

    second_day_rows = [row for row in rows if row["evidence_date"] == second_day]
    assert len(second_day_rows) == stock_count


def test_non_industry_label_does_not_become_a_dynamic_parent() -> None:
    first_day = date(2026, 7, 1)
    second_day = first_day + timedelta(days=1)
    rows = build_broad_daily_members(
        article_themes=[
            {
                "published_date": day,
                "theme_label": "配息",
                "story_id": f"story-{day.isoformat()}",
            }
            for day in (first_day, second_day)
        ],
        evidence=[
            {
                "evidence_date": day,
                "theme_id": 100,
                "quote_code": "2201",
                "stock_name": "甲公司",
                "story_id": f"story-{day.isoformat()}",
                "story_score": 2.0,
            }
            for day in (first_day, second_day)
        ],
        theme_catalog=[{"theme_id": 100, "theme_label": "配息"}],
        analysis_dates=[first_day, second_day],
        trade_dates={
            first_day: second_day,
            second_day: second_day + timedelta(days=1),
        },
    )

    assert rows == []
