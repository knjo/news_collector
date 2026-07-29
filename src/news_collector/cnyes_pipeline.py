"""End-to-end Cnyes backfill pipeline with point-in-time theme snapshots."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from news_collector.analysis.broad_themes import (
    BROAD_DAILY_SCHEMA,
    BROAD_RULE_VERSION,
    BROAD_SNAPSHOT_VERSION,
    BROAD_THEMES,
    SUBTHEMES,
    broad_theme_definitions,
    build_broad_daily_members,
)
from news_collector.analysis.theme_engine import (
    ARTICLE_THEME_SCHEMA,
    CATALOG_VERSION,
    DAILY_MEMBER_SCHEMA,
    DAILY_RELATION_SCHEMA,
    DAILY_THEME_SCHEMA,
    EVIDENCE_SCHEMA,
    RULE_VERSION,
    SNAPSHOT_VERSION,
    TAXONOMY_VERSION,
    THEME_ALIAS_SCHEMA,
    THEME_CATALOG_SCHEMA,
    ThemeBuildResult,
    build_point_in_time_themes,
    preserve_existing_theme_ids,
)
from news_collector.analysis.topics import dataframe
from news_collector.collectors.cnyes import TAIPEI, CnyesClient, normalize_item, taipei_day_bounds
from news_collector.config import Settings

TWSE_HOLIDAY_URL = "https://www.twse.com.tw/holidaySchedule/holidaySchedule"

ARTICLE_SCHEMA = {
    "article_id": pl.String,
    "source_article_id": pl.Int64,
    "canonical_url": pl.String,
    "source": pl.String,
    "content_provider": pl.String,
    "category_id": pl.Int64,
    "category_name": pl.String,
    "title": pl.String,
    "summary": pl.String,
    "body_text": pl.String,
    "published_at": pl.Datetime("us", "Asia/Taipei"),
    "published_date": pl.Date,
    "fetched_at": pl.Datetime("us", "Asia/Taipei"),
    "availability_grade": pl.String,
    "article_type": pl.String,
    "is_outsource": pl.Boolean,
    "included_in_analysis": pl.Boolean,
    "content_hash": pl.String,
    "raw_response_file": pl.String,
    "story_id": pl.String,
    "is_story_representative": pl.Boolean,
}
KEYWORD_SCHEMA = {
    "article_id": pl.String,
    "keyword": pl.String,
    "keyword_position": pl.Int64,
    "is_title_mention": pl.Boolean,
}
STOCK_SCHEMA = {
    "article_id": pl.String,
    "symbol": pl.String,
    "exchange": pl.String,
    "quote_code": pl.String,
    "stock_name": pl.String,
}


def _write_parquet(
    path: Path,
    records: list[dict[str, Any]],
    schema: dict[str, pl.DataType],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe(records, schema).write_parquet(path, compression="zstd", statistics=True)


def _replace_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    if target.exists():
        target.rename(backup)
    try:
        source.rename(target)
    except Exception:
        if backup.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_broad_universe_parquet(
    *,
    output_root: Path,
    completed_end_date: date,
) -> tuple[Path, int, date | None]:
    """Publish one compact Parquet file from all completed broad daily partitions."""
    source_pattern = str(
        output_root / "broad_daily" / "evidence_date=*" / "daily_broad_theme_members.parquet"
    )
    frame = (
        pl.scan_parquet(source_pattern)
        .filter(pl.col("evidence_date") <= completed_end_date)
        .sort(
            ["evidence_date", "theme_id", "membership_score", "quote_code"],
            descending=[False, False, True, False],
        )
        .collect()
    )
    output_path = output_root / "theme_universe.parquet"
    temporary = output_path.with_name(f".{output_path.name}.tmp-{uuid.uuid4().hex}")
    frame.write_parquet(
        temporary,
        compression="zstd",
        compression_level=9,
        statistics=True,
    )
    temporary.replace(output_path)
    legacy_csv = output_root / "theme_universe.csv"
    if legacy_csv.exists():
        legacy_csv.unlink()
    first_date = frame.select(pl.col("evidence_date").min()).item() if frame.height else None
    return output_path, frame.height, first_date


def _partition_records(
    records: list[dict[str, Any]],
    date_field: str,
) -> dict[date, list[dict[str, Any]]]:
    partitions: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        partitions[row[date_field]].append(row)
    return partitions


def _twse_calendar(
    *,
    client: httpx.Client,
    evidence_dates: list[date],
) -> tuple[dict[date, date], list[dict[str, Any]]]:
    years = sorted({day.year for day in evidence_dates})
    records: list[dict[str, Any]] = []
    for year in years:
        response = client.get(
            TWSE_HOLIDAY_URL,
            params={"response": "json", "date": year},
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("queryYear", 0)) != year:
            raise RuntimeError(f"TWSE returned the wrong holiday year for {year}")
        records.extend(
            {
                "Date": values[0],
                "Name": values[1],
                "Description": values[2],
            }
            for values in payload.get("data", [])
        )

    closed_dates: set[date] = set()
    for record in records:
        holiday_date = date.fromisoformat(record["Date"])
        text = f"{record.get('Name', '')} {record.get('Description', '')}"
        if any(marker in text for marker in ("放假", "補假", "休市", "無交易", "不交易")):
            closed_dates.add(holiday_date)

    result: dict[date, date] = {}
    for evidence_date in evidence_dates:
        trade_date = evidence_date + timedelta(days=1)
        while trade_date.weekday() >= 5 or trade_date in closed_dates:
            trade_date += timedelta(days=1)
        result[evidence_date] = trade_date
    return result, records


def _report_data(
    result: ThemeBuildResult,
    *,
    output_start_date: date,
    output_end_date: date,
    fetched_at: datetime,
    historical_end_date: date | None,
) -> tuple[date, list[dict[str, Any]], list[dict[str, Any]]]:
    labels = {row["theme_id"]: row["theme_label"] for row in result.theme_catalog}
    members_by_day_theme: dict[tuple[date, int], list[dict[str, Any]]] = defaultdict(list)
    for row in result.daily_members:
        members_by_day_theme[(row["evidence_date"], row["theme_id"])].append(row)

    complete_dates = sorted(
        {
            row["evidence_date"]
            for row in result.daily_themes
            if output_start_date <= row["evidence_date"] <= output_end_date
            and row["source_count"] >= 20
            and (row["evidence_date"] < fetched_at.date() or historical_end_date is not None)
        }
    )
    report_date = complete_dates[-1] if complete_dates else output_end_date
    latest = []
    for row in result.daily_themes:
        if row["evidence_date"] != report_date:
            continue
        members = sorted(
            members_by_day_theme[(report_date, row["theme_id"])],
            key=lambda member: member["member_rank"],
        )
        latest.append(
            {
                "theme_id": row["theme_id"],
                "theme_label": labels[row["theme_id"]],
                "theme_status": row["theme_status"],
                "story_count": row["theme_count"],
                "stock_count": row["stock_count"],
                "theme_share": row["theme_share"],
                "heat_index_7d": row["heat_index_7d"],
                "heat_status": row["heat_status"],
                "top_stocks": [
                    f"{member['stock_name']}({member['quote_code']})" for member in members[:5]
                ],
            }
        )
    latest.sort(key=lambda row: (-row["story_count"], row["theme_id"]))

    totals: dict[int, dict[str, Any]] = {}
    for row in result.daily_themes:
        if not output_start_date <= row["evidence_date"] <= output_end_date:
            continue
        if not row["is_active_today"]:
            continue
        value = totals.setdefault(
            row["theme_id"],
            {"stories": 0, "dates": set(), "stocks": set()},
        )
        value["stories"] += row["theme_count"]
        value["dates"].add(row["evidence_date"])
    member_scores: dict[tuple[int, str, str], float] = defaultdict(float)
    for row in result.daily_members:
        if not output_start_date <= row["evidence_date"] <= output_end_date:
            continue
        if row["is_carried_forward"]:
            continue
        totals.setdefault(
            row["theme_id"],
            {"stories": 0, "dates": set(), "stocks": set()},
        )["stocks"].add(row["quote_code"])
        member_scores[(row["theme_id"], row["quote_code"], row["stock_name"])] += row["link_score"]
    period = []
    for theme_id, value in totals.items():
        top_members = sorted(
            (
                (score, name, code)
                for (candidate_id, code, name), score in member_scores.items()
                if candidate_id == theme_id
            ),
            reverse=True,
        )
        period.append(
            {
                "theme_id": theme_id,
                "theme_label": labels[theme_id],
                "story_count": value["stories"],
                "active_days": len(value["dates"]),
                "stock_count": len(value["stocks"]),
                "top_stocks": [f"{name}({code})" for _, name, code in top_members[:5]],
            }
        )
    period.sort(key=lambda row: (-row["story_count"], -row["active_days"], row["theme_id"]))
    return report_date, latest[:20], period[:20]


def _markdown_report(
    manifest: dict[str, Any],
    latest: list[dict[str, Any]],
    period: list[dict[str, Any]],
) -> str:
    lines = [
        "# 鉅亨網台股新聞族群報告",
        "",
        f"- 正式輸出期間：{manifest['output_start_date']} ～ {manifest['output_end_date']}",
        f"- 暖機歷史起點：{manifest['analysis_start_date']}",
        f"- 原始文章：{manifest['article_count']:,} 篇",
        f"- 納入分析：{manifest['included_article_count']:,} 篇",
        f"- 規則版本：`{manifest['rule_version']}`",
        f"- 模型參與正式判斷：`{manifest['model_used']}`",
        f"- 最近完整報告日：{manifest['report_date']}",
        "",
        "每日 snapshot 依日期由舊到新建立；D 日只使用發布時間不晚於 D 日 AsOfAt",
        "的文章，以及截至 D-1 日的狀態。歷史回填可信度為 `archive_assumed`。",
        "",
        "## 本期通過固定門檻的族群",
        "",
        "| ThemeId | 族群 | 故事數 | 活躍日 | 商品數 | 主要商品 |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for row in period:
        lines.append(
            f"| {row['theme_id']} | {row['theme_label']} | {row['story_count']} "
            f"| {row['active_days']} | {row['stock_count']} "
            f"| {', '.join(row['top_stocks']) or '-'} |"
        )
    lines.extend(
        [
            "",
            f"## {manifest['report_date']} 最近完整日",
            "",
            "| ThemeId | 族群 | 生命週期 | 故事數 | 商品數 | 七日熱度 | 主要商品 |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
    )
    for row in latest:
        heat = f"{row['heat_index_7d']:.0f}" if row["heat_index_7d"] is not None else "無前期基準"
        lines.append(
            f"| {row['theme_id']} | {row['theme_label']} | {row['theme_status']} "
            f"| {row['story_count']} | {row['stock_count']} | {heat} "
            f"| {', '.join(row['top_stocks']) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_backfill(
    *,
    days: int,
    data_dir: Path,
    end_date: date | None,
    output_start_date: date | None,
    reuse_existing: bool,
    delay_seconds: float,
    settings: Settings,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """Collect and publish chronological, point-in-time daily theme datasets."""
    if days < 1:
        raise ValueError("days must be at least 1")

    fetched_at = datetime.now(tz=TAIPEI)
    if end_date is None:
        period_end = fetched_at
    else:
        _, period_end = taipei_day_bounds(end_date)
    analysis_start_date = period_end.date() - timedelta(days=days - 1)
    period_start, _ = taipei_day_bounds(analysis_start_date)
    analysis_dates = [analysis_start_date + timedelta(days=offset) for offset in range(days)]
    output_start = output_start_date or analysis_start_date
    if not analysis_start_date <= output_start <= period_end.date():
        raise ValueError("output-start-date must be inside the collected period")
    output_dates = [day for day in analysis_dates if output_start <= day <= period_end.date()]

    output_root = data_dir / "cnyes"
    output_root.mkdir(parents=True, exist_ok=True)
    existing_catalog_path = output_root / "reference" / "dim_themes.parquet"
    existing_catalog = (
        pl.read_parquet(existing_catalog_path).to_dicts() if existing_catalog_path.exists() else []
    )

    with tempfile.TemporaryDirectory(prefix=".staging-", dir=output_root) as temporary:
        staging_root = Path(temporary)
        raw_dir = staging_root / "raw"
        articles: list[dict[str, Any]] = []
        keywords: list[dict[str, Any]] = []
        stocks: list[dict[str, Any]] = []
        seen_article_ids: set[str] = set()
        reused_dates: set[date] = set()
        fetched_dates: set[date] = set()

        with httpx.Client(timeout=settings.request_timeout_seconds) as calendar_client:
            trade_dates, holiday_records = _twse_calendar(
                client=calendar_client,
                evidence_dates=analysis_dates,
            )

        if reuse_existing:
            for day in analysis_dates:
                normalized_day = output_root / "normalized" / f"published_date={day.isoformat()}"
                required = (
                    normalized_day / "articles.parquet",
                    normalized_day / "article_keywords.parquet",
                    normalized_day / "article_stocks.parquet",
                )
                if not all(path.exists() for path in required):
                    fetched_dates.add(day)
                    continue
                articles.extend(pl.read_parquet(required[0]).to_dicts())
                keywords.extend(pl.read_parquet(required[1]).to_dicts())
                stocks.extend(pl.read_parquet(required[2]).to_dicts())
                reused_dates.add(day)
            seen_article_ids.update(article["article_id"] for article in articles)

            if fetched_dates:
                ranges: list[tuple[date, date]] = []
                range_start: date | None = None
                previous_day: date | None = None
                for day in sorted(fetched_dates):
                    if range_start is None:
                        range_start = day
                    elif previous_day is not None and day != previous_day + timedelta(days=1):
                        ranges.append((range_start, previous_day))
                        range_start = day
                    previous_day = day
                if range_start is not None and previous_day is not None:
                    ranges.append((range_start, previous_day))

                with CnyesClient(
                    timeout_seconds=settings.request_timeout_seconds,
                    user_agent=settings.user_agent,
                    delay_seconds=delay_seconds,
                ) as client:
                    for first_day, last_day in ranges:
                        range_start_at, _ = taipei_day_bounds(first_day)
                        _, range_end_at = taipei_day_bounds(last_day)
                        range_end_at = min(range_end_at, period_end)
                        for item, raw_file in client.collect(
                            start_at=range_start_at,
                            end_at=range_end_at,
                            raw_dir=raw_dir,
                        ):
                            raw_path = Path(raw_file)
                            canonical_raw_path = Path("raw") / raw_path.relative_to(raw_dir)
                            article, article_keywords, article_stocks = normalize_item(
                                item,
                                fetched_at=fetched_at,
                                raw_response_file=str(canonical_raw_path),
                            )
                            if article["article_id"] in seen_article_ids:
                                continue
                            if not period_start <= article["published_at"] <= period_end:
                                continue
                            seen_article_ids.add(article["article_id"])
                            articles.append(article)
                            keywords.extend(article_keywords)
                            stocks.extend(article_stocks)
        else:
            fetched_dates.update(analysis_dates)
            with CnyesClient(
                timeout_seconds=settings.request_timeout_seconds,
                user_agent=settings.user_agent,
                delay_seconds=delay_seconds,
            ) as client:
                for item, raw_file in client.collect(
                    start_at=period_start,
                    end_at=period_end,
                    raw_dir=raw_dir,
                ):
                    raw_path = Path(raw_file)
                    canonical_raw_path = Path("raw") / raw_path.relative_to(raw_dir)
                    article, article_keywords, article_stocks = normalize_item(
                        item,
                        fetched_at=fetched_at,
                        raw_response_file=str(canonical_raw_path),
                    )
                    if article["article_id"] in seen_article_ids:
                        continue
                    if not period_start <= article["published_at"] <= period_end:
                        continue
                    seen_article_ids.add(article["article_id"])
                    articles.append(article)
                    keywords.extend(article_keywords)
                    stocks.extend(article_stocks)

        as_of_by_date = {
            day: (
                fetched_at
                if day == fetched_at.date() and end_date is None
                else taipei_day_bounds(day)[1]
            )
            for day in analysis_dates
        }
        result = build_point_in_time_themes(
            articles=articles,
            keywords=keywords,
            stocks=stocks,
            analysis_dates=analysis_dates,
            trade_dates=trade_dates,
            as_of_by_date=as_of_by_date,
        )
        if existing_catalog:
            result = preserve_existing_theme_ids(result, existing_catalog)
        broad_daily_rows = build_broad_daily_members(
            article_themes=result.article_themes,
            evidence=result.evidence,
            theme_catalog=result.theme_catalog,
            analysis_dates=analysis_dates,
            trade_dates=trade_dates,
        )

        article_dates = {row["article_id"]: row["published_date"] for row in articles}
        normalized_partitions: dict[str, dict[date, list[dict[str, Any]]]] = {
            "articles.parquet": _partition_records(articles, "published_date"),
            "article_keywords.parquet": defaultdict(list),
            "article_stocks.parquet": defaultdict(list),
            "article_themes.parquet": _partition_records(
                result.article_themes,
                "published_date",
            ),
            "article_theme_stock_evidence.parquet": _partition_records(
                result.evidence,
                "evidence_date",
            ),
        }
        for row in keywords:
            normalized_partitions["article_keywords.parquet"][
                article_dates[row["article_id"]]
            ].append(row)
        for row in stocks:
            normalized_partitions["article_stocks.parquet"][
                article_dates[row["article_id"]]
            ].append(row)
        normalized_schemas = {
            "articles.parquet": ARTICLE_SCHEMA,
            "article_keywords.parquet": KEYWORD_SCHEMA,
            "article_stocks.parquet": STOCK_SCHEMA,
            "article_themes.parquet": ARTICLE_THEME_SCHEMA,
            "article_theme_stock_evidence.parquet": EVIDENCE_SCHEMA,
        }
        daily_partitions = {
            "daily_themes.parquet": _partition_records(
                result.daily_themes,
                "evidence_date",
            ),
            "daily_theme_members.parquet": _partition_records(
                result.daily_members,
                "evidence_date",
            ),
            "daily_theme_relations.parquet": _partition_records(
                result.daily_relations,
                "evidence_date",
            ),
        }
        daily_schemas = {
            "daily_themes.parquet": DAILY_THEME_SCHEMA,
            "daily_theme_members.parquet": DAILY_MEMBER_SCHEMA,
            "daily_theme_relations.parquet": DAILY_RELATION_SCHEMA,
        }
        broad_daily_partitions = _partition_records(
            broad_daily_rows,
            "evidence_date",
        )

        for day in analysis_dates:
            normalized_day = staging_root / "normalized" / f"published_date={day.isoformat()}"
            for filename, partitions in normalized_partitions.items():
                _write_parquet(
                    normalized_day / filename,
                    partitions.get(day, []),
                    normalized_schemas[filename],
                )
        for day in output_dates:
            daily_day = staging_root / "daily" / f"evidence_date={day.isoformat()}"
            for filename, partitions in daily_partitions.items():
                _write_parquet(
                    daily_day / filename,
                    partitions.get(day, []),
                    daily_schemas[filename],
                )
            broad_daily_day = staging_root / "broad_daily" / f"evidence_date={day.isoformat()}"
            _write_parquet(
                broad_daily_day / "daily_broad_theme_members.parquet",
                broad_daily_partitions.get(day, []),
                BROAD_DAILY_SCHEMA,
            )

        reference_dir = staging_root / "reference"
        _write_parquet(
            reference_dir / "dim_themes.parquet",
            result.theme_catalog,
            THEME_CATALOG_SCHEMA,
        )
        _write_parquet(
            reference_dir / "dim_theme_aliases.parquet",
            result.theme_aliases,
            THEME_ALIAS_SCHEMA,
        )
        _write_json(
            reference_dir / "twse_holiday_schedule.json",
            {
                "source_url": TWSE_HOLIDAY_URL,
                "fetched_at": fetched_at.isoformat(),
                "records": holiday_records,
            },
        )
        _write_json(reference_dir / "data_leak_audit.json", result.audit)

        report_date, report, period_report = _report_data(
            result,
            output_start_date=output_start,
            output_end_date=period_end.date(),
            fetched_at=fetched_at,
            historical_end_date=end_date,
        )
        output_theme_rows = [
            row
            for row in result.daily_themes
            if output_start <= row["evidence_date"] <= period_end.date()
        ]
        output_member_rows = [
            row
            for row in result.daily_members
            if output_start <= row["evidence_date"] <= period_end.date()
        ]
        output_relation_rows = [
            row
            for row in result.daily_relations
            if output_start <= row["evidence_date"] <= period_end.date()
        ]
        output_broad_rows = [
            row
            for row in broad_daily_rows
            if output_start <= row["evidence_date"] <= period_end.date()
        ]
        broad_definitions = broad_theme_definitions(result.theme_catalog)
        broad_parent_count = sum(theme.parent_theme_id is None for theme in broad_definitions)
        article_type_counts: dict[str, int] = defaultdict(int)
        for article in articles:
            article_type_counts[article["article_type"]] += 1
        manifest = {
            "schema_version": SNAPSHOT_VERSION,
            "rule_version": RULE_VERSION,
            "catalog_version": CATALOG_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "broad_rule_version": BROAD_RULE_VERSION,
            "broad_snapshot_version": BROAD_SNAPSHOT_VERSION,
            "source": "cnyes",
            "category": "tw_stock_news",
            "analysis_start_date": analysis_start_date.isoformat(),
            "output_start_date": output_start.isoformat(),
            "output_end_date": period_end.date().isoformat(),
            "period_start_at": period_start.isoformat(),
            "period_end_at": period_end.isoformat(),
            "generated_at": fetched_at.isoformat(),
            "warmup_days": (output_start - analysis_start_date).days,
            "availability_grade": "archive_assumed",
            "today_is_partial": end_date is None,
            "reused_existing_articles": reuse_existing,
            "reused_date_count": len(reused_dates),
            "fetched_date_count": len(fetched_dates),
            "model_used": False,
            "article_count": len(articles),
            "included_article_count": sum(article["included_in_analysis"] for article in articles),
            "unique_story_count": len(
                {article["story_id"] for article in articles if article["included_in_analysis"]}
            ),
            "theme_catalog_count": len(result.theme_catalog),
            "theme_alias_count": len(result.theme_aliases),
            "daily_theme_row_count": len(output_theme_rows),
            "daily_member_row_count": len(output_member_rows),
            "daily_relation_row_count": len(output_relation_rows),
            "broad_theme_count": broad_parent_count,
            "broad_fixed_parent_count": len(BROAD_THEMES),
            "broad_dynamic_parent_count": broad_parent_count - len(BROAD_THEMES),
            "broad_subtheme_count": len(SUBTHEMES),
            "broad_daily_row_count": len(output_broad_rows),
            "article_type_counts": dict(article_type_counts),
            "report_date": report_date.isoformat(),
        }
        report_markdown = _markdown_report(manifest, report, period_report)

        for day in analysis_dates:
            if day in fetched_dates:
                _replace_directory(
                    staging_root / "raw" / f"published_date={day.isoformat()}",
                    output_root / "raw" / f"published_date={day.isoformat()}",
                )
            _replace_directory(
                staging_root / "normalized" / f"published_date={day.isoformat()}",
                output_root / "normalized" / f"published_date={day.isoformat()}",
            )
        for day in output_dates:
            _replace_directory(
                staging_root / "daily" / f"evidence_date={day.isoformat()}",
                output_root / "daily" / f"evidence_date={day.isoformat()}",
            )
            _replace_directory(
                staging_root / "broad_daily" / f"evidence_date={day.isoformat()}",
                output_root / "broad_daily" / f"evidence_date={day.isoformat()}",
            )
        _replace_directory(reference_dir, output_root / "reference")

        completed_end_date = (
            period_end.date() if end_date is not None else fetched_at.date() - timedelta(days=1)
        )
        universe_path, universe_row_count, universe_start_date = _write_broad_universe_parquet(
            output_root=output_root,
            completed_end_date=completed_end_date,
        )
        manifest.update(
            {
                "theme_universe_file": str(universe_path.relative_to(data_dir)),
                "theme_universe_start_date": (
                    universe_start_date.isoformat() if universe_start_date else None
                ),
                "theme_universe_end_date": completed_end_date.isoformat(),
                "theme_universe_row_count": universe_row_count,
                "theme_universe_format": "parquet",
                "theme_universe_compression": "zstd",
            }
        )
        _write_json(output_root / "manifest.json", manifest)
        _write_json(output_root / "report.json", report)
        _write_json(output_root / "period_report.json", period_report)
        temporary_report = output_root / f".report.md.tmp-{uuid.uuid4().hex}"
        temporary_report.write_text(report_markdown, encoding="utf-8")
        temporary_report.replace(output_root / "report.md")

    return output_root, manifest, report
