"""Versioned, conservative manual taxonomy rules for formal market themes."""

from __future__ import annotations

from dataclasses import dataclass

from news_collector.analysis.topics import normalized_text

TAXONOMY_VERSION = "theme-taxonomy-v1"


@dataclass(frozen=True)
class ThemeTaxonomyRule:
    source_label: str
    action: str
    category: str
    reason: str
    applies_from: str
    reviewed_on: str = "2026-07-27"


# These exclusions are based only on the label's semantic type, never on returns,
# strategy performance, or future membership overlap. Raw article keywords remain
# available even when a label is excluded from the formal theme universe.
THEME_TAXONOMY_RULES = (
    ThemeTaxonomyRule("GTC", "EXCLUDE", "EVENT", "產業活動，不是商品族群", "1900-01-01"),
    ThemeTaxonomyRule(
        "COMPUTEX",
        "EXCLUDE",
        "EVENT",
        "產業活動，不是商品族群",
        "1900-01-01",
    ),
    ThemeTaxonomyRule("黃仁勳", "EXCLUDE", "PERSON", "人物名稱", "1900-01-01"),
    ThemeTaxonomyRule("SpaceX", "EXCLUDE", "ENTITY", "單一企業名稱", "1900-01-01"),
    ThemeTaxonomyRule("工商協進會", "EXCLUDE", "ORGANIZATION", "組織名稱", "1900-01-01"),
    ThemeTaxonomyRule("三星", "EXCLUDE", "ENTITY", "單一企業名稱", "1900-01-01"),
    ThemeTaxonomyRule("現增", "EXCLUDE", "CORPORATE_ACTION", "公司籌資行為", "1900-01-01"),
    ThemeTaxonomyRule("股東會", "EXCLUDE", "CORPORATE_ACTION", "公司治理事件", "1900-01-01"),
    ThemeTaxonomyRule("營收創高", "EXCLUDE", "FINANCIAL_METRIC", "財務表現描述", "1900-01-01"),
    ThemeTaxonomyRule("獲利創高", "EXCLUDE", "FINANCIAL_METRIC", "財務表現描述", "1900-01-01"),
    ThemeTaxonomyRule("股利政策", "EXCLUDE", "CORPORATE_ACTION", "股利決策", "1900-01-01"),
    ThemeTaxonomyRule("股票股利", "EXCLUDE", "CORPORATE_ACTION", "股利形式", "1900-01-01"),
    ThemeTaxonomyRule("自結損益", "EXCLUDE", "FINANCIAL_METRIC", "財務揭露形式", "1900-01-01"),
    ThemeTaxonomyRule("處置股", "EXCLUDE", "MARKET_MECHANIC", "交易制度狀態", "1900-01-01"),
    ThemeTaxonomyRule("全額交割", "EXCLUDE", "MARKET_MECHANIC", "交易制度狀態", "1900-01-01"),
    ThemeTaxonomyRule("融資", "EXCLUDE", "MARKET_MECHANIC", "交易籌碼指標", "1900-01-01"),
    ThemeTaxonomyRule("MSCI", "EXCLUDE", "MARKET_MECHANIC", "指數調整事件", "1900-01-01"),
    ThemeTaxonomyRule("台股暴跌", "EXCLUDE", "MARKET_REGIME", "整體市場狀態", "1900-01-01"),
    ThemeTaxonomyRule("上市公司", "EXCLUDE", "GENERIC", "過度通用分類", "1900-01-01"),
    ThemeTaxonomyRule("供應鏈", "EXCLUDE", "GENERIC", "缺乏產業辨識力", "1900-01-01"),
    ThemeTaxonomyRule("運價", "EXCLUDE", "DRIVER", "價格指標，不是商品族群", "1900-01-01"),
    ThemeTaxonomyRule("漲價", "EXCLUDE", "DRIVER", "價格事件，不是商品族群", "1900-01-01"),
    ThemeTaxonomyRule("颱風", "EXCLUDE", "EVENT", "外生事件，不是商品族群", "1900-01-01"),
    ThemeTaxonomyRule("苯駢芘", "EXCLUDE", "EVENT", "食品安全事件，不是商品族群", "1900-01-01"),
    ThemeTaxonomyRule("美伊戰爭", "EXCLUDE", "EVENT", "地緣政治事件", "1900-01-01"),
    ThemeTaxonomyRule("中東戰爭", "EXCLUDE", "EVENT", "地緣政治事件", "1900-01-01"),
    ThemeTaxonomyRule("關稅", "EXCLUDE", "POLICY", "政策驅動因子，不是商品族群", "1900-01-01"),
)

_RULES_BY_NORMALIZED_LABEL = {
    normalized_text(rule.source_label): rule for rule in THEME_TAXONOMY_RULES
}


def taxonomy_rule(label: str) -> ThemeTaxonomyRule | None:
    """Return the exact normalized-label rule, if the label was manually reviewed."""
    return _RULES_BY_NORMALIZED_LABEL.get(normalized_text(label))


def is_excluded_theme_label(label: str) -> bool:
    """Whether a reviewed label is excluded from the formal theme universe."""
    rule = taxonomy_rule(label)
    return rule is not None and rule.action == "EXCLUDE"
