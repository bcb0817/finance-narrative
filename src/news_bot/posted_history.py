import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from news import NewsItem

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# リポジトリ直下の data/ を正とする（STATE_DIR 環境変数で変更可能）。
# 旧実装は src/ 起点で "src/data/posted_history.json" を見にいくバグがあった。
REPO_ROOT = Path(__file__).resolve().parents[2]


def _history_file() -> Path:
    p = os.environ.get("STATE_DIR", "").strip()
    state = (REPO_ROOT / "data") if not p else (Path(p) if Path(p).is_absolute() else REPO_ROOT / p)
    state.mkdir(parents=True, exist_ok=True)
    target = state / "posted_history.json"

    # 旧配置 src/data/posted_history.json が残っていれば1回だけ移行する
    legacy = REPO_ROOT / "src" / "data" / "posted_history.json"
    if legacy.exists() and not target.exists():
        try:
            target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info(f"旧履歴を移行しました: {legacy} -> {target}")
        except OSError as e:
            logger.warning(f"旧履歴の移行に失敗（続行）: {e}")
    return target


def _post_enabled() -> bool:
    return os.environ.get("POST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


MAX_ENTRIES = 500
RETENTION_DAYS = 30


def load_history() -> list[dict]:
    hf = _history_file()
    if not hf.exists():
        return []

    try:
        data = json.loads(hf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"投稿履歴の読み込みに失敗しました: {e}")
        return []

    if not isinstance(data, list):
        logger.warning("投稿履歴の形式が不正です。空の履歴として扱います。")
        return []

    return data


def save_history(entries: list[dict]) -> None:
    hf = _history_file()
    hf.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_posted_urls() -> set[str]:
    return {entry["url"] for entry in load_history() if entry.get("url")}


# =========================================================
# 評価済み履歴（#3/#4/#5）: 投稿したかに関わらず「評価した」URL/タイトルを記録し、
# 一定時間（既定6h、EVALUATED_TTL_HOURSで変更可）以内の再評価を防ぐ。
# =========================================================
import re as _re

EVALUATED_TTL_HOURS = int(os.environ.get("EVALUATED_TTL_HOURS", "6") or 6)


def _evaluated_file() -> Path:
    return _history_file().parent / "evaluated_history.json"


def normalize_title(title: str) -> str:
    """再評価防止用の正規化タイトル（小文字化・空白/記号除去）。"""
    t = (title or "").lower()
    t = _re.sub(r"\s+", "", t)
    t = _re.sub(r"[^\w]", "", t, flags=_re.UNICODE)
    return t


def _load_evaluated() -> list[dict]:
    f = _evaluated_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _prune_evaluated(entries: list[dict], ttl_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    kept = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e.get("evaluated_at", ""))
        except (TypeError, ValueError):
            continue
        if ts.astimezone(timezone.utc) >= cutoff:
            kept.append(e)
    return kept[-1000:]


def recently_evaluated(url: str, title: str, ttl_hours: int | None = None) -> tuple[bool, bool]:
    """(url一致で評価済み, 正規化タイトル一致で評価済み) を返す。TTL外は無視。"""
    ttl = EVALUATED_TTL_HOURS if ttl_hours is None else ttl_hours
    entries = _prune_evaluated(_load_evaluated(), ttl)
    nt = normalize_title(title)
    url_hit = any(e.get("url") and e.get("url") == url for e in entries)
    title_hit = bool(nt) and any(e.get("norm_title") == nt for e in entries)
    return url_hit, title_hit


def record_evaluated(url: str, title: str, skip_reason: str = "", should_post: bool = False) -> None:
    """評価したニュースを記録（投稿の成否に関係なく残す）。"""
    entries = _prune_evaluated(_load_evaluated(), EVALUATED_TTL_HOURS)
    entries.append({
        "url": url or "",
        "title": title or "",
        "norm_title": normalize_title(title),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "should_post": bool(should_post),
        "skip_reason": skip_reason or "",
    })
    try:
        _evaluated_file().write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning(f"評価済み履歴の保存に失敗（続行）: {e}")


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


def add_posted_entry(
    item: "NewsItem",
    tweet_id: str,
    mode: str,
    impact: dict | None = None,
    text: str = "",
) -> None:
    """News固有情報を共通投稿履歴へマージする。"""
    if not _post_enabled() or not tweet_id:
        logger.info("[INFO] POST_ENABLED=false or 未投稿のため履歴保存をスキップ")
        return

    try:
        from post_registry import record_post
    except ImportError:
        from common.post_registry import record_post

    extra = {}
    if impact:
        extra.update({
            "post_value": impact.get("post_value"),
            "us_equity_relevance": impact.get("us_equity_relevance"),
            "social_buzz_score": impact.get("social_buzz_score"),
            "narrative_value": impact.get("narrative_value"),
            "theme_relevance": impact.get("theme_relevance"),
            "market_scope": impact.get("market_scope"),
            "pass_path": impact.get("pass_path"),
        })

    record_post(
        tweet_id,
        text=text,
        title=item.title,
        source=item.source,
        url=item.url,
        bot="news",
        mode=mode,
        extra=extra,
    )
    logger.info(f"投稿履歴を保存しました: {item.url} (tweet_id={tweet_id})")
