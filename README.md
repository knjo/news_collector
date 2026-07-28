# News Collector

收集鉅亨網台股新聞，整理成可按日期回溯的文章資料與每日題材資料。

## 執行

第一次使用先安裝相依套件：

```bash
uv sync
```

抓取包含今天在內的近 60 個台北日曆日：

```bash
./crawl_cnyes.sh --days 60
```

讓 2026 年 7 月每個輸出日都有完整 60 日暖機窗口：

```bash
./crawl_cnyes.sh --days 86 --end-date 2026-07-27 --output-start-date 2026-07-01
```

抓取截至指定日期的 30 個完整歷史日：

```bash
./crawl_cnyes.sh --days 30 --end-date 2026-07-26
```

可用參數：

```text
--days N                 抓取 N 個日曆日，預設 60
--end-date YYYY-MM-DD    指定最後一天；未指定時抓到現在
--output-start-date DATE  前面的資料只暖機，從這一天開始輸出每日族群表
--data-dir PATH          資料根目錄，預設 data
--reuse-existing         沿用既有 normalized 日期，只補抓缺少日期
--delay-seconds N        API 分頁間隔秒數，預設 0.3
```

## 正式資料

每一天都是獨立分區。重跑同一天會更新該日分區，不會建立重複的 run：

```text
data/cnyes/
├── raw/
│   └── published_date=YYYY-MM-DD/
│       └── page-0001.json.gz
├── normalized/
│   └── published_date=YYYY-MM-DD/
│       ├── articles.parquet
│       ├── article_keywords.parquet
│       ├── article_stocks.parquet
│       ├── article_themes.parquet
│       └── article_theme_stock_evidence.parquet
├── daily/
│   └── evidence_date=YYYY-MM-DD/
│       ├── daily_themes.parquet
│       ├── daily_theme_members.parquet
│       └── daily_theme_relations.parquet
├── reference/
│   ├── dim_themes.parquet
│   ├── dim_theme_aliases.parquet
│   ├── twse_holiday_schedule.json
│   └── data_leak_audit.json
├── manifest.json
├── report.json
├── period_report.json
└── report.md
```

三張每日分析表：

- `daily_themes`：最近 60 個日曆日曾有新聞的正式題材池。每列直接包含
  `theme_id`、`theme_label`、今日文章數、去重故事數、標題命中數、60 日有新聞
  天數、連續有新聞日數及距最近新聞日數；今天沒有新聞的計數明確為 0。
  `has_news_today` 與 `is_active_today` 分開，標題命中是特徵而非成立門檻。
- `daily_theme_members`：直接包含 `theme_id`、`theme_label`、對應台股與最近一次
  正式登記日。`member_snapshot_date` 及 `is_carried_forward` 可辨識是否沿用舊
  商品集合。
- `daily_theme_relations`：同日族群間的去重故事共現與合格商品重疊。

`ThemeId` 是無語意的連續 `UInt32`；seed 與自動題材共用流水號，來源另由
`origin_type` 區分。名稱及 aliases 由 `reference` 內兩張維度表對應。
正式族群判斷不使用語言模型；完整規則見
[`Theme_algorithm_zh.md`](Theme_algorithm_zh.md)。
明確非商品族群的人工排除項目集中在
`src/news_collector/analysis/theme_taxonomy.py`；規則不刪除原始新聞 keyword。

原始新聞保留在 `raw`；正規化文章與來源標籤保留在 `normalized`，因此分析結果
之後可以重算。歷史回填使用網站目前顯示的發布時間，可信度標記為
`archive_assumed`，不代表系統曾在當時即時看見該文章。

## 程式

- `crawl_cnyes.sh`：唯一需要執行的入口。
- `src/news_collector/collectors/cnyes.py`：鉅亨網抓取與文章正規化。
- `src/news_collector/analysis/topics.py`：去重、題材及熱度計算。
- `src/news_collector/cnyes_pipeline.py`：每日資料分區與三張表輸出。

程式碼檢查：

```bash
uv run ruff check .
```
