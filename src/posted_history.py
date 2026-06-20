import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from news import NewsItem

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = REPO_ROOT / "data" / "posted_history.json"
MAX_ENTRIES = 500
RETENTION_DAYS = 30


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"投稿履歴の読み込みに失敗しました: {e}")
        return []

    if not isinstance(data, list):
        logger.warning("投稿履歴の形式が不正です。空の履歴として扱います。")
        return []

    return data


def save_history(entries: list[dict]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_posted_urls() -> set[str]:
    return {entry["url"] for entry in load_history() if entry.get("url")}


def _parse_posted_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def prune_history(entries: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept: list[dict] = []

    for entry in entries:
        posted_at = _parse_posted_at(entry.get("posted_at", ""))
        if posted_at is None:
            kept.append(entry)
            continue
        if posted_at.astimezone(timezone.utc) >= cutoff:
            kept.append(entry)

    if len(kept) > MAX_ENTRIES:
        kept = kept[-MAX_ENTRIES:]

    return kept


def add_posted_entry(item: "NewsItem", tweet_id: str, mode: str) -> None:
    entries = load_history()
    if any(entry.get("url") == item.url for entry in entries):
        logger.info(f"投稿済みURLのため履歴追加をスキップ: {item.url}")
        return

    entries.append({
        "url": item.url,
        "title": item.title,
        "source": item.source,
        "posted_at": datetime.now(JST).isoformat(),
        "tweet_id": tweet_id,
        "mode": mode,
    })
    entries = prune_history(entries)
    save_history(entries)
    logger.info(f"投稿履歴を保存しました: {item.url} (tweet_id={tweet_id})")
