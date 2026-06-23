# リポジトリ構成（最新）

最終更新の主な変更点:
- 通常Botを **1日48回・30分間隔の固定スケジュール**（24時間、深夜含む）に変更
- 投稿前に **AI市場インパクト審査** を実施し、影響が小さい（low）ものは投稿せずスキップ
- **dry-run モードを廃止**（手動実行は image / diagram の2択）
- ランダムスケジュール再生成（reset.yml / generate_schedule.py）は**廃止**

## ディレクトリ構成

```
src/
  common/            # 共通処理
    x_client.py        ← X投稿（tweepy v1.1/v2: post_tweet, post_tweet_with_image）
    openai_client.py   ← OpenAI（get_openai_client, generate_by_openai, review_tweet_with_openai, モデル定数）
    safety.py          ← NGワード, 文字数/安全チェック, JST定義（is_night_time_jstは現在未使用）
  news_bot/          # 通常ニュースBot
    post.py            ← エントリ（python news_bot/post.py <image|diagram>）
    news.py            ← RSS取得（16フィード）
    diagram_post.py    ← 図解画像のJSON生成パイプライン
    diagram_image.py   ← 図解PNG描画（Pillow）
    posted_history.py  ← 投稿済み履歴（repo直下 data/ を参照）
  weekly_bot/        # 週次イベントBot
    weekly_post.py     ← エントリ（python weekly_bot/weekly_post.py [post]）
    weekly_events.py   ← Finnhub決算 + 公式マクロ日程テーブル
    weekly_normalizer.py
    weekly_selector.py
    weekly_renderer.py
  narrative_bot/     # 編集長レイヤー（市場ナラティブ）
    narrative_post.py  ← エントリ（python narrative_bot/narrative_post.py [post]）
    market_narrative.py ← 4ソース集約 + 編集長AI（①〜④生成、post_value<7はスキップ）
    narrative_renderer.py
    reddit_signals.py
data/
  posted_history.json  # 投稿済み履歴（リポジトリ直下のまま）
.github/workflows/
  post.yml      # 通常Bot：30分間隔・48cron固定（image/diagramランダム）+ 手動実行
  weekly.yml    # 週次Bot：日曜21:00 JST投稿 + 手動（image/post）
  narrative.yml # 編集長レイヤー：手動（image/post）
```

## 投稿フロー（通常Bot）

```
30分おき（24時間・48回）→ post.yml が python news_bot/post.py を起動
  → image / diagram をランダム選択（手動時は選択）
  → ニュース取得（posted_history で既出URLを除外）
  → AI市場インパクト審査（assess_market_impact）
      ├ low（市場影響が小さい/読者価値が低い）→ 投稿せずスキップ
      └ medium / high → 背景判定 → 生成 → コンプラレビュー → 投稿
```

48回は「投稿の機会」であり、審査を通った分だけ実際に投稿される。

## import が壊れない仕組み

- 各エントリ（post.py / weekly_post.py / narrative_post.py）の先頭に、
  src配下の全機能ディレクトリを sys.path に追加するブートストラップを置いている。
  → 移動後も `from posted_history import ...` `from news import ...` 等が無修正で動く。
- post.py は common/ の関数を再エクスポートするため、weekly/narrative の
  `from post import ...` も従来通り動作する。

## 主要パラメータ（調整可能）

- 投稿間隔・回数: post.yml の cron（現在30分×48本）
- スキップ強度: news_bot/post.py の assess_market_impact プロンプト基準
  （厳しくすると投稿数が減り質が上がる / 緩くすると投稿数が増える）
- AI審査でlow判定 → 投稿スキップ（IMPACT_SKIP_LEVEL = "low"）

## 削除済み / 非使用（旧構成からの変更）

- `.github/workflows/reset.yml` … 廃止（固定スケジュール化に伴い不要。残すと深夜にpost.ymlを上書きしてしまうため必ず削除）
- `src/scheduler/generate_schedule.py` … 非使用（reset.yml廃止により誰も呼ばない。削除可）
- `.github/workflows/narrative_post.yml` … 旧パス版。新 narrative.yml に統合済みのため削除
- `src/weekly_bot/weekly.yml` … 場所違いのコピー。正は .github/workflows/weekly.yml のみ
- dry-run モード … 廃止
- 深夜投稿ガード（is_night_time_jst）… 24時間投稿化に伴い未使用
