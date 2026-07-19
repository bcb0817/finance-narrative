"""
common/post_registry.py
全Bot共通の投稿履歴レジストリ。

- Xへの実投稿が成功した親投稿だけを data/posted_history.json に保存
- tweet_id で重複排除し、News Bot固有のURL/スコア情報は後からマージ可能
- レポート・日次学習が全Bot（news/narrative/weekly/market-map）を横断できるようにする
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from runtime import state_dir, JST
except ImportError:  # pragma: no cover
    from common.runtime import state_dir, JST

logger = logging.getLogger(__name__)

MAX_ENTRIES = 1000
RETENTION_DAYS = 30


def _history_file() -> Path:
    return state_dir() / "posted_history.json"


def _load_history() -> list[dict]:
    path = _history_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("投稿履歴の読み込みに失敗しました: %s", e)
        return []


def _save_history(entries: list[dict]) -> None:
    _history_file().write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt
    except (TypeError, ValueError):
        return None


def _prune(entries: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept: list[dict] = []
    for entry in entries:
        dt = _parse_dt(entry.get("posted_at", ""))
        if dt is None or dt.astimezone(timezone.utc) >= cutoff:
            kept.append(entry)
    return kept[-MAX_ENTRIES:]


def record_post(
    tweet_id: str,
    *,
    text: str = "",
    title: str = "",
    source: str = "",
    url: str = "",
    bot: str = "",
    mode: str = "",
    posted_at: str = "",
    extra: dict | None = None,
) -> None:
    """投稿成功後の親投稿を記録する。失敗しても投稿処理自体は止めない。"""
    tid = str(tweet_id or "").strip()
    if not tid:
        return

    resolved_bot = (bot or os.environ.get("FINANCE_BOT_NAME", "") or "unknown").strip()
    resolved_mode = (mode or os.environ.get("FINANCE_BOT_MODE", "") or "").strip()
    clean_text = (text or "").strip()
    clean_title = (title or (clean_text.splitlines()[0] if clean_text else "")).strip()

    incoming = {
        "tweet_id": tid,
        "text": clean_text,
        "title": clean_title,
        "source": (source or resolved_bot).strip(),
        "url": (url or "").strip(),
        "posted_at": posted_at or datetime.now(JST).isoformat(),
        "mode": resolved_mode,
        "bot": resolved_bot,
    }
    if extra:
        incoming.update({k: v for k, v in extra.items() if v is not None})

    try:
        entries = _load_history()
        existing = next((e for e in entries if str(e.get("tweet_id", "")) == tid), None)
        if existing is None:
            entries.append(incoming)
        else:
            # 空値で既存の詳細情報を消さない
            for key, value in incoming.items():
                if value not in ("", None, [], {}):
                    existing[key] = value

        _save_history(_prune(entries))
        logger.info("共通投稿履歴を保存しました: bot=%s tweet_id=%s", resolved_bot, tid)
    except OSError as e:
        logger.warning("共通投稿履歴の保存に失敗（投稿は維持）: %s", e)
