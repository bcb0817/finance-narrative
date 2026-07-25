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
    path = _history_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _registry_file() -> Path:
    return state_dir() / "post_registry.jsonl"


def _classify_theme(text: str) -> str:
    value = (text or "").lower()
    groups = (("semiconductor", ("半導体", "nvidia", "amd", "chip")),
              ("AI", (" ai ", "人工知能", "生成ai")),
              ("mega_tech", ("apple", "microsoft", "amazon", "meta", "tesla")),
              ("interest_rate", ("金利", "fed", "fomc", "利下げ", "利上げ")),
              ("currency", ("為替", "ドル", "円")), ("energy", ("原油", "oil", "energy")),
              ("earnings", ("決算", "earnings")), ("macro", ("cpi", "gdp", "雇用")))
    return next((name for name, keys in groups if any(k in value for k in keys)), "other")


def _append_registry(record: dict) -> None:
    path = _registry_file(); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _post_type(bot: str, mode: str) -> str:
    if mode in ("image", "diagram"): return "diagram"
    if bot in ("weekly", "market-map"): return "scheduled_summary"
    if mode in ("breaking", "breaking_news"): return "breaking_news"
    if mode == "contrarian": return "contrarian"
    return "explanation"


def _hook_type(text: str) -> str:
    value = (text or "").strip()
    first = value.splitlines()[0] if value else ""
    if first.endswith(("?", "？")): return "question"
    if any(ch.isdigit() for ch in first[:15]): return "number_first"
    if "一方" in first or "対して" in first: return "comparison"
    if "誤解" in first or "実は" in first: return "misconception"
    if any(word in first for word in ("決算", "発表", "提携", "買収")): return "company_plus_event"
    return "conclusion_first"


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


def hours_since_last_post(now: datetime | None = None) -> float | None:
    """全Bot共通履歴から、最後の実投稿からの経過時間を返す。履歴なしは None。"""
    current = now or datetime.now(JST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=JST)
    posted = [dt for entry in _load_history()
              if (dt := _parse_dt(entry.get("posted_at", ""))) is not None]
    if not posted:
        return None
    latest = max(dt.astimezone(JST) for dt in posted)
    return max(0.0, (current.astimezone(JST) - latest).total_seconds() / 3600)


def posting_inactive(hours: float = 3.0, now: datetime | None = None) -> bool:
    """指定時間以上、実投稿がないかを判定する。履歴なしも休止状態として扱う。"""
    elapsed = hours_since_last_post(now)
    return elapsed is None or elapsed >= max(0.0, hours)


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
    notify_discord: bool = False,
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
        "post_type": _post_type(resolved_bot, resolved_mode),
        "theme": _classify_theme(f"{clean_title} {clean_text}"),
        "image_type": None,
        "opening_30_chars": clean_text[:30],
        "character_count": len(clean_text),
        "post_value": None,
        "time_slot": datetime.now(JST).strftime("%H:00"),
        "experiment_id": f"{datetime.now(JST):%Y%m%d}-{resolved_bot}-{tid[-6:]}",
        "hook_type": _hook_type(clean_text),
        "tickers": [],
        "with_image": resolved_mode in ("image", "diagram", "market-map"),
        "topic": "",
        "posted_window": datetime.now(JST).strftime("%H:00"),
        "x_topic_velocity": 0.0,
        "x_topic_acceleration": 0.0,
        "radar_influenced": False,
        "xai_signal_used": False,
        "xai_signal_reason": None,
        "radar_run_id": None,
        "radar_topic": None,
        "xai_cost_attribution_usd": 0.0,
        "observed_velocity_score": None,
        "observed_acceleration_score": None,
        "source_confirmation": None,
        "diagram_value_score": None,
        "diagram_structure_type": None,
        "diagram_reason": None,
        "diagram_png_attached": False,
        "source_type": "",
        "opinion_strength": "",
        "experiment_variant": "",
        "experiment_hypothesis": "",
    }
    if extra:
        incoming.update({k: v for k, v in extra.items() if v is not None})

    try:
        entries = _load_history()
        existing = next((e for e in entries if str(e.get("tweet_id", "")) == tid), None)
        if existing is None:
            entries.append(incoming)
            merged = incoming
        else:
            # 空値で既存の詳細情報を消さない
            for key, value in incoming.items():
                if value not in ("", None, [], {}):
                    existing[key] = value
            merged = existing

        # 初回・後続メタデータ更新の両方をイベントとして追記する。
        _append_registry({**merged, "event_type": "created" if existing is None else "metadata_update"})

        _save_history(_prune(entries))
        logger.info("共通投稿履歴を保存しました: bot=%s tweet_id=%s", resolved_bot, tid)
        if notify_discord and existing is None:
            try:
                from common.operations_alerts import notify_x_post
                notify_x_post(merged)
            except Exception as exc:
                logger.warning("Discord投稿通知に失敗（X投稿は維持）: %s",type(exc).__name__)
    except OSError as e:
        logger.warning("共通投稿履歴の保存に失敗（投稿は維持）: %s", e)
