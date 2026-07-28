"""Tests for deterministic daily theme construction."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from news_collector.analysis.theme_engine import (
    build_point_in_time_themes,
    preserve_existing_theme_ids,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _article(article_id: int, day: date) -> dict[str, object]:
    body = "量子材料供應鏈包含甲公司、乙公司與丙公司。"
    title = {
        1: "新材料供應鏈啟動擴產",
        2: "三家公司共同投入新製程",
    }[article_id]
    return {
        "article_id": f"cnyes:{article_id}",
        "source_article_id": article_id,
        "title": title,
        "summary": "",
        "body_text": body,
        "published_at": datetime(day.year, day.month, day.day, 10, tzinfo=TAIPEI),
        "published_date": day,
        "included_in_analysis": True,
        "content_hash": hashlib.sha256(f"{body}{article_id}".encode()).hexdigest(),
        "story_id": f"cnyes:{article_id}",
        "is_story_representative": True,
    }


def test_theme_can_pass_without_title_mention_and_keeps_count_features() -> None:
    day = date(2026, 7, 24)
    articles = [_article(1, day), _article(2, day)]
    keywords = [
        {
            "article_id": article["article_id"],
            "keyword": "量子材料",
            "keyword_position": 0,
            "is_title_mention": False,
        }
        for article in articles
    ]
    stocks = [
        {
            "article_id": article["article_id"],
            "symbol": f"TWS:{quote_code}",
            "quote_code": quote_code,
            "stock_name": stock_name,
        }
        for article in articles
        for quote_code, stock_name in (
            ("1101", "甲公司"),
            ("1102", "乙公司"),
            ("1103", "丙公司"),
        )
    ]

    result = build_point_in_time_themes(
        articles=articles,
        keywords=keywords,
        stocks=stocks,
        analysis_dates=[day],
        trade_dates={day: date(2026, 7, 27)},
        as_of_by_date={
            day: datetime(day.year, day.month, day.day, 23, 59, tzinfo=TAIPEI)
        },
    )

    assert len(result.daily_themes) == 1
    theme = result.daily_themes[0]
    assert theme["theme_label"] == "量子材料"
    assert theme["theme_count"] == 2
    assert theme["article_count"] == 2
    assert theme["title_mention_count"] == 0
    assert theme["stock_count"] == 3
    assert theme["has_news_today"] is True
    assert theme["is_active_today"] is True
    assert theme["count_in_season"] == 1
    assert theme["consecutive_news_days"] == 1
    assert theme["days_since_last_news"] == 0

    theme_key = result.theme_catalog[-1]["theme_key"]
    preserved = preserve_existing_theme_ids(
        result,
        [{"theme_key": theme_key, "theme_id": 20_000}],
    )
    assert preserved.daily_themes[0]["theme_id"] == 20_000
    assert preserved.theme_catalog[-1]["theme_id"] == 20_000


def test_theme_stays_in_sixty_day_universe_and_carries_latest_members() -> None:
    first_day = date(2026, 7, 1)
    second_day = first_day + timedelta(days=1)
    third_day = first_day + timedelta(days=2)
    expired_day = second_day + timedelta(days=60)
    articles = [
        _article(1, first_day),
        _article(2, first_day),
        {
            **_article(1, second_day),
            "article_id": "cnyes:3",
            "source_article_id": 3,
            "story_id": "cnyes:3",
            "content_hash": hashlib.sha256(b"third story").hexdigest(),
        },
    ]
    keywords = [
        {
            "article_id": article["article_id"],
            "keyword": "量子材料",
            "keyword_position": 0,
            "is_title_mention": False,
        }
        for article in articles
    ]
    stocks = [
        {
            "article_id": article["article_id"],
            "symbol": f"TWS:{quote_code}",
            "quote_code": quote_code,
            "stock_name": stock_name,
        }
        for article in articles[:2]
        for quote_code, stock_name in (
            ("1101", "甲公司"),
            ("1102", "乙公司"),
            ("1103", "丙公司"),
        )
    ]
    analysis_dates = [first_day, second_day, third_day, expired_day]

    result = build_point_in_time_themes(
        articles=articles,
        keywords=keywords,
        stocks=stocks,
        analysis_dates=analysis_dates,
        trade_dates={day: day + timedelta(days=1) for day in analysis_dates},
        as_of_by_date={
            day: datetime(day.year, day.month, day.day, 23, 59, tzinfo=TAIPEI)
            for day in analysis_dates
        },
    )

    themes_by_day = {row["evidence_date"]: row for row in result.daily_themes}
    second = themes_by_day[second_day]
    assert second["has_news_today"] is True
    assert second["is_active_today"] is False
    assert second["article_count"] == 1
    assert second["count_in_season"] == 2
    assert second["consecutive_news_days"] == 2
    assert second["days_since_last_news"] == 0

    third = themes_by_day[third_day]
    assert third["has_news_today"] is False
    assert third["is_active_today"] is False
    assert third["article_count"] == 0
    assert third["title_mention_count"] == 0
    assert third["count_in_season"] == 2
    assert third["consecutive_news_days"] == 0
    assert third["days_since_last_news"] == 1

    third_members = [
        row for row in result.daily_members if row["evidence_date"] == third_day
    ]
    assert len(third_members) == 3
    assert {row["member_snapshot_date"] for row in third_members} == {first_day}
    assert all(row["is_carried_forward"] for row in third_members)
    assert expired_day not in themes_by_day


def test_known_theme_title_alias_is_counted_without_source_keyword() -> None:
    day = date(2026, 7, 24)
    articles = [
        {**_article(1, day), "title": "AI應用帶動三家公司擴產"},
        {**_article(2, day), "title": "企業加速導入AI"},
    ]
    stocks = [
        {
            "article_id": article["article_id"],
            "symbol": f"TWS:{quote_code}",
            "quote_code": quote_code,
            "stock_name": stock_name,
        }
        for article in articles
        for quote_code, stock_name in (
            ("1101", "甲公司"),
            ("1102", "乙公司"),
            ("1103", "丙公司"),
        )
    ]

    result = build_point_in_time_themes(
        articles=articles,
        keywords=[],
        stocks=stocks,
        analysis_dates=[day],
        trade_dates={day: day + timedelta(days=1)},
        as_of_by_date={
            day: datetime(day.year, day.month, day.day, 23, 59, tzinfo=TAIPEI)
        },
    )

    assert len(result.daily_themes) == 1
    theme = result.daily_themes[0]
    assert theme["theme_label"] == "人工智慧"
    assert theme["article_count"] == 2
    assert theme["title_mention_count"] == 2


def test_reviewed_non_group_label_is_excluded_without_removing_raw_input() -> None:
    day = date(2026, 7, 24)
    articles = [_article(1, day), _article(2, day)]
    keywords = [
        {
            "article_id": article["article_id"],
            "keyword": "獲利創高",
            "keyword_position": 0,
            "is_title_mention": False,
        }
        for article in articles
    ]
    stocks = [
        {
            "article_id": article["article_id"],
            "symbol": f"TWS:{quote_code}",
            "quote_code": quote_code,
            "stock_name": stock_name,
        }
        for article in articles
        for quote_code, stock_name in (
            ("1101", "甲公司"),
            ("1102", "乙公司"),
            ("1103", "丙公司"),
        )
    ]

    result = build_point_in_time_themes(
        articles=articles,
        keywords=keywords,
        stocks=stocks,
        analysis_dates=[day],
        trade_dates={day: day + timedelta(days=1)},
        as_of_by_date={
            day: datetime(day.year, day.month, day.day, 23, 59, tzinfo=TAIPEI)
        },
    )

    assert keywords
    assert result.daily_themes == []
    assert all(row["theme_label"] != "獲利創高" for row in result.theme_catalog)
