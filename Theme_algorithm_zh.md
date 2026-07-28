# 每日族群固定規則 v6

本文件描述 `theme-rule-v6`。正式三張表完全由程式規則產生，不使用語言模型。

## Point-in-time 契約

歷史資料依 `EvidenceDate` 由舊到新重播：

```text
Snapshot(D) 可使用：
  published_at <= AsOfAt(D) 的文章
  截至 D-1 保存的族群與商品狀態

Snapshot(D) 不可使用：
  D 之後的文章
  未來才出現的 alias
  D 之後計算出的成員或熱度
```

回填資料的發布時間可信度為 `archive_assumed`。每次建置另產生
`reference/data_leak_audit.json`，記錄各日的來源資料最大發布時間與狀態截止日。

## ThemeId 與 alias

`ThemeId` 使用無語意的連續 `UInt32`。seed 與自動候選共用同一個流水號，
來源差異記錄於 `dim_themes.OriginType`，不使用 ID 號段表達。

初次建立依日期順序配置 `1..N`。重建既有資料根目錄時，先按 `ThemeKey`
保留既有 `ThemeId`；只有新出現的 `ThemeKey` 才使用
`max(ThemeId) + 1`，避免規則調整使舊族群換號。

```text
dim_themes       ThemeId 對應固定 ThemeLabel
dim_theme_aliases 文字 alias 對應 ThemeId，並有 EffectiveFrom
```

固定 alias 只做 exact normalization。未知詞不使用未來資料做語意合併；
不同名稱如果沒有預先宣告 alias，可能暫時成為兩個 ThemeId，這是規則版刻意保留
的限制。

## 人工排除規則

`analysis/theme_taxonomy.py` 保存版本化的人工 taxonomy 表。第一版只處理語意上
明確不是商品族群的 Label，例如財務數字、公司行為、交易制度、人物、單一企業、
通用詞及外生事件；不依報酬、策略績效或未來商品重疊做決定。

排除只影響正式 `daily_themes`，來源的 `article_keywords.parquet` 仍完整保留。
每條規則記錄 `category`、`reason`、`applies_from` 與 `reviewed_on`，建置輸出另記錄
`taxonomy_version`。有語意邊界爭議的 Label 不在本版合併或排除。

## 候選族群

候選來源為鉅亨文章 keyword，以及當時已登記族群 alias 在文章標題中的直接命中。
標題命中可以補足來源漏標的已知族群，但不會用未來才建立的 alias 回填舊資料。
程式會排除：

- 當日以前已知的公司名稱及股票代碼；
- 通用市場、財務、盤勢詞；
- 固定地名、國家及機構詞；
- 投顧、證券來源詞；
- 符合固定上下文規則的人名。

候選必須同時符合：

```text
UniqueStoryCount >= 2
QualifiedTaiwanStockCount >= 3
```

標題命中不再是族群成立的必要門檻。`daily_themes` 另外保存
`ArticleCount`、`ThemeCount`（去重故事數）與 `TitleMentionCount`，讓下游模型
自行判斷「多篇文章提及」及「直接進入標題」的訊號強度。

## 60 日題材池

族群至少曾有一天通過上述最低品質門檻，才會進入正式題材池。進入後，只要最近
60 個日曆日仍有至少一篇候選新聞，每日 snapshot 都會保留該族群；今天沒有新聞
時，`ArticleCount`、`ThemeCount` 與 `TitleMentionCount` 明確寫入 `0`。

- `HasNewsToday`：今天是否有至少一篇候選新聞；
- `IsActiveToday`：今天是否重新通過 2 個故事、3 檔商品的品質門檻；
- `CountInSeason`：截至今天的 60 日窗口內，有新聞的日曆日數；
- `ConsecutiveNewsDays`：以今天結尾的連續有新聞日數；今天無新聞時為 0；
- `LastNewsDate`：最近一次有候選新聞的日期；
- `DaysSinceLastNews`：距最近新聞經過的日曆日數，今天有新聞時為 0。

因此「今天有新聞」與「今天重新通過族群成立門檻」是兩個不同 feature。

## 族群與商品證據

每個去重故事的 `(ThemeId, QuoteCode)` 只保留最強證據：

| EvidenceType | RawScore |
|---|---:|
| 題材與股票都在標題 | 4.0 |
| 題材與股票在同一句 | 3.0 |
| 題材與股票在同一段 | 2.0 |
| 來源題材標籤且股票在標題 | 2.0 |
| 題材在標題、股票在摘要或前三段 | 2.0 |
| 只有鉅亨結構化股票標籤 | 0.5 |

文章標記 6～10 檔台股時乘以 `0.7`，超過 10 檔時乘以 `0.3`。

```text
StoryScore = RawScore × Dilution
DailyLinkScore = 同日去重故事 StoryScore 總和
```

`DailyLinkScore >= 2.0` 才是當日合格商品。每一筆分數的文章與證據文字保存在
`article_theme_stock_evidence.parquet`。

## 生命週期

逐日使用昨日狀態更新：

- 從未通過門檻：`NEW`
- 距前次活躍不超過 2 個完整日曆日：`CONTINUING`
- 沉寂超過 2 個完整日曆日：`REVIVED`

`ActiveStreakDays` 只計真正逐日連續活躍；即使因週末容忍仍為
`CONTINUING`，中間沒有活躍時 streak 會重新從 1 開始。

商品的 `FirstMemberDate`、`MemberStatus` 與 `MemberStreakDays` 使用相同的
逐日狀態機。

題材池中沒有於今天重新通過門檻的族群，沿用最近一次正式成立日的商品集合。
`daily_theme_members.MemberSnapshotDate` 記錄商品集合的來源日期，
`IsCarriedForward=true` 表示並非今天重新確認，避免把沿用關係誤當成今日證據。
`daily_theme_members.ThemeLabel` 同時保存當下 catalog 顯示名稱，方便直接查看；
正式識別與 join 仍以 `ThemeId` 為準。

## 交易日

`TradeDate` 使用臺灣證券交易所開休市資料尋找 `EvidenceDate` 後的下一個交易日，
不使用日曆日期直接加一。原始官方資料保存在
`reference/twse_holiday_schedule.json`。
