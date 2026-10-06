# Finance X Radar

金融Botが作成した公開用DTOを受信し、`[finance_x_radar]` で最新5テーマを表示するWordPressプラグインです。X全体の統計や投稿全文は扱いません。

公開記事の末尾には、X、Facebook、Threads、LinkedIn、LINE、はてなブックマーク、リンクコピーの共有ボタンを自動表示します。共有先への自動投稿やSNS認証は行いません。
同じ欄に記事ごとの閲覧数を表示します。同一IP・ブラウザによる30分以内の再読み込みは重複計上しません。
投稿本文には金融メディア向けの共通タイポグラフィ、配色、表・シナリオ・注意書き・執筆者欄のスタイルを適用し、H2見出しから目次とセクション番号を自動生成します。

## 導入

1. `finance-x-radar.zip` を管理画面の「プラグイン > 新規追加 > アップロード」から導入して有効化します。
2. 固定ページ（推奨スラッグ `x-radar`）に `[finance_x_radar]` を置きます。
3. WordPressの「ユーザー > プロフィール > アプリケーションパスワード」で専用資格情報を作り、Bot側へ設定します。

送信先は `POST /wp-json/finance-radar/v1/snapshots`、最新取得は `GET /wp-json/finance-radar/v1/snapshots/latest` です。POSTにはApplication PasswordのBasic認証と `publish_posts` 権限が必要です。snapshotは表示名「finance_radar_snapshot」、WordPressの20文字制約に適合する内部キー`finance_radar_snap`のprivate CPTに保存されます。同一snapshot_idは更新、初期保持期間は30日です。`wp-config.php` の `FINANCE_RADAR_RETENTION_DAYS` 定数で変更できます。

## セキュリティ

HTTPS、128 KiB、最大5テーマ、各2リンク、文字長、日時、X URLを検証します。HTMLは除去し、表示時にもescapeします。個別snapshotは公開されません。
