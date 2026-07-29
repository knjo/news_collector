# 廣義題材每日寬表規則 v2

輸出位置：

```text
data/cnyes/broad_daily/evidence_date=YYYY-MM-DD/
└── daily_broad_theme_members.parquet
```

主鍵為：

```text
evidence_date, theme_id, quote_code
```

每列表示「截至 `evidence_date`，某商品已通過某個廣義題材的新聞證據門檻」。
正式流程不讀取 `theme_universe_2026_hindsight.csv`；該檔案只可用於離線評估。
另將所有完整日期合併輸出為 Zstandard 壓縮的 Parquet：

```text
data/cnyes/theme_universe.parquet
```

## 廣義題材

廣義題材只保存文字到母題材的語意關係，不保存股票清單。例如：

```text
記憶體 <- 記憶體、DRAM、DDR4、DDR5、NAND、SSD
PCB    <- PCB、CCL、玻纖布、軟板、載板、BT載板、IC載板
軍工   <- 軍工、無人機、國防、航太
```

股票不能因為目前已知的產業拓樸被回填到歷史。它只能在當日及過去新聞證據
累積通過門檻後加入。

## 父子題材

父族群沒有數量上限。固定、版本化的文字規則先合併已知同義或上下位文字；
新聞演算法辨識出的其他正式題材若未被合併，直接以來源名稱成為獨立父族群。
因此 18 個既有廣義題材只是合併規則，不是白名單。
財報指標、配息、交易制度、公司行動、人物與一次性活動等非產業標籤會先依
固定語意規則排除；這些排除不使用股價、股票名單或後驗績效。

子題材使用固定、版本化的文字語意關係，例如：

```text
PCB   -> CCL、玻纖布、軟板、HDI、載板
記憶體 -> DRAM、DDR4、DDR5、HBM、NAND、SSD
軍工   -> 無人機、國防、航太
```

固定規則只定義文字關係，不含任何股票名單。某個子題材在資料日當下至少
5 檔商品通過下列滾動商品門檻時，才會以獨立 `theme_id` 輸出；不足 5 檔時
只保留父族群。相同商品可以同時出現在父族群與子題材。

## 滾動商品門檻

對每個去重故事的 `(廣義題材, 股票)` 只保留最強的既有 StoryScore，並在每個
資料日使用過去 365 個日曆日計算：

```text
至少 2 個不同證據日
累積 StoryScore >= 4
至少 1 個 StoryScore >= 2 的強證據故事
```

排名分數為：

```text
RankScore
= 累積 StoryScore
× (1 + log(1 + 證據日數))
× sqrt(本股票在此題材的分數 / 本股票在所有廣義題材的分數)
```

最後將同日、同題材的最高 RankScore 標準化為 100，寫入
`membership_score`。此值適合在同日同題材內比較，不適合跨題材直接比較。

## 欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `evidence_date` | Date | 新聞資料日期 |
| `trade_date` | Date | 此資料可供使用的下一個臺股交易日 |
| `theme_id` | UInt32 | 固定廣義題材 ID |
| `theme_name` | String | 廣義題材名稱 |
| `news_days_60d` | UInt32 | 最近 60 日中有題材新聞的日數 |
| `is_new_theme` | Boolean | 是否為此題材第一次正式輸出 |
| `days_since_last_news` | UInt32 | 距最近題材新聞的日曆日數 |
| `member_count_change` | Int32 | 商品數相較前一日的淨變化；第一次輸出為 0 |
| `quote_code` | String | 臺股代碼 |
| `stock_name` | String | 股票名稱 |
| `membership_score` | Float64 | 同日同題材內標準化為 0～100 的滾動相關度 |
| `is_new_member` | Boolean | 是否第一次通過此題材的商品門檻 |

## Point-in-time 契約

計算資料日 D 時只讀取：

```text
新聞日期 <= D
商品證據日期 <= D
截至 D 的臺股交易日曆
```

商品不再設固定檔數上限；只要通過證據門檻就會保留。股票的 `is_new_member`
只會在第一次通過門檻的日期為 true，不會因為未來後驗
名單而提前。廣義題材超過 60 日沒有任何新聞時暫停輸出；重新有新聞後，仍只使用
當時 365 日窗口內的證據重建商品集合。
