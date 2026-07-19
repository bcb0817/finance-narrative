# finance-narrative（ローカル運用版）

米国株向けのX自動投稿Bot 4系統。**GitHub Actions からローカル運用に移行しました。**
運用の中心は `local_finance_bot.py` です（**Windows / macOS / Linux 対応**）。GitHub Actions のスケジュールは無効化済みです
（`.github/workflows/` は削除して構いません）。

## Bot構成

| Bot | 内容 | 既定スケジュール |
|---|---|---|
| news | ニュース図解/要約投稿 | 30分間隔（`config/schedule.json`） |
| narrative | 市場ナラティブ | 米国営業日 08:30 / 09:35 / 16:05 ET |
| market-map | 寄り直後ヒートマップ | 米国営業日 09:35 ET |
| weekly | 週間注目イベント | 毎週日曜 21:00 JST |

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
- 投稿価値ゲート: news は post_value>=7 かつ 関連度/話題性ゲート、narrative は post_value>=8
- 履歴は**投稿成功後だけ**保存。失敗・POST_ENABLED=false では保存しない

## 今後の改善用（Botに読ませて改善する想定のファイル）

`config/bot_persona.md` / `config/finance_tone.md` /
`knowledge/viral_patterns/` / `knowledge/failed_patterns/` /
`knowledge/source_notes/` / `knowledge/ticker_notes/`
