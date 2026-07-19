"""
common/runtime.py
ローカル運用の共通基盤。
- .env の読み込み（標準ライブラリのみ・python-dotenv不要）
- リポジトリルート / STATE_DIR / OUTPUT_DIR / LOG_DIR の解決
- POST_ENABLED 安全弁
- decisions.jsonl / errors.jsonl / run_history.jsonl への追記
- logs/bot.log へのファイルロギング設定

秘密情報（APIキー等）は絶対にログへ出さないこと。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# repo_root = src/common/runtime.py から2つ上
REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_LOADED = False


def load_env(env_path: Path | None = None) -> None:
    """リポジトリ直下の .env を os.environ に読み込む（既存の環境変数を優先）。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    path = env_path or (REPO_ROOT / ".env")
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            pass
    _ENV_LOADED = True


def state_dir() -> Path:
    load_env()
    p = os.environ.get("STATE_DIR", "").strip()
    d = (REPO_ROOT / "data") if not p else (Path(p) if Path(p).is_absolute() else REPO_ROOT / p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir(sub: str = "") -> Path:
    load_env()
    p = os.environ.get("OUTPUT_DIR", "").strip()
    d = (REPO_ROOT / "outputs") if not p else (Path(p) if Path(p).is_absolute() else REPO_ROOT / p)
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    load_env()
    p = os.environ.get("LOG_DIR", "").strip()
    d = (REPO_ROOT / "logs") if not p else (Path(p) if Path(p).is_absolute() else REPO_ROOT / p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def post_enabled() -> bool:
    """POST_ENABLED=true のときだけ実投稿する（既定は false = 投稿しない）。"""
    load_env()
    return os.environ.get("POST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _append_jsonl(path: Path, record: dict) -> None:
    try:
        record = dict(record)
        record.setdefault("ts", datetime.now(JST).isoformat())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # ログ書き込み失敗でBotは止めない


def log_decision(record: dict) -> None:
    """投稿判断を logs/decisions.jsonl に1行追記。"""
    _append_jsonl(log_dir() / "decisions.jsonl", record)


def log_error(record: dict) -> None:
    """エラーを logs/errors.jsonl に1行追記。"""
    _append_jsonl(log_dir() / "errors.jsonl", record)


def log_run(record: dict) -> None:
    """run結果を logs/run_history.jsonl に1行追記。"""
    _append_jsonl(log_dir() / "run_history.jsonl", record)


_FILE_LOGGING_SET = False


def setup_file_logging() -> None:
    """標準出力に加えて logs/bot.log にも残す（多重追加を防止）。"""
    global _FILE_LOGGING_SET
    if _FILE_LOGGING_SET:
        return
    try:
        fh = logging.FileHandler(log_dir() / "bot.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass
    _FILE_LOGGING_SET = True
