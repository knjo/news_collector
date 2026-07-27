# News Collector

用 Python 收集、正規化並分析新聞資料的專案。

## 開始使用

```bash
uv sync
uv run news-collector
```

如需自訂設定，先複製環境變數範例：

```bash
cp .env.example .env
```

## 開發指令

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## 預計模組

- `collectors`：RSS、新聞 API 與網頁來源
- `models`：新聞文章的共用資料格式
- `storage`：原始內容及結構化資料的保存
- `analysis`：分類、關鍵字、情緒與趨勢分析
