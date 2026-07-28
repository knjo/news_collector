"""Shared text normalization and same-source story deduplication."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import polars as pl

_PUNCTUATION = re.compile(r"[\W_]+", re.UNICODE)


def normalized_text(value: str) -> str:
    return _PUNCTUATION.sub("", value).casefold()


def _jaccard_bigrams(left: str, right: str) -> float:
    def bigrams(value: str) -> set[str]:
        normalized = normalized_text(value)
        return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}

    left_tokens = bigrams(left)
    right_tokens = bigrams(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


class _UnionFind:
    def __init__(self, values: list[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def assign_story_ids(
    articles: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
) -> None:
    """Assign deterministic IDs using exact and conservative same-day deduplication."""
    stock_sets: dict[str, set[str]] = defaultdict(set)
    for stock in stocks:
        stock_sets[stock["article_id"]].add(stock["symbol"])

    included = [article for article in articles if article["included_in_analysis"]]
    union_find = _UnionFind([article["article_id"] for article in included])
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for article in included:
        by_date[article["published_date"]].append(article)

    for daily_articles in by_date.values():
        for index, left in enumerate(daily_articles):
            for right in daily_articles[index + 1 :]:
                if left["content_hash"] == right["content_hash"]:
                    union_find.union(left["article_id"], right["article_id"])
                    continue
                if not stock_sets[left["article_id"]] & stock_sets[right["article_id"]]:
                    continue
                title_similarity = _jaccard_bigrams(left["title"], right["title"])
                if title_similarity >= 0.82:
                    union_find.union(left["article_id"], right["article_id"])
                    continue
                if title_similarity < 0.62:
                    continue
                summary_similarity = SequenceMatcher(
                    None,
                    normalized_text(left["summary"]),
                    normalized_text(right["summary"]),
                    autojunk=False,
                ).ratio()
                if summary_similarity >= 0.84:
                    union_find.union(left["article_id"], right["article_id"])

    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in included:
        clusters[union_find.find(article["article_id"])].append(article)

    for cluster in clusters.values():
        representative = min(cluster, key=lambda article: article["source_article_id"])
        story_id = f"cnyes-story:{representative['source_article_id']}"
        for article in cluster:
            article["story_id"] = story_id
            article["is_story_representative"] = article is representative


def dataframe(
    records: list[dict[str, Any]],
    schema: dict[str, pl.DataType],
) -> pl.DataFrame:
    """Build a stable-schema DataFrame, including for an empty result."""
    if not records:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(records, schema_overrides=schema)
