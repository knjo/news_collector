# 每日新聞族群資料設計

> 狀態：設計草案，供確認  
> 目的：定義一套可長期保存、可追溯、可擴充，且未來能被 hedge fund 形式使用的每日新聞族群資料格式。  
> 使用時點：每天晚上固定時間封存，例如 Asia/Taipei 23:55，提供下一個台灣交易日使用。

---

## 1. 要解決的問題

目前市場關係資訊主要使用價量，當市場報酬越來越受到動態題材與族群共同因子影響時無法充分描述下列現象：

- 今天的主要新聞集中在哪些族群；
- 某個族群是第一次形成、持續發酵，還是沉寂後重新活化；
- 每個族群有哪些股票，以及股票與族群的關聯強弱；
- 同一檔股票同時屬於多個族群時，應如何分配其影響；
- 上游族群、下游族群或容易連動的族群之間是否正在傳導；
- 強勢族群是否由核心股擴散到外圍概念股；
- 市場活動是否從一個族群重新配置到另一個族群。

本設計不直接定義交易策略，而是建立一個穩定的每日資料底座，使後續可以
從不同角度研究族群熱度、價量強弱、輪動、外溢與風險中性化。

---

## 2. 核心工作項目

1. 從指定平排收集每日（歷史）新聞 / 文章，並且儲存供後續使用

2. 從上述資料製作出每日的族群表，由三張稀疏表構成：

```text
daily_themes
    每天有哪些族群，以及族群自己的狀態

daily_theme_members
    每個族群包含哪些股票，以及股票與族群的關聯強度

daily_theme_relations
    族群與族群之間有哪些結構或動態關係
```

它們形成兩個多對多網路：

```text
Theme <-> Stock
Theme <-> Theme
```
資料使用 Parquet long table，方便未來 Polars 查詢、日期切分。

---

## 3. 時間與可用性契約

### 3.1 每日封存

假設每天 23:00 建立 snapshot：

```text
EvidenceDate = 2026-07-23
AsOfAt       = 2026-07-23 23:00:00 Asia/Taipei
TradeDate    = 2026-07-24
```

此 snapshot 只能使用 `AsOfAt` 前已取得的新聞與資料，並提供
`TradeDate` 使用。

### 3.2 TradeDate 必須來自交易日曆

禁止直接使用日曆日期加一。必須使用台灣交易日曆尋找下一個交易日：

- 星期日 23:00 的 snapshot 可提供星期一使用；
- 連假最後一天 23:00 的 snapshot 提供下一個開市日使用；
- 假日前的新聞仍按其原始日曆日期保存；
- 交易用 snapshot 可以聚合前一交易日封存後至本次 `AsOfAt` 的新增證據。

### 3.3 data leaking 避免

 D 的族群身分、成員權重及族群關係，在 D+1 前固定。不可回頭使用 D+1 的為來新聞修改族群標籤 / 成員定義。


### 3.4 歷史新聞的時間可信度

每筆資料必須有 `AvailabilityGrade`：

| 值 | 定義 |
|---|---|
| `vendor` | 供應商（來源） |
| `live_observed` | 系統實際記錄明取得時間(哪天爬的) |
| `vendor_timestamped` | 使用供應商提供的首次發布時間 |
| `archive_assumed` | 歷史回填僅能依賴 archive 的 `published_at` |

歷史資料不得偽造 `fetched_at`。`archive_assumed` 可以研究，但回測必須將
它視為已知限制，並與未來 live overlap 做差異比較。

---

## 4. 三張表的共用欄位（key）

三張表均應包含以下欄位：

| 欄位 | 建議型別 | 說明 |
|---|---|---|
| `TradeDate` | Date | 此 snapshot 可使用的交易日 |
| `ThemeId` | UInt32 | 跨日穩定的族群 ID，對應 `dim_themes` |

共同規則：

- 所有時間均以 Asia/Taipei 做分隔；
- `TradeDate` 是交易所公告的日期 join key；
- 同一版本的 snapshot 不可被覆寫；
- 所有數值欄位必須區分 `0` 與 `null`；
- `0` 表示已觀察且數值為零，`null` 表示無法計算或來源缺失。

### 4.1 族群維度表

`ThemeId` 不使用 Label 字串，而使用不重複配置的 `UInt32`。名稱與 aliases
分別由下列維度表管理：

```text
dim_themes
    ThemeId, ThemeKey, ThemeLabel, Description, Status, CreatedDate

dim_theme_aliases
    ThemeId, Alias, NormalizedAlias, AliasType, EffectiveFrom
```

`ThemeLabel` 可以改名，但既有 `ThemeId` 不可改號或重複配給其他族群。
新增 alias 必須記錄生效日期，歷史 snapshot 不得使用當時尚未生效的 alias。

---

## 5. 表一：`daily_themes`

### 5.1 用途

描述某個交易日盤前已知的族群集合，以及每個族群自己的新聞狀態與生命週期。

它回答：

- 今天有哪些主要族群；
- 哪些是新族群；
- 哪些已經熱了一段時間；
- 哪些是沉寂後重新活化；
- 族群熱度正在升高還是衰退；
- 當天新聞是集中於少數題材，還是分散於許多題材。

### 5.2 主鍵

```text
TradeDate, ThemeId, SnapshotVersion
```

### 5.3 欄位

| 欄位 | 建議型別 | 必要性 | 說明 |
|---|---|---:|---|
| `ThemeId` | UInt32 | 必要 | 跨日穩定的族群 ID |
| `ThemeLabel` | String | 必要 | 顯示名稱，例如 CPO、AI 散熱 |
| `HasNewsToday` | Boolean | 必要 | 今天是否有至少一篇族群新聞 |
| `IsActiveToday` | Boolean | 必要 | 今天是否重新通過族群最低品質門檻 |
| `SourceCount` | UInt32 | 必要 | 當日新聞總數 |
| `ThemeCount` | UInt32 | 必要 | 來源中站多少 |
| `ArticleCount` | UInt32 | 必要 | 當日提及該族群的文章數，未做故事去重 |
| `TitleMentionCount` | UInt32 | 必要 | 當日標題提及該族群的去重故事數 |
| `CountInSeason` | UInt32 | 必要 | 最近 60 個日曆日中有族群新聞的天數 |
| `ConsecutiveNewsDays` | UInt32 | 必要 | 截至今天連續有新聞的日數；今天無新聞為 0 |
| `LastNewsDate` | Date | 必要 | 最近一次族群新聞日期 |
| `DaysSinceLastNews` | UInt32 | 必要 | 距最近新聞的日曆日數 |
| `ThemeStatus` | String | 必要 | `NEW`、`CONTINUING`、`REVIVED`、`IN_SEASON` |
| `FirstSeenDate` | Date | 必要 | 首次被辨識的日期 |
| `InactiveDaysBeforeToday` | UInt32 | 必要 | 本次活化前沉寂日數 |
| `ActiveStreakDays` | UInt32 | 必要 | 連續活躍日數 |
| `FirstMentionCount` | UInt32 | 必要 | 在內文中第一個提及的數量 |

可嘗試項目 
--- 
| 欄位 | 建議型別 | 必要性 | 說明 | 
|---|---|---:|---|
| `IsMainTheme` | Boolean | 衍生 | 是否為當日主要族群 |
| `RelevanceSum` | Float64 | 衍生 | 主題相關度加權總和 |
| `HeatRaw` | Float64 | 衍生 | 熱度變化 |
| `NoveltyScore` | Float64 | 衍生 | 與既有族群的差異程度 | 

### 5.4 範例

```text
TradeDate  ThemeId     ThemeLabel  ThemeStatus  SourceCount  ThemeCount  ThemeStatus
20260724   CPO         CPO         CONTINUING   28           20          CONTINUING
20260724   AI_COOLING  AI散熱       NEW          28           9           NEW
```  

### 5.5 注意事項

1. 不要只保存 Top-N 族群。底層應保存所有通過最低品質門檻的族群。
2. 可能會有 從母族群拆出子族群的狀況，若一族群商品數量 <3，擇不拆/視為沒有族群。
3. `ThemeLabel` 可以改名，`ThemeId` 不應因顯示名稱改變。
4. `ThemeStatus` 只能使用封存前的歷史判斷。
5. 同一篇通稿被多家媒體轉載，不應被視為多個獨立故事。


---

## 6. 表二：`daily_theme_members`

### 6.1 用途

描述每個族群有哪些股票，以及股票與該族群的關聯強度。

它回答：

- 哪些股票是族群核心；
- 哪些是外圍或新加入的概念股；
- 同一股票同時屬於哪些族群；
- 新聞熱度是否從核心股擴散到更多成員；
- 族群報酬、成交量與 breadth 應如何加權；
- 跨族群計算時如何避免重複計入同一檔股票。

### 6.2 主鍵

```text
TradeDate, ThemeId, QuoteCode, SnapshotVersion
```

### 6.3 欄位

| 欄位 | 建議型別 | 必要性 | 說明 |
|---|---|---:|---|
| `ThemeId` | UInt32 | 必要 | 跨日穩定的族群 ID |
| `ThemeLabel` | String | 必要 | 顯示名稱，例如 CPO、AI 散熱 |
| `QuoteCode` | String | 必要 | 股票代碼 |
| `MentionCount` | UInt32 | 必要 | 原始提及次數(同一篇文章可算多次) |
| `UniqueStoryCount` | UInt32 | 必要 | 出現於多少篇去重故事 |
| `TitleMentionCount` | UInt32 | 必要 | 標題提及故事數 |
| `FirstMemberDate` | Date | 必要 | 首次被歸入該族群 |
| `MemberStreakDays` | UInt32 | 必要 | 連續屬於該族群的日數 |
| `MemberSnapshotDate` | Date | 必要 | 本列商品集合最近一次正式登記日期 |
| `IsCarriedForward` | Boolean | 必要 | 是否從最近登記日沿用，而非今天重新確認 |

### 6.4 範例

```text
TradeDate  ThemeId     QuoteCode  MentionCount  UniqueStoryCount  TitleMentionCount
20260724   AI_SERVER   2382       102            7                 3         
20260724   AI_SERVER   3017       8              3                 0         
20260724   AI_COOLING  3017       18             3                 0         
``` 

### 6.5 注意事項

1. `MentionCount` 不等於真正關聯強度。還需考慮標題、文章位置、來源數、
   文章包含的實體數量與主題相關度。
2. 同一篇通稿被多家媒體轉載，不應被視為多個獨立故事。
3. 一檔股票可以有多個 `ThemeId`，不強迫單一分類。


---

## 7. 表三：`daily_theme_relations`

### 7.1 用途

描述族群與族群之間的結構關係與每日活化程度。

它回答：

- 哪些族群是上下游；
- 哪些族群常在同一批新聞中出現；
- 哪些族群成員高度重疊；
- 哪些族群容易價格連動；
- 哪個族群通常領先另一個族群；
- 某條上下游或連動關係今天是否正在活化；
- 強勢族群是否可能向關聯族群外溢。

### 7.2 主鍵

同一對族群可以存在多種關係，因此主鍵為：

```text
TradeDate
SourceThemeId
TargetThemeId
RelationType
Window
SnapshotVersion
```

若 `RelationType` 需要多個 lag，主鍵另加 `LagMinutes`。

### 7.3 欄位

| 欄位 | 建議型別 | 必要性 | 說明 |
|---|---|---:|---|
| `ThemeId` | UInt32 | 必要 | 關係來源族群(標題帶有族群) |
| `ThemeLabel` | String | 必要 | 族群名稱 |
| `TargetThemeId` | UInt32 | 其他當日族群 |
| `TargetConnection` | int | 從標題可連到其他族群 |


### 7.4 範例

```text
TradeDate  ThemeLabel     CPO      AI_SERVER   PCB        
20260724   CPO            4        1            2          
20260724   AI_SERVER      2        1            2          
20260724   PCB            0        3            3          
```

- 自己對自己 ＝ 該商品為標題的文章總數
- 若標題太少，則嘗試改成使用文章中出現為第一提及族群當追蹤來源
