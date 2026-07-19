# 投稿量・コスト制御

目標は1日最大30投稿、X APIとOpenAI APIの合計を月額5,000円以内に保つことです。

## 投稿制御

- 全bot合計: 30件/日
- 1時間: 2件まで
- 同一銘柄: 180分空ける
- 同一テーマ: 90分空ける
- URL付き投稿: 禁止
- スレッド返信: 既定で無効
- X書き込み予算: 月15米ドルまで
- OpenAI API予算: 月5米ドルまで

返信を有効にすると、返信1件ごとに投稿数とX API費用が増えます。

## 実績取得

- 全投稿を投稿24時間後に1回取得
- 反応上位20%だけ投稿7日後に再取得
- それ以外は `data/metrics_history.json` のキャッシュを使用

## 主な環境変数

```text
DAILY_POST_LIMIT=30
HOURLY_POST_LIMIT=2
TICKER_COOLDOWN_MINUTES=180
THEME_COOLDOWN_MINUTES=90
THREADS_ENABLED=false
X_CONTENT_CREATE_USD=0.015
X_WRITE_MONTHLY_BUDGET_USD=15.0
OPENAI_MONTHLY_BUDGET_USD=5.0
```

`python local_finance_bot.py status` で当日投稿数とX書き込み推定額を確認できます。
