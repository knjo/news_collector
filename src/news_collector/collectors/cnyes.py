"""Collector and normalizer for Anue (Cnyes) Taiwan stock news."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

CNYES_API_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news"
CNYES_ARTICLE_URL = "https://news.cnyes.com/news/id/{news_id}"
TAIPEI = ZoneInfo("Asia/Taipei")
PAGE_SIZE = 30

_ANNOUNCEMENT_PATTERNS = (
    re.compile(r"公告本公司"),
    re.compile(r"代(?:重要)?子公司.*公告"),
    re.compile(r"更正本公司"),
    re.compile(r"股票面額.*公告"),
    re.compile(r"公告期間"),
)
_MARKET_RECAP_PATTERNS = (
    re.compile(r"〈台股盤(?:前|中|後)〉"),
    re.compile(r"台股盤前要聞"),
    re.compile(r"台股盤勢"),
)
_RESEARCH_PATTERNS = (
    re.compile(r"鉅亨研報"),
    re.compile(r"優分析"),
)
_AUTOMATED_FINANCIAL_PATTERNS = (re.compile(r"^營收速報"),)
_MARKET_TICK_PATTERNS = (re.compile(r"^盤中速報\s*[-－]"),)


def taipei_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    """Return the inclusive Asia/Taipei bounds for a calendar date."""
    start = datetime.combine(target_date, datetime_time.min, tzinfo=TAIPEI)
    end = datetime.combine(target_date, datetime_time.max, tzinfo=TAIPEI)
    return start, end


def clean_html_text(value: str | None) -> str:
    """Decode the API's escaped HTML and return normalized plain text."""
    if not value:
        return ""
    decoded = html.unescape(value)
    soup = BeautifulSoup(decoded, "html.parser")
    return "\n".join(
        line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()
    )


def clean_label(value: object) -> str:
    """Remove source-side invisible markers from a short label."""
    return str(value or "").replace("\ufeff", "").strip()


def classify_article(item: dict[str, Any]) -> str:
    """Classify content so non-editorial material can be excluded transparently."""
    title = clean_label(item.get("title"))
    source = clean_label(item.get("source"))
    keywords = {clean_label(keyword) for keyword in item.get("keyword") or []}
    if item.get("isOutsource") == 1 or source:
        return "partner"
    if any(pattern.search(title) for pattern in _RESEARCH_PATTERNS) or any(
        keyword.endswith("投顧") for keyword in keywords
    ):
        return "sponsored_or_research"
    if any(pattern.search(title) for pattern in _AUTOMATED_FINANCIAL_PATTERNS):
        return "automated_financial"
    if any(pattern.search(title) for pattern in _MARKET_TICK_PATTERNS):
        return "market_tick"
    if any(pattern.search(title) for pattern in _ANNOUNCEMENT_PATTERNS):
        return "company_announcement"
    if any(pattern.search(title) for pattern in _MARKET_RECAP_PATTERNS):
        return "market_recap"
    return "original_news"


def _content_hash(title: str, summary: str, body_text: str) -> str:
    material = "\n".join((title.strip(), summary.strip(), body_text.strip()))
    return hashlib.sha256(material.encode()).hexdigest()


def normalize_item(
    item: dict[str, Any],
    *,
    fetched_at: datetime,
    raw_response_file: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize one API item into article, keyword, and stock records."""
    news_id = int(item["newsId"])
    title = clean_label(item.get("title"))
    summary = clean_html_text(str(item.get("summary") or ""))
    body_text = clean_html_text(str(item.get("content") or ""))
    published_at = datetime.fromtimestamp(int(item["publishAt"]), tz=UTC).astimezone(TAIPEI)
    article_type = classify_article(item)
    article_id = f"cnyes:{news_id}"

    article = {
        "article_id": article_id,
        "source_article_id": news_id,
        "canonical_url": CNYES_ARTICLE_URL.format(news_id=news_id),
        "source": "cnyes",
        "content_provider": clean_label(item.get("source")) or None,
        "category_id": int(item["categoryId"]) if item.get("categoryId") is not None else None,
        "category_name": str(item.get("categoryName") or ""),
        "title": title,
        "summary": summary,
        "body_text": body_text,
        "published_at": published_at,
        "published_date": published_at.date(),
        "fetched_at": fetched_at.astimezone(TAIPEI),
        "availability_grade": "archive_assumed",
        "article_type": article_type,
        "is_outsource": bool(item.get("isOutsource")),
        "included_in_analysis": article_type in {"original_news", "market_recap"},
        "content_hash": _content_hash(title, summary, body_text),
        "raw_response_file": raw_response_file,
        "story_id": article_id,
        "is_story_representative": True,
    }

    keywords: list[dict[str, Any]] = []
    seen_keywords: set[str] = set()
    for position, raw_keyword in enumerate(item.get("keyword") or []):
        keyword = clean_label(raw_keyword)
        if not keyword or keyword.casefold() in seen_keywords:
            continue
        seen_keywords.add(keyword.casefold())
        keywords.append(
            {
                "article_id": article_id,
                "keyword": keyword,
                "keyword_position": position,
                "is_title_mention": keyword.casefold() in title.casefold(),
            }
        )

    stocks: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for market in item.get("market") or []:
        symbol = clean_label(market.get("symbol"))
        code = clean_label(market.get("code"))
        if not symbol or not code or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        exchange = symbol.split(":", maxsplit=1)[0]
        stocks.append(
            {
                "article_id": article_id,
                "symbol": symbol,
                "exchange": exchange,
                "quote_code": code,
                "stock_name": clean_label(market.get("name")),
            }
        )

    return article, keywords, stocks


class CnyesClient:
    """Small, rate-limited client for the public web application's news endpoint."""

    def __init__(self, *, timeout_seconds: float, user_agent: str, delay_seconds: float = 0.3):
        transport = httpx.HTTPTransport(retries=2)
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "Referer": "https://news.cnyes.com/",
                "User-Agent": user_agent,
            },
        )
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CnyesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def collect(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        raw_dir: Path,
    ) -> Iterator[tuple[dict[str, Any], str]]:
        """Yield API items and raw pages, slicing requests by Taipei calendar day."""
        raw_dir.mkdir(parents=True, exist_ok=True)
        window_start = start_at.astimezone(TAIPEI)

        while window_start <= end_at:
            _, day_end = taipei_day_bounds(window_start.date())
            window_end = min(day_end, end_at)
            daily_raw_dir = raw_dir / f"published_date={window_start.date().isoformat()}"
            daily_raw_dir.mkdir(parents=True, exist_ok=True)
            page = 1
            last_page = 1

            while page <= last_page:
                params = {
                    "startAt": int(window_start.timestamp()),
                    "endAt": int(window_end.timestamp()),
                    "limit": PAGE_SIZE,
                    "page": page,
                }
                response = self._client.get(CNYES_API_URL, params=params)
                response.raise_for_status()
                payload = response.json()
                if payload.get("statusCode") != 200:
                    raise RuntimeError(
                        f"Cnyes API error: {payload.get('message', 'unknown error')}"
                    )

                raw_file = daily_raw_dir / f"page-{page:04d}.json.gz"
                with gzip.open(raw_file, "wt", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))

                items = payload.get("items") or {}
                last_page = int(items.get("last_page") or 1)
                raw_reference = str(raw_file)
                for item in items.get("data") or []:
                    yield item, raw_reference

                page += 1
                if page <= last_page and self.delay_seconds > 0:
                    time.sleep(self.delay_seconds)

            window_start = window_end + timedelta(microseconds=1)
            if window_start <= end_at and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
