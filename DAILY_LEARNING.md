# 日次Top3学習機能

毎日22:00 JSTの `report` 実行時に、直近24時間の投稿をX APIで再取得し、
インプレッション上位3件をOpenAIでレビューします。

## 保存先

- `knowledge/viral_patterns/daily_top3.jsonl`
  - 日ごとの上位投稿とレビュー結果
- `knowledge/viral_patterns/reviews/YYYY-MM-DD.json`
  - 当日の詳細
- `knowledge/viral_patterns/latest_patterns.md`
  - 次回以降の生成プロンプトへ差し込む最新ルール
- `data/metrics_history.json`
  - X APIから取得した実績キャッシュ

## 対象

共通投稿履歴を導入し、次の親投稿を記録します。

- news
- narrative
- weekly
- market-map

## 学習の意味

モデルをファインチューニングするのではなく、
上位投稿から得た再利用可能な表現・構成ルールをプロンプトへ読み込ませます。

安全審査、投稿価値ゲート、事実確認、重複回避は学習メモより常に優先されます。

## 手動テスト

```powershell
$work = "C:\Projects\finance-narrative"
& "$work\.venv\Scripts\python.exe" "$work\local_finance_bot.py" report --days 1
```

初回は、直近24時間に `tweet_id` 付き投稿があり、
X APIからインプレッションを取得できる場合に学習ファイルが作成されます。
