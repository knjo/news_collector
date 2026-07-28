"""Deterministic, point-in-time daily theme construction."""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import combinations
from typing import Any

import polars as pl

from news_collector.analysis.theme_taxonomy import (
    TAXONOMY_VERSION,
    is_excluded_theme_label,
)
from news_collector.analysis.topics import assign_story_ids, normalized_text

RULE_VERSION = "theme-rule-v6"
CATALOG_VERSION = "theme-catalog-v4"
SNAPSHOT_VERSION = "cnyes-theme-v6"

MIN_THEME_STORIES = 2
MIN_THEME_STOCKS = 3
MIN_MEMBER_SCORE = 2.0
CONTINUING_GAP_DAYS = 2
SEASON_WINDOW_DAYS = 60

SEED_THEMES = (
    {
        "theme_id": 1,
        "theme_key": "ARTIFICIAL_INTELLIGENCE",
        "theme_label": "人工智慧",
        "description": "人工智慧技術、應用與供應鏈",
        "aliases": ("AI", "人工智慧", "人工智能"),
    },
    {
        "theme_id": 2,
        "theme_key": "AI_SERVER",
        "theme_label": "AI伺服器",
        "description": "人工智慧伺服器及其供應鏈",
        "aliases": ("AI Server", "AI伺服器", "人工智慧伺服器", "AI伺服器供應鏈"),
    },
    {
        "theme_id": 3,
        "theme_key": "CPO_SILICON_PHOTONICS",
        "theme_label": "CPO／矽光子",
        "description": "共同封裝光學與矽光子供應鏈",
        "aliases": ("CPO", "矽光子", "共同封裝光學", "CPO／矽光子"),
    },
    {
        "theme_id": 4,
        "theme_key": "PCB",
        "theme_label": "PCB",
        "description": "印刷電路板產業",
        "aliases": ("PCB", "印刷電路板"),
    },
)

_GENERIC_KEYWORDS = {
    "ai概念股",
    "top",
    "上市",
    "上櫃",
    "中國",
    "主力",
    "個股",
    "休市",
    "公司",
    "公告",
    "台灣",
    "台股",
    "台股大盤",
    "台股指數",
    "台股盤中",
    "台股盤前",
    "台股盤前要聞",
    "台股盤後",
    "國際",
    "大盤",
    "外資",
    "市場",
    "投信",
    "股利",
    "指數走勢",
    "摘要",
    "收盤",
    "新聞",
    "日本",
    "法人",
    "法說",
    "法說會",
    "漲停",
    "熱門股",
    "千金股",
    "產業",
    "盤中",
    "盤後",
    "科技",
    "美股",
    "美國",
    "股價",
    "股票",
    "董事會",
    "財報",
    "趨勢分析",
    "開盤",
    "集中市場加權指數",
    "櫃買市場加權指數",
    "鉅亨速報",
    "除權息",
    "除息",
    "三大法人",
    "每股純益",
    "現金股利",
    "營收",
    "營收速報",
    "獲利",
    "eps",
    "etf",
}
_GEOGRAPHIES = {
    "亞洲",
    "中國",
    "台北",
    "台南",
    "台中",
    "台東",
    "台灣",
    "南韓",
    "嘉義",
    "基隆",
    "宜蘭",
    "屏東",
    "彰化",
    "新北",
    "新竹",
    "日本",
    "新加坡",
    "桃園",
    "歐洲",
    "泰國",
    "澎湖",
    "美國",
    "英國",
    "苗栗",
    "花蓮",
    "金門",
    "韓國",
    "雲林",
    "高雄",
    "印度",
    "印尼",
    "越南",
    "香港",
}
_ORGANIZATIONS = {
    "中央銀行",
    "主計總處",
    "公平會",
    "國發會",
    "國科會",
    "櫃買中心",
    "經濟部",
    "證交所",
    "財政部",
    "金管會",
}
_CODE = re.compile(r"^(?:\d{4,6}|US-[A-Z]{1,5})$", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])|\n+")
_CHINESE_NAME = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_PERSON_CONTEXT = re.compile(
    r"(?:執行長|董事長|創辦人|總經理|分析師|經濟學家|總裁|主席|部長|院長|現身)"
)


ARTICLE_THEME_SCHEMA = {
    "article_id": pl.String,
    "story_id": pl.String,
    "published_date": pl.Date,
    "theme_id": pl.UInt32,
    "theme_label": pl.String,
    "raw_keyword": pl.String,
    "is_title_mention": pl.Boolean,
    "is_first_mention": pl.Boolean,
    "rule_version": pl.String,
}
EVIDENCE_SCHEMA = {
    "evidence_date": pl.Date,
    "article_id": pl.String,
    "story_id": pl.String,
    "theme_id": pl.UInt32,
    "quote_code": pl.String,
    "stock_name": pl.String,
    "symbol": pl.String,
    "evidence_type": pl.String,
    "evidence_text": pl.String,
    "mention_count": pl.UInt32,
    "is_title_mention": pl.Boolean,
    "raw_score": pl.Float64,
    "dilution": pl.Float64,
    "story_score": pl.Float64,
    "qualifies_as_member": pl.Boolean,
    "rule_version": pl.String,
}
DAILY_THEME_SCHEMA = {
    "evidence_date": pl.Date,
    "as_of_at": pl.Datetime("us", "Asia/Taipei"),
    "trade_date": pl.Date,
    "theme_id": pl.UInt32,
    "theme_label": pl.String,
    "theme_status": pl.String,
    "has_news_today": pl.Boolean,
    "is_active_today": pl.Boolean,
    "first_seen_date": pl.Date,
    "last_news_date": pl.Date,
    "count_in_season": pl.UInt32,
    "consecutive_news_days": pl.UInt32,
    "days_since_last_news": pl.UInt32,
    "inactive_days_before_today": pl.UInt32,
    "active_streak_days": pl.UInt32,
    "source_count": pl.UInt32,
    "theme_count": pl.UInt32,
    "article_count": pl.UInt32,
    "stock_count": pl.UInt32,
    "title_mention_count": pl.UInt32,
    "first_mention_count": pl.UInt32,
    "theme_share": pl.Float64,
    "previous_7d_story_mean": pl.Float64,
    "previous_7d_share_mean": pl.Float64,
    "heat_index_7d": pl.Float64,
    "heat_status": pl.String,
    "is_main_theme": pl.Boolean,
    "relevance_sum": pl.Float64,
    "history_start_date": pl.Date,
    "availability_grade": pl.String,
    "rule_version": pl.String,
    "catalog_version": pl.String,
    "snapshot_version": pl.String,
}
DAILY_MEMBER_SCHEMA = {
    "evidence_date": pl.Date,
    "as_of_at": pl.Datetime("us", "Asia/Taipei"),
    "trade_date": pl.Date,
    "theme_id": pl.UInt32,
    "theme_label": pl.String,
    "quote_code": pl.String,
    "stock_name": pl.String,
    "symbol": pl.String,
    "exchange": pl.String,
    "mention_count": pl.UInt32,
    "unique_story_count": pl.UInt32,
    "title_mention_count": pl.UInt32,
    "link_score": pl.Float64,
    "member_weight": pl.Float64,
    "member_rank": pl.UInt32,
    "member_status": pl.String,
    "first_member_date": pl.Date,
    "member_streak_days": pl.UInt32,
    "member_snapshot_date": pl.Date,
    "is_carried_forward": pl.Boolean,
    "availability_grade": pl.String,
    "rule_version": pl.String,
    "catalog_version": pl.String,
    "snapshot_version": pl.String,
}
DAILY_RELATION_SCHEMA = {
    "evidence_date": pl.Date,
    "as_of_at": pl.Datetime("us", "Asia/Taipei"),
    "trade_date": pl.Date,
    "source_theme_id": pl.UInt32,
    "target_theme_id": pl.UInt32,
    "relation_type": pl.String,
    "window": pl.String,
    "lag_minutes": pl.Int32,
    "connection_count": pl.UInt32,
    "jaccard_score": pl.Float64,
    "availability_grade": pl.String,
    "rule_version": pl.String,
    "catalog_version": pl.String,
    "snapshot_version": pl.String,
}
THEME_CATALOG_SCHEMA = {
    "theme_id": pl.UInt32,
    "theme_key": pl.String,
    "theme_label": pl.String,
    "description": pl.String,
    "status": pl.String,
    "origin_type": pl.String,
    "is_seed": pl.Boolean,
    "created_date": pl.Date,
    "first_seen_date": pl.Date,
    "catalog_version": pl.String,
    "rule_version": pl.String,
}
THEME_ALIAS_SCHEMA = {
    "theme_id": pl.UInt32,
    "alias": pl.String,
    "normalized_alias": pl.String,
    "alias_type": pl.String,
    "effective_from": pl.Date,
    "catalog_version": pl.String,
    "rule_version": pl.String,
}


@dataclass
class ThemeBuildResult:
    article_themes: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    daily_themes: list[dict[str, Any]]
    daily_members: list[dict[str, Any]]
    daily_relations: list[dict[str, Any]]
    theme_catalog: list[dict[str, Any]]
    theme_aliases: list[dict[str, Any]]
    audit: dict[str, Any]


def preserve_existing_theme_ids(
    result: ThemeBuildResult,
    existing_catalog: list[dict[str, Any]],
) -> ThemeBuildResult:
    """Keep prior theme-key IDs stable and allocate only new IDs above the prior max."""

    existing_by_key = {
        str(row["theme_key"]): int(row["theme_id"])
        for row in existing_catalog
        if row.get("catalog_version") == CATALOG_VERSION
    }
    for theme in result.theme_catalog:
        if bool(theme["is_seed"]):
            existing_by_key.setdefault(
                str(theme["theme_key"]),
                int(theme["theme_id"]),
            )
    used_ids = set(existing_by_key.values())
    next_theme_id = max(used_ids, default=4) + 1
    id_mapping: dict[int, int] = {}
    for theme in sorted(result.theme_catalog, key=lambda row: int(row["theme_id"])):
        generated_id = int(theme["theme_id"])
        theme_key = str(theme["theme_key"])
        stable_id = existing_by_key.get(theme_key)
        if stable_id is None:
            while next_theme_id in used_ids:
                next_theme_id += 1
            stable_id = next_theme_id
            used_ids.add(stable_id)
            next_theme_id += 1
        id_mapping[generated_id] = stable_id

    def remap(rows: list[dict[str, Any]], *columns: str) -> None:
        for row in rows:
            for column in columns:
                row[column] = id_mapping[int(row[column])]

    remap(result.article_themes, "theme_id")
    remap(result.evidence, "theme_id")
    remap(result.daily_themes, "theme_id")
    remap(result.daily_members, "theme_id")
    remap(result.daily_relations, "source_theme_id", "target_theme_id")
    remap(result.theme_catalog, "theme_id")
    remap(result.theme_aliases, "theme_id")
    result.theme_catalog.sort(key=lambda row: int(row["theme_id"]))
    result.theme_aliases.sort(key=lambda row: (int(row["theme_id"]), str(row["normalized_alias"])))
    if len({row["theme_id"] for row in result.theme_catalog}) != len(result.theme_catalog):
        raise ValueError("theme ID preservation produced duplicate IDs")
    return result


def _canonical_key(label: str) -> str:
    return f"AUTO_{hashlib.sha1(normalized_text(label).encode()).hexdigest()[:12].upper()}"


def _contains(text: str, term: str) -> bool:
    if not text or not term:
        return False
    escaped = re.escape(term)
    if term.isascii() and term.replace(" ", "").isalnum():
        return re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text, re.I) is not None
    return re.search(escaped, text, re.I) is not None


def _count_terms(text: str, terms: tuple[str, ...]) -> int:
    count = 0
    seen_spans: set[tuple[int, int]] = set()
    for term in sorted(set(terms), key=len, reverse=True):
        if not term:
            continue
        escaped = re.escape(term)
        boundary = term.isascii() and term.replace(" ", "").isalnum()
        pattern = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])" if boundary else escaped
        for match in re.finditer(pattern, text, re.I):
            span = match.span()
            if span not in seen_spans:
                seen_spans.add(span)
                count += 1
    return count


def _first_matching_sentence(
    text: str,
    theme_terms: tuple[str, ...],
    stock_terms: tuple[str, ...],
) -> str | None:
    for sentence in _SENTENCE_BREAK.split(text):
        compact = sentence.strip()
        if (
            compact
            and any(_contains(compact, term) for term in theme_terms)
            and any(_contains(compact, term) for term in stock_terms)
        ):
            return compact[:500]
    return None


def _score_pair(
    article: dict[str, Any],
    *,
    theme_terms: tuple[str, ...],
    stock_name: str,
    quote_code: str,
    tagged_stock_count: int,
    allow_tag_to_stock_title: bool,
) -> dict[str, Any]:
    title = article["title"]
    summary = article["summary"]
    body = article["body_text"]
    full_text = "\n".join((title, summary, body))
    paragraphs = tuple(
        paragraph.strip()
        for paragraph in re.split(r"\n+", "\n".join((summary, body)))
        if paragraph.strip()
    )
    lead = "\n".join(paragraphs[:3])
    stock_terms = tuple(term for term in (stock_name, quote_code) if term)
    topic_in_title = any(_contains(title, term) for term in theme_terms)
    stock_in_title = any(_contains(title, term) for term in stock_terms)

    raw_score = 0.5
    evidence_type = "TAG_ONLY"
    evidence_text = ""
    if topic_in_title and stock_in_title:
        raw_score = 4.0
        evidence_type = "TITLE"
        evidence_text = title[:500]
    elif stock_in_title and allow_tag_to_stock_title:
        raw_score = 2.0
        evidence_type = "TAG_TO_STOCK_TITLE"
        evidence_text = title[:500]
    else:
        sentence = _first_matching_sentence(full_text, theme_terms, stock_terms)
        if sentence is not None:
            raw_score = 3.0
            evidence_type = "SAME_SENTENCE"
            evidence_text = sentence
        else:
            paragraph = next(
                (
                    value
                    for value in paragraphs
                    if any(_contains(value, term) for term in theme_terms)
                    and any(_contains(value, term) for term in stock_terms)
                ),
                None,
            )
            if paragraph is not None:
                raw_score = 2.0
                evidence_type = "SAME_PARAGRAPH"
                evidence_text = paragraph[:500]
            elif topic_in_title and any(_contains(lead, term) for term in stock_terms):
                raw_score = 2.0
                evidence_type = "TITLE_TO_LEAD"
                evidence_text = lead[:500]

    if tagged_stock_count > 10:
        dilution = 0.3
    elif tagged_stock_count > 5:
        dilution = 0.7
    else:
        dilution = 1.0
    story_score = raw_score * dilution
    return {
        "evidence_type": evidence_type,
        "evidence_text": evidence_text,
        "mention_count": _count_terms(full_text, stock_terms),
        "is_title_mention": stock_in_title,
        "raw_score": raw_score,
        "dilution": dilution,
        "story_score": story_score,
    }


def _heat_status(index: float | None) -> str:
    if index is None:
        return "NEW_BASELINE"
    if index >= 150:
        return "HEATING"
    if index <= 67:
        return "COOLING"
    return "STEADY"


def _consecutive_days_ending_on(day: date, news_dates: set[date]) -> int:
    if day not in news_dates:
        return 0
    streak = 0
    cursor = day
    while cursor in news_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _eligible_keyword(
    keyword: str,
    *,
    known_stock_names: set[str],
    known_stock_codes: set[str],
) -> bool:
    normalized = normalized_text(keyword)
    if (
        not normalized
        or is_excluded_theme_label(keyword)
        or normalized in {normalized_text(value) for value in _GENERIC_KEYWORDS}
        or normalized in {normalized_text(value) for value in _GEOGRAPHIES}
        or normalized in {normalized_text(value) for value in _ORGANIZATIONS}
        or normalized in known_stock_names
        or normalized in known_stock_codes
        or _CODE.fullmatch(keyword)
    ):
        return False
    return not keyword.endswith(("投顧", "證券"))


def _looks_like_person(
    keyword: str,
    *,
    article_ids: set[str],
    article_by_id: dict[str, dict[str, Any]],
) -> bool:
    if not _CHINESE_NAME.fullmatch(keyword):
        return False
    contexts = 0
    for article_id in article_ids:
        text = f"{article_by_id[article_id]['title']} {article_by_id[article_id]['summary']}"
        if re.search(rf"{re.escape(keyword)}\s*[：:]", text) or (
            keyword in text and _PERSON_CONTEXT.search(text)
        ):
            contexts += 1
    return contexts > 0 and contexts / len(article_ids) >= 0.5


def _inferred_title_mention(theme_key: str, title: str) -> bool:
    if theme_key == "AI_SERVER":
        return (_contains(title, "AI") or _contains(title, "人工智慧")) and _contains(
            title, "伺服器"
        )
    return False


def _allow_tag_to_stock_title(theme_key: str, title: str) -> bool:
    if theme_key == "ARTIFICIAL_INTELLIGENCE":
        return False
    if theme_key == "AI_SERVER":
        return any(
            _contains(title, term)
            for term in ("伺服器", "Server", "資料中心", "EPYC", "AWS", "GB300")
        )
    return True


def build_point_in_time_themes(
    *,
    articles: list[dict[str, Any]],
    keywords: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
    analysis_dates: list[date],
    trade_dates: dict[date, date],
    as_of_by_date: dict[date, datetime],
) -> ThemeBuildResult:
    """Replay dates in order; each snapshot sees only current and prior state."""
    if not analysis_dates:
        raise ValueError("analysis_dates must not be empty")
    history_start = min(analysis_dates)
    assign_story_ids(articles, stocks)

    articles_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    keywords_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stocks_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        articles_by_date[article["published_date"]].append(article)
    for keyword in keywords:
        keywords_by_article[keyword["article_id"]].append(keyword)
    for stock in stocks:
        stocks_by_article[stock["article_id"]].append(stock)

    catalog: dict[str, dict[str, Any]] = {}
    alias_lookup: dict[str, str] = {}
    aliases: dict[tuple[int, str], dict[str, Any]] = {}
    for seed in SEED_THEMES:
        catalog[seed["theme_key"]] = {
            "theme_id": seed["theme_id"],
            "theme_key": seed["theme_key"],
            "theme_label": seed["theme_label"],
            "description": seed["description"],
            "status": "ACTIVE",
            "origin_type": "SEED",
            "is_seed": True,
            "created_date": None,
            "first_seen_date": None,
            "catalog_version": CATALOG_VERSION,
            "rule_version": RULE_VERSION,
            "aliases": tuple(seed["aliases"]),
        }
        for alias in seed["aliases"]:
            normalized = normalized_text(alias)
            alias_lookup[normalized] = seed["theme_key"]
            aliases[(seed["theme_id"], normalized)] = {
                "theme_id": seed["theme_id"],
                "alias": alias,
                "normalized_alias": normalized,
                "alias_type": "SEED_EXACT",
                "effective_from": date(1900, 1, 1),
                "catalog_version": CATALOG_VERSION,
                "rule_version": RULE_VERSION,
            }

    next_theme_id = 5
    known_stock_names: set[str] = set()
    known_stock_codes: set[str] = set()
    theme_state: dict[int, dict[str, Any]] = {}
    member_state: dict[tuple[int, str], dict[str, Any]] = {}
    theme_history: dict[tuple[int, date], dict[str, float]] = {}
    news_dates_by_theme: dict[str, set[date]] = defaultdict(set)
    latest_member_snapshots: dict[int, list[dict[str, Any]]] = {}

    article_theme_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    daily_theme_rows: list[dict[str, Any]] = []
    daily_member_rows: list[dict[str, Any]] = []
    daily_relation_rows: list[dict[str, Any]] = []
    day_audit: list[dict[str, Any]] = []

    for day in sorted(analysis_dates):
        day_articles = [
            article
            for article in articles_by_date.get(day, [])
            if article["included_in_analysis"] and article["published_at"] <= as_of_by_date[day]
        ]
        article_by_id = {article["article_id"]: article for article in day_articles}
        for article in day_articles:
            for stock in stocks_by_article[article["article_id"]]:
                known_stock_names.add(normalized_text(stock["stock_name"]))
                known_stock_codes.add(normalized_text(stock["quote_code"]))

        topics_by_article: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        raw_aliases_by_key: dict[str, set[str]] = defaultdict(set)
        for article in day_articles:
            article_keywords = sorted(
                keywords_by_article[article["article_id"]],
                key=lambda row: row["keyword_position"],
            )
            for keyword in article_keywords:
                raw_keyword = keyword["keyword"].strip()
                if not _eligible_keyword(
                    raw_keyword,
                    known_stock_names=known_stock_names,
                    known_stock_codes=known_stock_codes,
                ):
                    continue
                normalized = normalized_text(raw_keyword)
                theme_key = alias_lookup.get(normalized) or _canonical_key(raw_keyword)
                theme_label = (
                    catalog[theme_key]["theme_label"] if theme_key in catalog else raw_keyword
                )
                raw_aliases_by_key[theme_key].add(raw_keyword)
                topic = topics_by_article[article["article_id"]].setdefault(
                    theme_key,
                    {
                        "theme_key": theme_key,
                        "theme_label": theme_label,
                        "raw_keyword": raw_keyword,
                        "keyword_position": keyword["keyword_position"],
                        "is_title_mention": False,
                    },
                )
                topic["keyword_position"] = min(
                    topic["keyword_position"],
                    keyword["keyword_position"],
                )
                topic["is_title_mention"] = (
                    topic["is_title_mention"]
                    or keyword["is_title_mention"]
                    or _inferred_title_mention(theme_key, article["title"])
                )
            for theme_key, theme in catalog.items():
                matched_aliases = [
                    alias for alias in theme["aliases"] if _contains(article["title"], alias)
                ]
                if not matched_aliases and not _inferred_title_mention(
                    theme_key,
                    article["title"],
                ):
                    continue
                raw_alias = (
                    sorted(matched_aliases, key=lambda value: (-len(value), value))[0]
                    if matched_aliases
                    else theme["theme_label"]
                )
                raw_aliases_by_key[theme_key].add(raw_alias)
                topic = topics_by_article[article["article_id"]].setdefault(
                    theme_key,
                    {
                        "theme_key": theme_key,
                        "theme_label": theme["theme_label"],
                        "raw_keyword": raw_alias,
                        "keyword_position": len(article_keywords),
                        "is_title_mention": True,
                    },
                )
                topic["is_title_mention"] = True

        topic_story_sets: dict[str, set[str]] = defaultdict(set)
        topic_article_sets: dict[str, set[str]] = defaultdict(set)
        topic_title_story_sets: dict[str, set[str]] = defaultdict(set)
        topic_first_story_sets: dict[str, set[str]] = defaultdict(set)
        provisional_evidence: dict[tuple[str, str, str], dict[str, Any]] = {}
        topic_rows_for_day: list[dict[str, Any]] = []

        for article_id, article_topics in topics_by_article.items():
            article = article_by_id[article_id]
            first_position = min(topic["keyword_position"] for topic in article_topics.values())
            tagged_stocks = [
                stock
                for stock in stocks_by_article[article_id]
                if stock["symbol"].startswith(("TWS:", "TWG:"))
            ]
            for theme_key, topic in article_topics.items():
                topic_story_sets[theme_key].add(article["story_id"])
                topic_article_sets[theme_key].add(article_id)
                if topic["is_title_mention"]:
                    topic_title_story_sets[theme_key].add(article["story_id"])
                if topic["keyword_position"] == first_position:
                    topic_first_story_sets[theme_key].add(article["story_id"])

                known_aliases = (
                    catalog[theme_key]["aliases"]
                    if theme_key in catalog
                    else tuple(raw_aliases_by_key[theme_key])
                )
                theme_terms = tuple(dict.fromkeys((*known_aliases, topic["raw_keyword"])))
                topic_rows_for_day.append(
                    {
                        "article_id": article_id,
                        "story_id": article["story_id"],
                        "published_date": day,
                        "theme_key": theme_key,
                        "theme_label": topic["theme_label"],
                        "raw_keyword": topic["raw_keyword"],
                        "is_title_mention": topic["is_title_mention"],
                        "is_first_mention": topic["keyword_position"] == first_position,
                    }
                )
                for stock in tagged_stocks:
                    scored = _score_pair(
                        article,
                        theme_terms=theme_terms,
                        stock_name=stock["stock_name"],
                        quote_code=stock["quote_code"],
                        tagged_stock_count=len(tagged_stocks),
                        allow_tag_to_stock_title=_allow_tag_to_stock_title(
                            theme_key,
                            article["title"],
                        ),
                    )
                    evidence_key = (theme_key, stock["symbol"], article["story_id"])
                    value = {
                        "evidence_date": day,
                        "article_id": article_id,
                        "story_id": article["story_id"],
                        "theme_key": theme_key,
                        "quote_code": stock["quote_code"],
                        "stock_name": stock["stock_name"],
                        "symbol": stock["symbol"],
                        **scored,
                    }
                    previous = provisional_evidence.get(evidence_key)
                    if previous is None or value["story_score"] > previous["story_score"]:
                        provisional_evidence[evidence_key] = value
                    elif previous is not None:
                        previous["mention_count"] = max(
                            previous["mention_count"],
                            value["mention_count"],
                        )
                        previous["is_title_mention"] = (
                            previous["is_title_mention"] or value["is_title_mention"]
                        )

        person_keys = {
            theme_key
            for theme_key, raw_aliases in raw_aliases_by_key.items()
            if any(
                _looks_like_person(
                    alias,
                    article_ids=topic_article_sets[theme_key],
                    article_by_id=article_by_id,
                )
                for alias in raw_aliases
            )
        }
        for theme_key in person_keys:
            topic_story_sets.pop(theme_key, None)
            topic_article_sets.pop(theme_key, None)
            topic_title_story_sets.pop(theme_key, None)
            topic_first_story_sets.pop(theme_key, None)
        provisional_evidence = {
            key: value
            for key, value in provisional_evidence.items()
            if value["theme_key"] not in person_keys
        }
        topic_rows_for_day = [
            row for row in topic_rows_for_day if row["theme_key"] not in person_keys
        ]
        for theme_key, story_ids in topic_story_sets.items():
            if story_ids:
                news_dates_by_theme[theme_key].add(day)

        member_candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for evidence in provisional_evidence.values():
            key = (evidence["theme_key"], evidence["symbol"])
            member = member_candidates.setdefault(
                key,
                {
                    "theme_key": evidence["theme_key"],
                    "quote_code": evidence["quote_code"],
                    "stock_name": evidence["stock_name"],
                    "symbol": evidence["symbol"],
                    "mention_count": 0,
                    "story_ids": set(),
                    "title_story_ids": set(),
                    "link_score": 0.0,
                },
            )
            member["mention_count"] += evidence["mention_count"]
            member["story_ids"].add(evidence["story_id"])
            if evidence["is_title_mention"]:
                member["title_story_ids"].add(evidence["story_id"])
            member["link_score"] += evidence["story_score"]

        qualified_member_keys = {
            key
            for key, member in member_candidates.items()
            if member["link_score"] >= MIN_MEMBER_SCORE
        }
        stocks_by_topic: dict[str, set[str]] = defaultdict(set)
        for theme_key, symbol in qualified_member_keys:
            stocks_by_topic[theme_key].add(symbol)

        active_keys = {
            theme_key
            for theme_key, story_ids in topic_story_sets.items()
            if len(story_ids) >= MIN_THEME_STORIES
            and len(stocks_by_topic[theme_key]) >= MIN_THEME_STOCKS
        }

        for theme_key in sorted(active_keys):
            if theme_key in catalog:
                continue
            raw_alias = sorted(
                raw_aliases_by_key[theme_key],
                key=lambda value: (len(value), value),
            )[0]
            theme_id = next_theme_id
            next_theme_id += 1
            catalog[theme_key] = {
                "theme_id": theme_id,
                "theme_key": theme_key,
                "theme_label": raw_alias,
                "description": "由固定門檻自動建立的候選題材",
                "status": "ACTIVE",
                "origin_type": "AUTO",
                "is_seed": False,
                "created_date": day,
                "first_seen_date": day,
                "catalog_version": CATALOG_VERSION,
                "rule_version": RULE_VERSION,
                "aliases": (raw_alias,),
            }
            normalized_alias = normalized_text(raw_alias)
            alias_lookup[normalized_alias] = theme_key
            aliases[(theme_id, normalized_alias)] = {
                "theme_id": theme_id,
                "alias": raw_alias,
                "normalized_alias": normalized_alias,
                "alias_type": "AUTO_EXACT",
                "effective_from": day,
                "catalog_version": CATALOG_VERSION,
                "rule_version": RULE_VERSION,
            }

        source_story_count = len({article["story_id"] for article in day_articles})
        active_rows_for_day: list[dict[str, Any]] = []
        member_rows_for_day: list[dict[str, Any]] = []
        active_story_sets: dict[int, set[str]] = {}
        active_member_sets: dict[int, set[str]] = {}

        for theme_key in sorted(active_keys, key=lambda key: catalog[key]["theme_id"]):
            theme = catalog[theme_key]
            theme_id = theme["theme_id"]
            theme["first_seen_date"] = theme["first_seen_date"] or day
            previous_state = theme_state.get(theme_id)
            inactive_days = (
                (day - previous_state["last_active_date"]).days - 1 if previous_state else 0
            )
            if previous_state is None:
                status = "NEW"
                first_seen_date = day
                active_streak = 1
            else:
                status = "CONTINUING" if inactive_days <= CONTINUING_GAP_DAYS else "REVIVED"
                first_seen_date = previous_state["first_seen_date"]
                active_streak = (
                    previous_state["active_streak_days"] + 1 if inactive_days == 0 else 1
                )
            theme_state[theme_id] = {
                "first_seen_date": first_seen_date,
                "last_active_date": day,
                "active_streak_days": active_streak,
            }

            theme_count = len(topic_story_sets[theme_key])
            theme_share = theme_count / source_story_count if source_story_count else 0.0
            season_start = day - timedelta(days=SEASON_WINDOW_DAYS - 1)
            season_news_dates = {
                news_day
                for news_day in news_dates_by_theme[theme_key]
                if season_start <= news_day <= day
            }
            previous_counts = [
                theme_history.get(
                    (theme_id, day - timedelta(days=offset)),
                    {"theme_count": 0.0},
                )["theme_count"]
                for offset in range(1, 8)
            ]
            previous_shares = [
                theme_history.get(
                    (theme_id, day - timedelta(days=offset)),
                    {"theme_share": 0.0},
                )["theme_share"]
                for offset in range(1, 8)
            ]
            prior_count_mean = statistics.mean(previous_counts)
            prior_share_mean = statistics.mean(previous_shares)
            heat_index = theme_share / prior_share_mean * 100 if prior_share_mean > 0 else None
            theme_history[(theme_id, day)] = {
                "theme_count": float(theme_count),
                "theme_share": theme_share,
            }

            theme_members = [
                member
                for (candidate_key, _), member in member_candidates.items()
                if candidate_key == theme_key
                and (candidate_key, member["symbol"]) in qualified_member_keys
            ]
            relevance_sum = sum(member["link_score"] for member in theme_members)
            active_rows_for_day.append(
                {
                    "evidence_date": day,
                    "as_of_at": as_of_by_date[day],
                    "trade_date": trade_dates[day],
                    "theme_id": theme_id,
                    "theme_label": theme["theme_label"],
                    "theme_status": status,
                    "has_news_today": True,
                    "is_active_today": True,
                    "first_seen_date": first_seen_date,
                    "last_news_date": day,
                    "count_in_season": len(season_news_dates),
                    "consecutive_news_days": _consecutive_days_ending_on(
                        day,
                        news_dates_by_theme[theme_key],
                    ),
                    "days_since_last_news": 0,
                    "inactive_days_before_today": inactive_days,
                    "active_streak_days": active_streak,
                    "source_count": source_story_count,
                    "theme_count": theme_count,
                    "article_count": len(topic_article_sets[theme_key]),
                    "stock_count": len(theme_members),
                    "title_mention_count": len(topic_title_story_sets[theme_key]),
                    "first_mention_count": len(topic_first_story_sets[theme_key]),
                    "theme_share": theme_share,
                    "previous_7d_story_mean": prior_count_mean,
                    "previous_7d_share_mean": prior_share_mean,
                    "heat_index_7d": heat_index,
                    "heat_status": _heat_status(heat_index),
                    "is_main_theme": False,
                    "relevance_sum": relevance_sum,
                    "history_start_date": history_start,
                    "availability_grade": "archive_assumed",
                    "rule_version": RULE_VERSION,
                    "catalog_version": CATALOG_VERSION,
                    "snapshot_version": SNAPSHOT_VERSION,
                }
            )
            active_story_sets[theme_id] = topic_story_sets[theme_key]
            active_member_sets[theme_id] = {member["symbol"] for member in theme_members}

            theme_members.sort(
                key=lambda member: (
                    -member["link_score"],
                    -len(member["story_ids"]),
                    member["quote_code"],
                )
            )
            current_member_rows: list[dict[str, Any]] = []
            for rank, member in enumerate(theme_members, start=1):
                state_key = (theme_id, member["quote_code"])
                previous_member = member_state.get(state_key)
                member_gap = (
                    (day - previous_member["last_member_date"]).days - 1 if previous_member else 0
                )
                if previous_member is None:
                    member_status = "NEW"
                    first_member_date = day
                    member_streak = 1
                else:
                    member_status = "CONTINUING" if member_gap <= CONTINUING_GAP_DAYS else "REVIVED"
                    first_member_date = previous_member["first_member_date"]
                    member_streak = (
                        previous_member["member_streak_days"] + 1 if member_gap == 0 else 1
                    )
                member_state[state_key] = {
                    "first_member_date": first_member_date,
                    "last_member_date": day,
                    "member_streak_days": member_streak,
                }
                member_row = {
                    "evidence_date": day,
                    "as_of_at": as_of_by_date[day],
                    "trade_date": trade_dates[day],
                    "theme_id": theme_id,
                    "theme_label": theme["theme_label"],
                    "quote_code": member["quote_code"],
                    "stock_name": member["stock_name"],
                    "symbol": member["symbol"],
                    "exchange": member["symbol"].split(":", maxsplit=1)[0],
                    "mention_count": member["mention_count"],
                    "unique_story_count": len(member["story_ids"]),
                    "title_mention_count": len(member["title_story_ids"]),
                    "link_score": member["link_score"],
                    "member_weight": (
                        member["link_score"] / relevance_sum if relevance_sum > 0 else 0.0
                    ),
                    "member_rank": rank,
                    "member_status": member_status,
                    "first_member_date": first_member_date,
                    "member_streak_days": member_streak,
                    "member_snapshot_date": day,
                    "is_carried_forward": False,
                    "availability_grade": "archive_assumed",
                    "rule_version": RULE_VERSION,
                    "catalog_version": CATALOG_VERSION,
                    "snapshot_version": SNAPSHOT_VERSION,
                }
                member_rows_for_day.append(member_row)
                current_member_rows.append(member_row)
            latest_member_snapshots[theme_id] = current_member_rows

        season_start = day - timedelta(days=SEASON_WINDOW_DAYS - 1)
        for theme_key, theme in sorted(
            catalog.items(),
            key=lambda item: item[1]["theme_id"],
        ):
            theme_id = theme["theme_id"]
            if theme_key in active_keys or theme["first_seen_date"] is None:
                continue
            season_news_dates = {
                news_day
                for news_day in news_dates_by_theme[theme_key]
                if season_start <= news_day <= day
            }
            if not season_news_dates:
                continue

            last_news_date = max(season_news_dates)
            today_story_count = len(topic_story_sets.get(theme_key, set()))
            today_article_count = len(topic_article_sets.get(theme_key, set()))
            today_share = today_story_count / source_story_count if source_story_count else 0.0
            previous_counts = [
                theme_history.get(
                    (theme_id, day - timedelta(days=offset)),
                    {"theme_count": 0.0},
                )["theme_count"]
                for offset in range(1, 8)
            ]
            previous_shares = [
                theme_history.get(
                    (theme_id, day - timedelta(days=offset)),
                    {"theme_share": 0.0},
                )["theme_share"]
                for offset in range(1, 8)
            ]
            prior_count_mean = statistics.mean(previous_counts)
            prior_share_mean = statistics.mean(previous_shares)
            heat_index = today_share / prior_share_mean * 100 if prior_share_mean > 0 else None
            theme_history[(theme_id, day)] = {
                "theme_count": float(today_story_count),
                "theme_share": today_share,
            }

            previous_state = theme_state[theme_id]
            latest_members = latest_member_snapshots.get(theme_id, [])
            relevance_sum = sum(member["link_score"] for member in latest_members)
            active_rows_for_day.append(
                {
                    "evidence_date": day,
                    "as_of_at": as_of_by_date[day],
                    "trade_date": trade_dates[day],
                    "theme_id": theme_id,
                    "theme_label": theme["theme_label"],
                    "theme_status": "IN_SEASON",
                    "has_news_today": today_article_count > 0,
                    "is_active_today": False,
                    "first_seen_date": previous_state["first_seen_date"],
                    "last_news_date": last_news_date,
                    "count_in_season": len(season_news_dates),
                    "consecutive_news_days": _consecutive_days_ending_on(
                        day,
                        news_dates_by_theme[theme_key],
                    ),
                    "days_since_last_news": (day - last_news_date).days,
                    "inactive_days_before_today": max(
                        (day - previous_state["last_active_date"]).days - 1,
                        0,
                    ),
                    "active_streak_days": 0,
                    "source_count": source_story_count,
                    "theme_count": today_story_count,
                    "article_count": today_article_count,
                    "stock_count": len(latest_members),
                    "title_mention_count": len(topic_title_story_sets.get(theme_key, set())),
                    "first_mention_count": len(topic_first_story_sets.get(theme_key, set())),
                    "theme_share": today_share,
                    "previous_7d_story_mean": prior_count_mean,
                    "previous_7d_share_mean": prior_share_mean,
                    "heat_index_7d": heat_index,
                    "heat_status": _heat_status(heat_index),
                    "is_main_theme": False,
                    "relevance_sum": relevance_sum,
                    "history_start_date": history_start,
                    "availability_grade": "archive_assumed",
                    "rule_version": RULE_VERSION,
                    "catalog_version": CATALOG_VERSION,
                    "snapshot_version": SNAPSHOT_VERSION,
                }
            )
            for member in latest_members:
                carried_member = {
                    **member,
                    "evidence_date": day,
                    "as_of_at": as_of_by_date[day],
                    "trade_date": trade_dates[day],
                    "is_carried_forward": True,
                    "rule_version": RULE_VERSION,
                    "catalog_version": CATALOG_VERSION,
                    "snapshot_version": SNAPSHOT_VERSION,
                }
                member_rows_for_day.append(carried_member)

        active_rows_for_day.sort(
            key=lambda row: (
                not row["is_active_today"],
                -row["theme_count"],
                -row["relevance_sum"],
                row["theme_id"],
            )
        )
        main_theme_rows = [row for row in active_rows_for_day if row["is_active_today"]][:5]
        for row in main_theme_rows:
            row["is_main_theme"] = True
        daily_theme_rows.extend(active_rows_for_day)
        daily_member_rows.extend(member_rows_for_day)

        for left_id, right_id in combinations(sorted(active_story_sets), 2):
            story_intersection = active_story_sets[left_id] & active_story_sets[right_id]
            story_union = active_story_sets[left_id] | active_story_sets[right_id]
            member_intersection = active_member_sets[left_id] & active_member_sets[right_id]
            member_union = active_member_sets[left_id] | active_member_sets[right_id]
            relations = (
                (
                    "CO_MENTION",
                    len(story_intersection),
                    len(story_intersection) / len(story_union) if story_union else 0.0,
                ),
                (
                    "MEMBER_OVERLAP",
                    len(member_intersection),
                    len(member_intersection) / len(member_union) if member_union else 0.0,
                ),
            )
            for relation_type, connection_count, jaccard_score in relations:
                if connection_count == 0:
                    continue
                daily_relation_rows.append(
                    {
                        "evidence_date": day,
                        "as_of_at": as_of_by_date[day],
                        "trade_date": trade_dates[day],
                        "source_theme_id": left_id,
                        "target_theme_id": right_id,
                        "relation_type": relation_type,
                        "window": "1D",
                        "lag_minutes": None,
                        "connection_count": connection_count,
                        "jaccard_score": jaccard_score,
                        "availability_grade": "archive_assumed",
                        "rule_version": RULE_VERSION,
                        "catalog_version": CATALOG_VERSION,
                        "snapshot_version": SNAPSHOT_VERSION,
                    }
                )

        for row in topic_rows_for_day:
            if (
                row["theme_key"] not in catalog
                or catalog[row["theme_key"]]["first_seen_date"] is None
            ):
                continue
            article_theme_rows.append(
                {
                    "article_id": row["article_id"],
                    "story_id": row["story_id"],
                    "published_date": row["published_date"],
                    "theme_id": catalog[row["theme_key"]]["theme_id"],
                    "theme_label": catalog[row["theme_key"]]["theme_label"],
                    "raw_keyword": row["raw_keyword"],
                    "is_title_mention": row["is_title_mention"],
                    "is_first_mention": row["is_first_mention"],
                    "rule_version": RULE_VERSION,
                }
            )
        for evidence in provisional_evidence.values():
            if (
                evidence["theme_key"] not in catalog
                or catalog[evidence["theme_key"]]["first_seen_date"] is None
            ):
                continue
            theme_id = catalog[evidence["theme_key"]]["theme_id"]
            evidence_rows.append(
                {
                    "evidence_date": day,
                    "article_id": evidence["article_id"],
                    "story_id": evidence["story_id"],
                    "theme_id": theme_id,
                    "quote_code": evidence["quote_code"],
                    "stock_name": evidence["stock_name"],
                    "symbol": evidence["symbol"],
                    "evidence_type": evidence["evidence_type"],
                    "evidence_text": evidence["evidence_text"],
                    "mention_count": evidence["mention_count"],
                    "is_title_mention": evidence["is_title_mention"],
                    "raw_score": evidence["raw_score"],
                    "dilution": evidence["dilution"],
                    "story_score": evidence["story_score"],
                    "qualifies_as_member": (
                        evidence["theme_key"],
                        evidence["symbol"],
                    )
                    in qualified_member_keys,
                    "rule_version": RULE_VERSION,
                }
            )

        max_published = max(
            (article["published_at"] for article in day_articles),
            default=None,
        )
        if max_published is not None and max_published > as_of_by_date[day]:
            raise AssertionError(f"future article leaked into {day}")
        day_audit.append(
            {
                "evidence_date": day.isoformat(),
                "as_of_at": as_of_by_date[day].isoformat(),
                "max_source_published_at": (
                    max_published.isoformat() if max_published is not None else None
                ),
                "source_story_count": source_story_count,
                "candidate_count": len(topic_story_sets),
                "active_theme_count": len(active_keys),
                "state_cutoff": (day - timedelta(days=1)).isoformat(),
            }
        )

    catalog_rows = []
    for theme in sorted(catalog.values(), key=lambda row: row["theme_id"]):
        catalog_rows.append({key: value for key, value in theme.items() if key != "aliases"})

    audit = {
        "rule_version": RULE_VERSION,
        "catalog_version": CATALOG_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
        "history_start_date": history_start.isoformat(),
        "processing_order": "evidence_date_ascending",
        "future_data_policy": (
            "Snapshot D uses articles published at or before D as_of_at and state through D-1."
        ),
        "model_used": False,
        "thresholds": {
            "minimum_theme_stories": MIN_THEME_STORIES,
            "minimum_theme_stocks": MIN_THEME_STOCKS,
            "minimum_title_stories": 0,
            "minimum_member_score": MIN_MEMBER_SCORE,
            "continuing_gap_days": CONTINUING_GAP_DAYS,
            "season_window_calendar_days": SEASON_WINDOW_DAYS,
        },
        "dates": day_audit,
    }
    return ThemeBuildResult(
        article_themes=article_theme_rows,
        evidence=evidence_rows,
        daily_themes=daily_theme_rows,
        daily_members=daily_member_rows,
        daily_relations=daily_relation_rows,
        theme_catalog=catalog_rows,
        theme_aliases=sorted(
            aliases.values(),
            key=lambda row: (row["theme_id"], row["normalized_alias"]),
        ),
        audit=audit,
    )
