# finance-narrative（ローカル運用版）

米国株向けのX自動投稿Bot 4系統。**GitHub Actions からローカル運用に移行しました。**
運用の中心は `local_finance_bot.py` です（**Windows / macOS / Linux 対応**）。GitHub Actions のスケジュールは無効化済みです

## Bot構成

| Bot | 内容 | 既定スケジュール |
|---|---|---|
| news | ニュース図解/要約投稿 | 30分間隔（`config/schedule.json`） |
| narrative | 米国株の注目材料 | 米国営業日 08:30 / 09:35 / 16:05 ET |
| market-map | 大幅変動時だけヒートマップ（判定） | 米国営業日 09:35 / 15:50 ET |
| weekly | 週間注目イベント | 毎週日曜 21:00 JST |

market-mapは定時刻に必ず投稿するのではなく、時価総額・指数・セクター集中・市場内部の大きな変化が基準を超えた場合だけ投稿します。引け前は、S&P500が前日終値をまたいで0.75ポイント以上反転した場合も大幅変動として扱います。

Newsは19本のRSSを使用します。市場ニュース12本、公式マクロ・規制・政策7本で構成し、
同一媒体系列は既定で候補上位3件までに制限します。TechCrunch AIとCoinDeskは
米国株・市場に関係する見出しだけを候補化し、有料媒体を含めRSSから保存するのは
見出し、URL、公開日時、情報源だけです。記事本文は転載・推測しません。各RSS取得は
既定12秒・最大2MBに制限し、1媒体の遅延や異常応答でBot全体を停止させません。

## Windows クイックスタート（ダブルクリックだけで使う）

エクスプローラーでこのフォルダを開き、順に**ダブルクリック**：

1. `setup_windows.bat` … 仮想環境の作成と依存インストール（初回のみ・数分）
2. `.env` にAPIキーを記入（`notepad .env`）。**`POST_ENABLED=false` のままなら実投稿されません**
3. `run_status.bat` … 状態確認（POST_ENABLED / 次回予定）
4. `run_test_post.bat` … Xへ1件だけテスト投稿（`y`で実行。本番前の疎通確認用）
5. `run_report.bat` … 投稿実績レポート（インプレ/いいね/RT・テーマ別分析）
6. 本番投稿するときは `.env` を `POST_ENABLED=true` にして `run_daemon.bat` で常駐

> 「WindowsによってPCが保護されました」と出たら「詳細情報」→「実行」で許可してください。
> `run_daemon.bat` のウィンドウを閉じると daemon は停止します。

### Windows で自動起動（タスクスケジューラ）

1. スタートメニューで「タスクスケジューラ」を検索して開く
2. 「基本タスクの作成」→ 名前: finance-bot
3. トリガー: 「ログオン時」
4. 操作: 「プログラムの開始」
   - プログラム: `C:\Projects\finance-narrative\run_daemon.bat`（実際のパスに合わせる）
   - 開始（オプション）: `C:\Projects\finance-narrative`
5. 完了。次回ログオンから自動起動します
6. 電源設定でスリープを無効に（設定 → システム → 電源 → スリープしない）

**注意: MacとWindowsの両方で daemon を動かすと同じ内容が2回投稿されます。どちらか一方だけで運用してください。**

---

## macOS クイックスタート（ダブルクリックだけで使う）

Finder でこのフォルダを開き、順に**ダブルクリック**するだけで動きます。

1. `setup_mac.command` … 仮想環境の作成と依存インストール（初回のみ・数分）
2. `.env` にAPIキーを記入（`open -e .env`）。**`POST_ENABLED=false` のままなら実投稿されません**
3. `run_status.command` … 状態確認（POST_ENABLED / 次回予定）
4. `run_test_post.command` … Xへ1件だけテスト投稿（`y`で実行。本番前の疎通確認用）
5. 本番投稿するときは `.env` を `POST_ENABLED=true` にして `run_daemon.command` で常駐

> **初回だけ Gatekeeper の警告**が出ることがあります。その場合は `.command` ファイルを
> **右クリック →「開く」→「開く」** で許可してください（2回目以降はダブルクリックでOK）。
> それでも「実行できません」の場合はターミナルで一度だけ:
> `chmod +x "/フルパス/finance-narrative/"*.command`

ターミナル派の場合:

```bash
cd "/Users/あなた/finance-narrative"     # 実際のパスに
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # キー記入。POST_ENABLED=false のまま確認
python local_finance_bot.py init-state
python local_finance_bot.py status
python local_finance_bot.py once news --mode image     # 投稿なしで動作確認
```

### macOS で自動起動（ログイン時に常駐・落ちても再起動）

`com.example.financebot.plist.example` を使います。

```bash
# 1) テンプレの __REPO_PATH__ を実パスに置換して LaunchAgents へ
sed "s|__REPO_PATH__|$PWD|g" com.example.financebot.plist.example \
  > ~/Library/LaunchAgents/com.example.financebot.plist
# 2) 読み込み（起動）
launchctl load ~/Library/LaunchAgents/com.example.financebot.plist
# 停止/解除
launchctl unload ~/Library/LaunchAgents/com.example.financebot.plist
```

投稿の可否は `.env` の `POST_ENABLED` に従います（本番は `true`）。
出力は `logs/launchd.out.log` / `logs/launchd.err.log` にも残ります。

> **Apple Silicon (M1〜) で `kaleido`/`plotly` の導入に失敗する場合**は、先に
> `pip install --upgrade pip` を実行してから `pip install -r requirements.txt` を再試行してください。
> market-map のヒートマップ画像のみに影響し、他Botは動きます。

---

## セットアップ

```bash
cp .env.example .env      # APIキーを記入（コミット禁止・.gitignore済み）
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python local_finance_bot.py init-state   # 初回だけ（過去スロット暴発防止）
python local_finance_bot.py status       # 状態確認
```

### `.env` の重要ポイント
- **`POST_ENABLED=false`（既定）の間は、絶対にXへ実投稿されません。**
  ニュース取得・AI判定・投稿文/画像生成までは動くので、安全に動作確認できます。
  投稿直前で `[INFO] POST_ENABLED=false -> X posting skipped` と出て止まり、
  **投稿履歴（data/posted_history.json）にも保存されません。**
- 本番投稿を始めるときだけ `POST_ENABLED=true` にしてください。
- X APIキーは `API_KEY / API_KEY_SECRET / ACCESS_TOKEN / ACCESS_TOKEN_SECRET`（全Bot共通・market-mapも同じ。旧 `X_API_KEY` 系もフォールバックで読めます）。
- キーが不足している場合、どの環境変数が無いかエラーに表示されます。POST_ENABLED=false ならXキーが無くても投稿直前まで動きます。
- `FINNHUB_API_KEY` 未設定時は、weekly/narrative の決算データ取得がスキップまたは縮退します（クラッシュはしません）。

### `init-state` とは
GitHub Actions からの移行初回に、**過去の未実行スケジュールをまとめて追いかけない**ための初期化です。
各Botの `last_run_at` を現在時刻にし、`daemon` は未来のスケジュールからだけ動きます。実投稿はしません。

## 使い方

```bash
# 個別に1回だけ実行
python local_finance_bot.py once news --mode image
python local_finance_bot.py once news --mode diagram
python local_finance_bot.py once narrative
python local_finance_bot.py once market-map
python local_finance_bot.py once weekly

# 強制実行（スケジュール条件のみ無視。安全審査・投稿価値ゲート・AIレビューは維持）
python local_finance_bot.py force narrative   # 休場日ゲートも無視して検証できる

# 常駐（次の予定までsleep。Ctrl+Cで安全終了。lockで二重起動防止）
python local_finance_bot.py daemon
```

スケジュールは `config/schedule.json` で変更できます。
`RUN_WINDOW_MINUTES`（既定10分）を超えて遅延したスロットは、`CATCH_UP_ENABLED=false` の場合スキップされます。

### 生成ファイルの自動整理

daemon は24時間に1回、再生成できる古いファイルだけを自動削除します。既定では
`outputs/` とバックアップを30日、マーケットデータのキャッシュを7日、
レポート類を90日保持します。投稿履歴・API利用履歴・`.venv` は削除しません。

```bash
python local_finance_bot.py cleanup --dry-run  # 削除対象だけ確認
python local_finance_bot.py cleanup            # 今すぐ実行
```

`.env` で `HOUSEKEEPING_ENABLED=false` にすると停止できます。保持期間は
`OUTPUT_RETENTION_DAYS`、`BACKUP_RETENTION_DAYS`、`CACHE_RETENTION_DAYS`、
`REPORT_RETENTION_DAYS` で変更できます。

## OSごとの常駐方法

- **Windows**: タスクスケジューラで「ログオン時に `python local_finance_bot.py daemon`」を登録、またはターミナル常駐
- **macOS**: launchd（`~/Library/LaunchAgents` に plist）またはターミナル常駐
- **Linux**: systemd ユニット例:

```ini
[Unit]
Description=finance bot daemon
[Service]
WorkingDirectory=/path/to/finance-narrative
ExecStart=/path/to/.venv/bin/python local_finance_bot.py daemon
Restart=on-failure
[Install]
WantedBy=default.target
```

## ログ

| ファイル | 内容 |
|---|---|
| `logs/bot.log` | 全Botの実行ログ |
| `logs/decisions.jsonl` | 投稿判断（post_value / relevance / buzz / skip_reason / tweet_id 等） |
| `logs/errors.jsonl` | エラー |
| `logs/run_history.jsonl` | run単位の結果（開始/終了/returncode/POST_ENABLED） |

画像は `outputs/news/` `outputs/narrative/` `outputs/weekly/` `outputs/market_map/` に出ます（`OUTPUT_DIR`で変更可）。
状態は `data/`（`STATE_DIR`で変更可）。**旧 `src/data/posted_history.json` が残っている場合は初回に自動移行されます。**

## 投稿されないときの確認

1. `python local_finance_bot.py status` — POST_ENABLED / 次回予定 / lock
2. `POST_ENABLED=true` になっているか（falseなら仕様どおり投稿されません）
3. `logs/decisions.jsonl` の `skip_reason`（post_value不足 / relevance不足 / レビューNG / NGワード / 休場日）
4. `logs/errors.jsonl`（APIキー不足・ネットワーク・X APIエラー）
5. narrative / market-map は米国休場日は動きません（`force`で検証可）

## フォント

日本語フォントは自動検出します（Linux: Noto CJK / macOS: ヒラギノ / Windows: Yu Gothic・Meiryo）。
明示指定する場合は `.env` に:

```env
FONT_PATH=C:/Windows/Fonts/YuGothM.ttc
```

見つからない場合も落ちずに警告を出します（画像の日本語が崩れる可能性のみ）。

## 安全設計（変更禁止の前提）

- 投資助言・売買推奨は禁止（NGワード + OpenAIレビューの二重ゲート、fail closed）
- 未確認の数字・事実の捏造は禁止。市場データ取得失敗時は推測で埋めずスキップ
- 通常投稿は簡潔さを維持し、詳しい解説が必要な複雑事象に限って130文字超を許可。背景・因果・市場の注目点を省略せず、文章の完結を優先
- 投稿価値ゲート: news は post_value>=7 かつ 関連度/話題性ゲート、narrative は post_value>=8
- 履歴は**投稿成功後だけ**保存。失敗・POST_ENABLED=false では保存しない

## 今後の改善用（Botに読ませて改善する想定のファイル）

`config/bot_persona.md` / `config/finance_tone.md` /
`knowledge/viral_patterns/` / `knowledge/failed_patterns/` /
`knowledge/source_notes/` / `knowledge/ticker_notes/`
# Growth operations additions

The bot keeps its existing safety and posting pipeline while adding controlled growth experiments.

- xAI Radar runs at six priority windows in JST: `00:00`, `06:00`, `08:00`, `17:00`, `21:00`, and `22:30`.
- Radar is enabled only when the feature flags, schedule, API key, budget, and daily limit all permit it.
- xAI counts are observed samples returned by the search, not complete totals for all of X. New fields use the `observed_` prefix; legacy aliases remain readable.
- Post styles are tracked as `breaking_news`, `misconception`, `second_order_effect`, `comparison`, and `scheduled_summary`.
- No more than three experiments are active. Results prioritize available growth KPIs and never convert unavailable metrics to zero.
- Metrics collection windows are 45–90 minutes, 330–420 minutes, and 1380–1620 minutes. Expired stages are recorded as `missed`; later values are never backfilled.
- OpenAI Batch is limited to delayed historical analysis and is never used for realtime generation, duplicate checks, or safety review.
- Quote-post candidate generation is disabled. The bot never automatically
  quotes, replies, likes, follows, or sends DMs, and no human review queue is
  created.
- Operational alerts are written locally under `outputs/alerts`. Set
  `DISCORD_ALERTS_ENABLED=true` and `DISCORD_WEBHOOK_URL` in `.env` to deliver
  only newly detected and resolved alert state changes to Discord.
- Set `DISCORD_POST_NOTIFICATIONS_ENABLED=true` to mirror successful X posts
  with their full text and X URL. Production uses result-only Discord
  notifications: keep `DISCORD_LOGS_ENABLED=false` so detailed stdout, stderr,
  decision, and runtime logs stay local. Enable it only temporarily when
  detailed remote diagnostics are explicitly required.

Inspection commands:

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py config-status
.\.venv\Scripts\python.exe local_finance_bot.py radar-plan
.\.venv\Scripts\python.exe local_finance_bot.py experiments --weekly
.\.venv\Scripts\python.exe local_finance_bot.py metrics-status
.\.venv\Scripts\python.exe local_finance_bot.py rss-status
.\.venv\Scripts\python.exe local_finance_bot.py alerts
.\.venv\Scripts\python.exe local_finance_bot.py xai-cost-report --days 30
.\.venv\Scripts\python.exe local_finance_bot.py xai-roi-report --days 30
.\.venv\Scripts\python.exe local_finance_bot.py alerts-self-test
.\.venv\Scripts\python.exe local_finance_bot.py health-check
```

Current cost and quality controls:

- Normal xAI radar windows are `21:00` and `22:30` JST, capped at 2 searches/day.
  Event mode is capped at 4 searches/day. Each search is limited to 5 topics,
  2 representative posts and 2 accounts per topic.
- Cache keys include the effective query configuration. Cache hits/misses and
  reported-versus-estimated costs are observable; reported cost and estimates
  are never added together.
- Metrics runs every 30 minutes and prioritizes the closest 1h/6h/24h deadline.
  Misses and unavailable/deleted posts retain explicit reason codes.
- Daily report subtasks are isolated. `partial_success` uses exit code 2 and
  saves per-task status under `outputs/reports`.
- Follow conversion remains `unavailable` unless X actually supplies follow and
  profile-click metrics. The bot does not fabricate this KPI.

## FX Alert Bot

USD/JPYの大幅変動を5分ごとに監視し、データ品質、固定閾値とボラティリティ、
再通知クールダウンを通過した場合に1600×900のチャートと投稿候補を生成します。
初期値は `FX_POST_ENABLED=false` であり、`POST_ENABLED` と両方がtrueになるまで
Xへは投稿しません。実投稿時は既存Moderation、金融安全レビュー、全Bot共通の
投稿上限を通過する必要があります。

初期プロバイダーはTwelve DataのRESTです。WebSocket契約能力は自動で仮定せず、
利用不可または未実装の場合は安全な5分REST pollingとして稼働します。
`TWELVE_DATA_API_KEY` が未設定ならFX Alertだけが安全停止し、他Botは継続します。
Polygonは交換用interfaceのみで、本番アクセスは無効です。価格・bar・変動・
利用記録は `data/fx/`、チャートは `outputs/fx_charts/YYYY-MM-DD/` に保存します。

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py fx-status
.\.venv\Scripts\python.exe local_finance_bot.py fx-provider-status
.\.venv\Scripts\python.exe local_finance_bot.py fx-monitor --dry-run
.\.venv\Scripts\python.exe local_finance_bot.py fx-check USDJPY
.\.venv\Scripts\python.exe local_finance_bot.py fx-chart USDJPY --period 24h
.\.venv\Scripts\python.exe local_finance_bot.py fx-alert-test --fixture
.\.venv\Scripts\python.exe local_finance_bot.py fx-history
.\.venv\Scripts\python.exe local_finance_bot.py fx-enable-status
```

閾値、品質上限、API呼出上限、保持期間は `.env.example` の `FX_*` で変更できます。
料金単価が不明な契約ではコストを推測せず、呼出回数と「推定コスト利用不可」を
分けて記録します。APIキー、Webhook URL、`.env` 全体をログへ出してはいけません。

## Twelve Data マルチアセット市場監視

米国メガキャップ、主要ETF、債券・金ETF、BTC/USDをTwelve Data RESTで
15分ごとにローテーション監視します。固定変動率に加え、z-score、ATR倍率、
ブレイクアウト、相対出来高を二次条件として使い、通常の値動きでは通知しません。
米国株とETFは通常取引時間だけ、暗号資産は1時間に1回の補助監視です。
USD/JPYは既存FX Alertが担当するため二重取得しません。

個人向け契約の外部表示権を自動では仮定しません。現在の安全な初期値は次の通りです。

```env
MARKET_DATA_ENABLED=true
MARKET_DATA_POST_ENABLED=false
TWELVEDATA_EXTERNAL_DISPLAY_APPROVED=false
```

外部表示が未承認の間、取得値・チャート・投稿本文はローカルだけに保存され、
DiscordとXには出ません。fixture通知だけは
`[TEST/FIXTURE・架空データ]` と明示して送信できます。市場データの投稿には
上記2フラグ、全体の`POST_ENABLED`、OpenAIレビュー、既存の投稿上限をすべて
通過する必要があります。API creditは分・日単位で記録し、80%で低優先取得を
止め、95%で全取得を安全停止します。

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py td-capabilities
.\.venv\Scripts\python.exe local_finance_bot.py td-provider-status
.\.venv\Scripts\python.exe local_finance_bot.py market-data-status
.\.venv\Scripts\python.exe local_finance_bot.py market-watchlist
.\.venv\Scripts\python.exe local_finance_bot.py market-check NVDA
.\.venv\Scripts\python.exe local_finance_bot.py market-chart NVDA --period 24h
.\.venv\Scripts\python.exe local_finance_bot.py mega-alert-test --fixture
.\.venv\Scripts\python.exe local_finance_bot.py etf-alert-test --fixture
.\.venv\Scripts\python.exe local_finance_bot.py cross-asset-test --fixture
.\.venv\Scripts\python.exe local_finance_bot.py earnings-reaction-test --fixture
.\.venv\Scripts\python.exe local_finance_bot.py market-usage
.\.venv\Scripts\python.exe local_finance_bot.py market-data-enable-status
```

## Safety and observability controls

Twelve Data publication rights default to `unknown`. Data collection, local
analysis, metrics, reports, and explicitly internal Discord previews continue,
but public X text and charts are automatically blocked until explicit rights
configuration is present. No per-candidate human review queue is used.

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py td-license-status
.\.venv\Scripts\python.exe local_finance_bot.py td-license-checklist
.\.venv\Scripts\python.exe local_finance_bot.py market-publication-status
.\.venv\Scripts\python.exe local_finance_bot.py metrics-stage-status
.\.venv\Scripts\python.exe local_finance_bot.py metrics-rolling --days 7
.\.venv\Scripts\python.exe local_finance_bot.py xai-cost-breakdown
.\.venv\Scripts\python.exe local_finance_bot.py shadow-report --days 7
.\.venv\Scripts\python.exe local_finance_bot.py heartbeat-status
.\.venv\Scripts\python.exe local_finance_bot.py runtime-manifest
```

External heartbeat integration is provider-neutral and disabled by default.
If an operator supplies an HTTPS ping URL, set `EXTERNAL_HEARTBEAT_URL` and
then set `EXTERNAL_HEARTBEAT_ENABLED=true`. The URL is treated as a secret;
status output only shows a masked host. Heartbeat failures never stop the
daemon.

Twelve Data failures are isolated to market-data features:

- `healthy`: normal monitoring
- `degraded`: cache use, reduced polling, and an internal warning
- `stale`: public posting is blocked; internal analysis only
- `unavailable`: market-data features stop while News/Narrative continue
- `auth_failed`: retries are limited and credentials are never logged
- `budget_limited`: low-priority symbols and public posting stop
- `license_blocked`: internal analysis continues; external display stops

No unconfigured fallback provider is invented, and stale cache values are
never published as current prices.

監視対象は`config/market_watchlist.json`、検知・使用量・キャッシュは
`data/market_data/`、チャートとメタデータは`outputs/market_charts/`に保存します。

Twelve Dataの外部表示権が未承認の場合、数値を丸める、方向だけにする、
チャートを描き直す等の加工でもFX・市場データ投稿は許可しません。代わりに
`OFFICIAL_EDITORIAL_POST_ENABLED=true`では、Fed、BEA、BLS、EIA、SEC等の
公式RSSを独立した根拠とする市場解説をNews経路から投稿できます。この経路は
Twelve Dataの検知・価格・騰落率・時間足・チャートを一切引き継がず、公式見出しに
ない数字やリアルタイム相場表現を投稿前に遮断します。これは法的判断ではなく、
データ出所を分離する技術的安全策です。

## Evidence-first market-trigger workflow

Twelve Data is used only as an internal movement sensor. Its prices, returns,
high/low, volume, charts, and provider-derived figures are excluded from the
public evidence bundle and from the OpenAI publication prompt. A movement
creates `TriggerEvidence`, followed by source-specific `EventEvidence`,
timing-aware `CausalEvidence`, and claim-level `PublicationEvidence`.

Only a validated `PublicEvidenceBundle` reaches structured OpenAI generation.
Every factual or causal claim must map to an evidence ID. Missing sources,
stale timing, duplicate wire republication, reference-rate-only sources,
unsupported market figures, and unconfirmed intervention claims stop before
the X API call. Pending cases are rechecked after 15/30/60 minutes and expire
after 120 minutes by default. News, Narrative, and Market Map continue if
confirmation sources fail.

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py trigger-status
.\.venv\Scripts\python.exe local_finance_bot.py trigger-pending
.\.venv\Scripts\python.exe local_finance_bot.py trigger-show <movement_id>
.\.venv\Scripts\python.exe local_finance_bot.py trigger-evidence <movement_id>
.\.venv\Scripts\python.exe local_finance_bot.py trigger-recheck <movement_id> --dry-run
.\.venv\Scripts\python.exe local_finance_bot.py trigger-suppressed --hours 24
.\.venv\Scripts\python.exe local_finance_bot.py trigger-confirmation-report --days 7
.\.venv\Scripts\python.exe local_finance_bot.py trigger-source-report --days 7
.\.venv\Scripts\python.exe local_finance_bot.py trigger-later-review --days 7
.\.venv\Scripts\python.exe local_finance_bot.py publication-evidence-check <candidate_id>
.\.venv\Scripts\python.exe local_finance_bot.py publication-license-status
```

Rollback: restore the task backup under
`outputs/backups/market_trigger_flow_<timestamp>/`, then restart the daemon.
The append-only JSONL evidence can be retained for audit after rollback.

## xAI X Social Intelligence

xAIは広域トレンド集計器ではなく、RSS、公式情報、FX・マルチアセット急変から
ローカル生成した最大5件のイベントについて、X上の観測反応を調査する補助層です。
事実確定、価格生成、売買推奨、為替介入の断定、Xへの直接投稿は行いません。
検索結果の件数は「同じ検索条件内で観測された独立投稿数」であり、X全体の件数や
世論を表しません。

モードは`event_reaction`、`movement_explanation`、`expert_watch`、
`exploration`の4種類です。探索枠は総費用の20%未満、同一イベントは既定60分の
クールダウンを設けます。通常日2実行、重要イベント日4実行、60分キャッシュ、
月額20ドルのhard limitは既存の予算台帳で継続管理します。1回の費用が警告値を
超えた場合は、次回のイベント数を自動的に縮小します。

観測結果は`data/xai/`の`runs.jsonl`、`events.jsonl`、
`observations.jsonl`、`posts.jsonl`、`accounts.jsonl`、
`content_opportunities.jsonl`へ保存します。各イベントの最新観測は銘柄・通貨・
主体・話題語で関連付け、一致見解、単独見解、反論、誤情報候補、未確認事項、
市場含意、証拠品質をローカルで統合分析します。結果は
`integrated_analyses.jsonl`へ保存し、この処理による追加API費用は発生しません。
統合結果は自動投稿せず、証拠品質と追加確認の要否を投稿判断へ渡します。
新しい観測が入るたびに分析を版管理し、前版からの要約・証拠品質・確認事項・
投稿準備状態の変化を記録します。編集用briefは`integrated_drafts.jsonl`、
News候補や実投稿での利用履歴は`integrated_analysis_usage.jsonl`へ保存します。
News生成では統合結果を市場解釈の参考に限定し、正式ソースを事実基準とします。
FX・市場急変の統合結果は各機能の`integrated_context.jsonl`へ展開しますが、
未確認の因果関係や為替介入を確定扱いしません。
旧`topic_radar.jsonl`は、APIを再実行せず`possible`かつ確認必須の観測として
新DBへ冪等移行できます。関連グループは主銘柄・意味類似度で作り、既定12件を
上限に分割して過度に広いテーマ統合を防ぎます。
投稿抜粋は短く制限し、生結果7日、
正規化投稿30日、観測集計180日の保持方針です。破損JSONLは内容を再保存せず、
ハッシュだけを隔離します。アカウントの人手修正は
`config/xai_account_watchlist.json`で行います。

```powershell
.\.venv\Scripts\python.exe local_finance_bot.py xai-status
.\.venv\Scripts\python.exe local_finance_bot.py xai-run --dry-run
.\.venv\Scripts\python.exe local_finance_bot.py xai-events
.\.venv\Scripts\python.exe local_finance_bot.py xai-event-show <event_id>
.\.venv\Scripts\python.exe local_finance_bot.py xai-observations <event_id>
.\.venv\Scripts\python.exe local_finance_bot.py xai-delta <event_id>
.\.venv\Scripts\python.exe local_finance_bot.py xai-accounts
.\.venv\Scripts\python.exe local_finance_bot.py xai-expert-watch
.\.venv\Scripts\python.exe local_finance_bot.py xai-exploration-status
.\.venv\Scripts\python.exe local_finance_bot.py xai-content-opportunities
.\.venv\Scripts\python.exe local_finance_bot.py xai-integrate --days 3
.\.venv\Scripts\python.exe local_finance_bot.py xai-import-legacy
.\.venv\Scripts\python.exe local_finance_bot.py xai-integrated-results
.\.venv\Scripts\python.exe local_finance_bot.py xai-integrated-show <analysis_id>
.\.venv\Scripts\python.exe local_finance_bot.py xai-integrated-drafts
.\.venv\Scripts\python.exe local_finance_bot.py xai-integrated-usage
.\.venv\Scripts\python.exe local_finance_bot.py xai-funnel
.\.venv\Scripts\python.exe local_finance_bot.py xai-roi
.\.venv\Scripts\python.exe local_finance_bot.py xai-cost-breakdown
.\.venv\Scripts\python.exe local_finance_bot.py xai-cache-status
.\.venv\Scripts\python.exe local_finance_bot.py xai-budget-status
.\.venv\Scripts\python.exe local_finance_bot.py xai-key-safety-status
.\.venv\Scripts\python.exe local_finance_bot.py xai-shadow-report --days 14
.\.venv\Scripts\python.exe local_finance_bot.py xai-social-report --days 30
```

`XAI_SCORE_BONUS_ENABLED`は明示的な強制フラグです。通常は
`XAI_SCORE_BONUS_AUTO_ENABLE=true`により、最低14日、20観測、
24時間実績の機械条件を満たした場合だけ自動的に有効になります。
候補ごとの人間レビューや承認待ちはありません。
`XAI_KEY_ROTATION_VERIFIED=false`の場合、statusと運用アラートは
`rotation verification required`を表示します。安全停止は
`XAI_SAFE_DISABLED=true`または`XAI_ENABLED=false`で行えます。
