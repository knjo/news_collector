"""Point-in-time broad themes with rolling, news-derived stock membership."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from math import log1p, sqrt
from typing import Any

import polars as pl

from news_collector.analysis.theme_taxonomy import is_excluded_theme_label
from news_collector.analysis.topics import normalized_text

BROAD_RULE_VERSION = "broad-theme-v3"
BROAD_SNAPSHOT_VERSION = "cnyes-broad-v3"

NEWS_WINDOW_DAYS = 60
MEMBER_WINDOW_DAYS = 365
MIN_MEMBER_DAYS = 2
MIN_MEMBER_SCORE = 4.0
MIN_STRONG_STORIES = 1
MIN_SUBTHEME_MEMBERS = 5

BROAD_DAILY_SCHEMA = {
    "evidence_date": pl.Date,
    "trade_date": pl.Date,
    "theme_id": pl.UInt32,
    "theme_name": pl.String,
    "news_days_60d": pl.UInt32,
    "is_new_theme": pl.Boolean,
    "days_since_last_news": pl.UInt32,
    "member_count_change": pl.Int32,
    "quote_code": pl.String,
    "stock_name": pl.String,
    "membership_score": pl.Float64,
    "is_new_member": pl.Boolean,
}


@dataclass(frozen=True)
class BroadTheme:
    theme_id: int
    theme_name: str
    source_labels: tuple[str, ...]
    parent_theme_id: int | None = None


# This taxonomy contains semantic label-to-parent relationships only. It never
# contains a stock list, price outcome, or the hindsight evaluation universe.
BROAD_THEMES = (
    BroadTheme(
        1,
        "記憶體",
        ("記憶體", "DRAM", "DDR4", "DDR5", "HBM", "NAND", "SSD"),
    ),
    BroadTheme(
        2,
        "PCB",
        (
            "PCB",
            "CCL",
            "銅箔基板",
            "玻纖布",
            "軟板",
            "HDI",
            "載板",
            "ABF",
            "ABF載板",
            "BT載板",
            "IC載板",
        ),
    ),
    BroadTheme(
        3,
        "AI伺服器",
        ("AI伺服器", "伺服器", "機殼", "GB200", "GB300"),
    ),
    BroadTheme(4, "CPO與光通訊", ("CPO／矽光子", "光通訊")),
    BroadTheme(5, "散熱", ("散熱", "水冷", "液冷")),
    BroadTheme(6, "機器人", ("機器人", "自動化")),
    BroadTheme(7, "軍工", ("軍工", "無人機", "國防", "航太")),
    BroadTheme(8, "低軌衛星", ("低軌衛星", "LEO")),
    BroadTheme(9, "網通", ("網通", "5G", "WiFi 8")),
    BroadTheme(10, "面板", ("面板",)),
    BroadTheme(11, "被動元件", ("被動元件",)),
    BroadTheme(12, "半導體設備", ("設備", "檢測")),
    BroadTheme(13, "先進封裝", ("先進封裝", "COWOS", "3DIC")),
    BroadTheme(14, "ASIC與矽智財", ("ASIC", "矽智財", "IP")),
    BroadTheme(15, "電源與BBU", ("電源", "BBU", "變壓器")),
    BroadTheme(
        16,
        "功率半導體",
        ("功率元件", "成熟製程", "砷化鎵", "碳化矽", "氮化鎵", "Navitas"),
    ),
    BroadTheme(17, "航運", ("航運", "海運", "散裝")),
    BroadTheme(18, "矽晶圓", ("矽晶圓", "研磨")),
)

# A subtheme is only published on dates where its own rolling evidence produces
# at least MIN_SUBTHEME_MEMBERS qualified stocks. These are semantic text
# relationships, not stock constituent lists.
SUBTHEMES = (
    BroadTheme(19, "DRAM", ("DRAM",), 1),
    BroadTheme(20, "DDR4", ("DDR4",), 1),
    BroadTheme(21, "DDR5", ("DDR5",), 1),
    BroadTheme(22, "HBM", ("HBM",), 1),
    BroadTheme(23, "NAND", ("NAND",), 1),
    BroadTheme(24, "SSD", ("SSD",), 1),
    BroadTheme(25, "CCL", ("CCL", "銅箔基板"), 2),
    BroadTheme(26, "玻纖布", ("玻纖布",), 2),
    BroadTheme(27, "軟板", ("軟板",), 2),
    BroadTheme(28, "HDI", ("HDI",), 2),
    BroadTheme(29, "載板", ("載板",), 2),
    BroadTheme(30, "ABF載板", ("ABF", "ABF載板"), 2),
    BroadTheme(31, "BT載板", ("BT載板",), 2),
    BroadTheme(32, "IC載板", ("IC載板",), 2),
    BroadTheme(33, "伺服器", ("伺服器",), 3),
    BroadTheme(34, "伺服器機殼", ("機殼",), 3),
    BroadTheme(35, "GB200", ("GB200",), 3),
    BroadTheme(36, "GB300", ("GB300",), 3),
    BroadTheme(37, "CPO／矽光子", ("CPO／矽光子",), 4),
    BroadTheme(38, "光通訊", ("光通訊",), 4),
    BroadTheme(39, "水冷／液冷", ("水冷", "液冷"), 5),
    BroadTheme(40, "自動化", ("自動化",), 6),
    BroadTheme(41, "無人機", ("無人機",), 7),
    BroadTheme(42, "國防", ("國防",), 7),
    BroadTheme(43, "航太", ("航太",), 7),
    BroadTheme(44, "5G", ("5G",), 9),
    BroadTheme(45, "WiFi 8", ("WiFi 8",), 9),
    BroadTheme(46, "半導體檢測", ("檢測",), 12),
    BroadTheme(47, "CoWoS", ("COWOS",), 13),
    BroadTheme(48, "3DIC", ("3DIC",), 13),
    BroadTheme(49, "矽智財", ("矽智財", "IP"), 14),
    BroadTheme(50, "BBU", ("BBU",), 15),
    BroadTheme(51, "變壓器", ("變壓器",), 15),
    BroadTheme(52, "成熟製程", ("成熟製程",), 16),
    BroadTheme(53, "砷化鎵", ("砷化鎵",), 16),
    BroadTheme(54, "碳化矽", ("碳化矽",), 16),
    BroadTheme(55, "氮化鎵", ("氮化鎵",), 16),
    BroadTheme(56, "Navitas", ("Navitas",), 16),
    BroadTheme(57, "海運", ("海運",), 17),
    BroadTheme(58, "散裝航運", ("散裝",), 17),
    BroadTheme(59, "研磨", ("研磨",), 18),
)

FIXED_BROAD_THEMES = BROAD_THEMES + SUBTHEMES

# Semantic exclusions for labels that are not reusable product/industry groups.
# These rules never use returns, a stock constituent list, or the hindsight file.
_BROAD_EXCLUDED_LABELS = {
    "0403強震",
    "232條款",
    "AWS",
    "BDI",
    "CB",
    "CDP",
    "CES",
    "DeepSeek",
    "GDP",
    "IPO",
    "ITF",
    "MWC",
    "NVIDIA",
    "OpenAI",
    "SCFI",
    "SEMICON",
    "TPCA",
    "TSIA年會",
    "Touch Taiwan",
    "上櫃掛牌",
    "上市掛牌",
    "中秋",
    "人才",
    "人民幣",
    "企業環保獎",
    "併購",
    "借券賣出",
    "債券",
    "公司治理評鑑",
    "出國",
    "創新板",
    "升值",
    "半導體展",
    "台北國際電玩展",
    "台大",
    "台幣",
    "台灣永續投資獎",
    "合併",
    "國家企業環保獎",
    "國家品牌玉山獎",
    "增資",
    "川普",
    "庫存",
    "庫藏股",
    "強震",
    "徵才",
    "成分股",
    "戰爭",
    "投資",
    "承銷價",
    "護盤",
    "掛牌",
    "擴產",
    "收購",
    "新台幣",
    "新股掛牌",
    "旅展",
    "智慧能源周",
    "智慧能源週",
    "智慧顯示展",
    "東京電玩展",
    "毛利率",
    "民調",
    "美元",
    "美伊戰事",
    "美債",
    "聯準會",
    "聯貸",
    "自動化展",
    "自結",
    "自結盈餘",
    "董事",
    "董事改選",
    "董監事",
    "蛇年",
    "蛇年封關",
    "觀光展",
    "訂單",
    "調漲",
    "賑災",
    "資本公積",
    "資本支出",
    "資產活化",
    "資金",
    "資籌",
    "賣出",
    "配息",
    "醫療科技展",
    "金價",
    "降息",
    "非洲豬瘟",
    "高息",
    "高股息",
    "焦點股",
    "盈餘",
    "稅後純益",
    "股息",
    "缺貨",
    "績效",
    "虧損",
    "競拍",
    "籌資",
    "私募",
    "興櫃",
    "萬七",
    "萬金股",
    "注意股",
    "原物料",
    "匯率",
    "基金",
    "台股ETF",
    "地震",
    "封關",
    "業績發表",
    "業績發表會",
    "景氣",
    "經營權",
    "罷工",
    "航班",
    "機票",
    "銅價",
    "布蘭特原油",
    "電價調漲",
    "郭台銘",
    "總統",
    "俄烏",
    "SBTI",
    "Vanmoof",
    "djsi",
    "ESG共同推動",
    "三菱",
    "中國信託",
    "中華隊",
    "亞洲生技大會",
    "京元電",
    "人力銀行",
    "代理",
    "以色列",
    "伊朗",
    "兆豐銀行",
    "光寶",
    "凱基金",
    "北美",
    "兩岸",
    "台塑集團",
    "台新新光金",
    "台灣之星",
    "合庫銀行",
    "墨西哥",
    "安瑞",
    "定穎",
    "巨蛋",
    "捐款",
    "星宇",
    "智慧城市展",
    "港股",
    "材料",
    "東京",
    "棒球",
    "熊本",
    "瑞信",
    "信保基金",
    "矽品",
    "納智捷",
    "經寶",
    "緬甸",
    "臻鼎",
    "華為",
    "虎航",
    "虎門",
    "車展",
    "車用電子展",
    "金仁寶集團",
    "金融科技產業聯盟",
    "錼創",
    "蜜月行情",
    "阿里山",
    "騰輝",
    "高端",
    "鴻華先進",
}
_NORMALIZED_BROAD_EXCLUDED_LABELS = {normalized_text(label) for label in _BROAD_EXCLUDED_LABELS}
_BROAD_EXCLUDED_SUFFIXES = ("大會", "年會", "發表會", "展", "獎")


@dataclass
class _DailyEvidence:
    evidence_date: date
    score: float
    strong_story_count: int
    stock_name: str


def _is_dynamic_root_label(label: str) -> bool:
    normalized = normalized_text(label)
    return bool(
        normalized
        and normalized not in _NORMALIZED_BROAD_EXCLUDED_LABELS
        and not is_excluded_theme_label(label)
        and "關稅" not in label
        and not label.endswith(_BROAD_EXCLUDED_SUFFIXES)
    )


def broad_theme_definitions(
    theme_catalog: list[dict[str, Any]],
) -> tuple[BroadTheme, ...]:
    """Return fixed semantic groups plus unlimited eligible source-label roots."""
    definitions = list(FIXED_BROAD_THEMES)
    mapped_labels = {
        normalized_text(label) for theme in FIXED_BROAD_THEMES for label in theme.source_labels
    }
    seen_labels = set(mapped_labels)
    next_theme_id = max(theme.theme_id for theme in definitions) + 1
    for source_theme in sorted(
        theme_catalog,
        key=lambda row: (
            str(row.get("first_seen_date") or row.get("created_date") or "9999-12-31"),
            normalized_text(str(row["theme_label"])),
            int(row["theme_id"]),
        ),
    ):
        label = str(source_theme["theme_label"]).strip()
        normalized = normalized_text(label)
        if not label or normalized in seen_labels or not _is_dynamic_root_label(label):
            continue
        definitions.append(
            BroadTheme(
                theme_id=next_theme_id,
                theme_name=label,
                source_labels=(label,),
            )
        )
        seen_labels.add(normalized)
        next_theme_id += 1
    return tuple(definitions)


def _source_label_targets(
    output_themes: tuple[BroadTheme, ...],
) -> dict[str, tuple[BroadTheme, ...]]:
    result: dict[str, list[BroadTheme]] = defaultdict(list)
    root_by_id = {theme.theme_id: theme for theme in output_themes if theme.parent_theme_id is None}
    for broad_theme in root_by_id.values():
        for label in broad_theme.source_labels:
            normalized = normalized_text(label)
            if result[normalized]:
                raise ValueError(f"broad source label is assigned to two parents: {label}")
            result[normalized].append(broad_theme)
    for subtheme in (theme for theme in output_themes if theme.parent_theme_id is not None):
        parent = root_by_id.get(subtheme.parent_theme_id or -1)
        if parent is None:
            raise ValueError(f"subtheme has unknown parent: {subtheme.theme_name}")
        for label in subtheme.source_labels:
            if normalized_text(label) not in {
                normalized_text(parent_label) for parent_label in parent.source_labels
            }:
                raise ValueError(
                    f"subtheme label {label} is not assigned to parent {parent.theme_name}"
                )
            result[normalized_text(label)].append(subtheme)
    return {label: tuple(themes) for label, themes in result.items()}


def build_broad_daily_members(
    *,
    article_themes: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    theme_catalog: list[dict[str, Any]],
    analysis_dates: list[date],
    trade_dates: dict[date, date],
) -> list[dict[str, Any]]:
    """Build one wide `(date, broad theme, stock)` table without future evidence."""

    if not analysis_dates:
        return []

    output_themes = broad_theme_definitions(theme_catalog)
    source_label_targets = _source_label_targets(output_themes)
    source_labels = {int(theme["theme_id"]): str(theme["theme_label"]) for theme in theme_catalog}

    news_stories_by_day: dict[date, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in article_themes:
        source_label = normalized_text(str(row["theme_label"]))
        for broad_theme in source_label_targets.get(source_label, ()):
            news_stories_by_day[row["published_date"]][broad_theme.theme_id].add(
                str(row["story_id"])
            )

    strongest_story_evidence: dict[
        tuple[date, int, str, str],
        dict[str, Any],
    ] = {}
    for row in evidence:
        source_label = source_labels.get(int(row["theme_id"]))
        normalized_source_label = normalized_text(source_label or "")
        for broad_theme in source_label_targets.get(normalized_source_label, ()):
            key = (
                row["evidence_date"],
                broad_theme.theme_id,
                str(row["quote_code"]),
                str(row["story_id"]),
            )
            previous = strongest_story_evidence.get(key)
            if previous is None or float(row["story_score"]) > float(previous["story_score"]):
                strongest_story_evidence[key] = row

    daily_evidence: dict[
        date,
        dict[tuple[int, str], dict[str, Any]],
    ] = defaultdict(dict)
    for (
        evidence_date,
        broad_theme_id,
        quote_code,
        _,
    ), row in strongest_story_evidence.items():
        key = (broad_theme_id, quote_code)
        value = daily_evidence[evidence_date].setdefault(
            key,
            {
                "score": 0.0,
                "strong_story_count": 0,
                "stock_name": str(row["stock_name"]),
            },
        )
        value["score"] += float(row["story_score"])
        value["strong_story_count"] += int(float(row["story_score"]) >= 2.0)

    rolling_evidence: dict[
        tuple[int, str],
        deque[_DailyEvidence],
    ] = defaultdict(deque)
    news_dates: dict[int, deque[date]] = defaultdict(deque)
    first_theme_date: dict[int, date] = {}
    first_member_date: dict[tuple[int, str], date] = {}
    previous_members: dict[int, set[str]] = defaultdict(set)
    output_rows: list[dict[str, Any]] = []
    broad_by_id = {theme.theme_id: theme for theme in output_themes}

    for day in sorted(analysis_dates):
        for theme_id, stories in news_stories_by_day.get(day, {}).items():
            if stories:
                news_dates[theme_id].append(day)

        news_cutoff = day - timedelta(days=NEWS_WINDOW_DAYS - 1)
        for dates in news_dates.values():
            while dates and dates[0] < news_cutoff:
                dates.popleft()

        for key, value in daily_evidence.get(day, {}).items():
            rolling_evidence[key].append(
                _DailyEvidence(
                    evidence_date=day,
                    score=float(value["score"]),
                    strong_story_count=int(value["strong_story_count"]),
                    stock_name=str(value["stock_name"]),
                )
            )

        member_cutoff = day - timedelta(days=MEMBER_WINDOW_DAYS - 1)
        for values in rolling_evidence.values():
            while values and values[0].evidence_date < member_cutoff:
                values.popleft()

        accumulated: dict[tuple[int, str], dict[str, Any]] = {}
        stock_total_scores: dict[str, float] = defaultdict(float)
        for key, values in rolling_evidence.items():
            if not values:
                continue
            score = sum(value.score for value in values)
            accumulated[key] = {
                "score": score,
                "evidence_days": len(values),
                "strong_story_count": sum(value.strong_story_count for value in values),
                "stock_name": values[-1].stock_name,
            }
            stock_total_scores[key[1]] += score

        candidates_by_theme: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for (theme_id, quote_code), value in accumulated.items():
            if (
                value["evidence_days"] < MIN_MEMBER_DAYS
                or value["score"] < MIN_MEMBER_SCORE
                or value["strong_story_count"] < MIN_STRONG_STORIES
            ):
                continue
            total_score = stock_total_scores[quote_code]
            affinity = value["score"] / total_score if total_score > 0 else 0.0
            rank_score = value["score"] * (1.0 + log1p(value["evidence_days"])) * sqrt(affinity)
            candidates_by_theme[theme_id].append(
                {
                    "quote_code": quote_code,
                    "stock_name": value["stock_name"],
                    "rank_score": rank_score,
                    "evidence_days": value["evidence_days"],
                }
            )

        current_members: dict[int, list[dict[str, Any]]] = {}
        for theme_id, candidates in candidates_by_theme.items():
            if not news_dates[theme_id]:
                continue
            theme = broad_by_id[theme_id]
            if theme.parent_theme_id is not None and len(candidates) < MIN_SUBTHEME_MEMBERS:
                continue
            candidates.sort(
                key=lambda row: (
                    -row["rank_score"],
                    -row["evidence_days"],
                    row["quote_code"],
                )
            )
            current_members[theme_id] = candidates

        all_theme_ids = set(previous_members) | set(current_members)
        for theme_id in all_theme_ids:
            theme = broad_by_id[theme_id]
            members = current_members.get(theme_id, [])
            member_codes = {str(member["quote_code"]) for member in members}
            is_first_theme_day = theme_id not in first_theme_date and bool(members)
            if is_first_theme_day:
                first_theme_date[theme_id] = day
            count_change = (
                0
                if is_first_theme_day
                else len(member_codes) - len(previous_members.get(theme_id, set()))
            )

            if members:
                maximum_score = max(float(member["rank_score"]) for member in members)
                last_news_date = news_dates[theme_id][-1]
                for member in members:
                    member_key = (theme_id, str(member["quote_code"]))
                    is_new_member = member_key not in first_member_date
                    if is_new_member:
                        first_member_date[member_key] = day
                    output_rows.append(
                        {
                            "evidence_date": day,
                            "trade_date": trade_dates[day],
                            "theme_id": theme_id,
                            "theme_name": theme.theme_name,
                            "news_days_60d": len(news_dates[theme_id]),
                            "is_new_theme": is_first_theme_day,
                            "days_since_last_news": (day - last_news_date).days,
                            "member_count_change": count_change,
                            "quote_code": member["quote_code"],
                            "stock_name": member["stock_name"],
                            "membership_score": round(
                                float(member["rank_score"]) / maximum_score * 100.0,
                                6,
                            ),
                            "is_new_member": is_new_member,
                        }
                    )
            previous_members[theme_id] = member_codes

    return output_rows
