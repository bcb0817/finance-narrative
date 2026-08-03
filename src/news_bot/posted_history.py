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
            "post_type": impact.get("post_type"),
            "topic": impact.get("topic") or impact.get("market_scope"),
            "tickers": impact.get("tickers", []),
            "with_image": mode in ("image", "diagram"),
            "source_type": impact.get("source_type"),
            "opinion_strength": impact.get("opinion_strength"),
            "x_topic_velocity": impact.get("x_topic_velocity", 0),
            "x_topic_acceleration": impact.get("x_topic_acceleration", 0),
            "radar_influenced": impact.get("radar_influenced", False),
            "xai_signal_used": impact.get("xai_signal_used", False),
            "xai_signal_reason": impact.get("xai_signal_reason"),
            "xai_priority_applied": impact.get("xai_priority_applied", False),
            "xai_researched": impact.get("xai_researched", False),
            "xai_run_id": impact.get("xai_run_id"),
            "xai_event_id": impact.get("xai_event_id"),
            "radar_run_id": impact.get("radar_run_id"),
            "radar_topic": impact.get("radar_topic"),
            "xai_cost_attribution_usd": impact.get("xai_cost_attribution_usd", 0),
            "observed_velocity_score": impact.get("observed_velocity_score"),
            "observed_acceleration_score": impact.get("observed_acceleration_score"),
            "source_confirmation": impact.get("source_confirmation"),
            "unique_accounts": impact.get("unique_accounts"),
            "independent_commentary_count": impact.get("independent_commentary_count"),
            "dominant_narrative": impact.get("dominant_narrative"),
            "dissent_present": impact.get("dissent_present", False),
            "misconception_present": impact.get("misconception_present", False),
            "official_participation": impact.get("official_participation", False),
            "xai_confidence": impact.get("xai_confidence"),
            "xai_integrated_analysis_id": impact.get("xai_integrated_analysis_id"),
            "xai_integrated_summary": impact.get("xai_integrated_summary"),
            "xai_integrated_evidence_quality": impact.get(
                "xai_integrated_evidence_quality"
            ),
            "xai_integrated_posting_readiness": impact.get(
                "xai_integrated_posting_readiness"
            ),
            "xai_integrated_requires_confirmation": impact.get(
                "xai_integrated_requires_confirmation", False
            ),
            "xai_integrated_context_used": impact.get(
                "xai_integrated_context_used", False
            ),
            "xai_integrated_priority_applied": False,
            "provider_isolated_editorial": impact.get(
                "provider_isolated_editorial", False
            ),
            "provider_isolated_reason": impact.get("provider_isolated_reason"),
            "provider_isolated_source_host": impact.get(
                "provider_isolated_source_host"
            ),
            "provider_isolated_topic": impact.get("provider_isolated_topic"),
            "market_data_provider_lineage": [],
            "twelvedata_used_for_post": False,
            "live_price_used": False,
            "provider_chart_used": False,
            "provider_isolated_text_gate": impact.get(
                "provider_isolated_text_gate"
            ),
            "independent_confirmation": impact.get(
                "independent_confirmation", False
            ),
            "independent_confirmation_decision": impact.get(
                "independent_confirmation_decision"
            ),
            "independent_confirmation_text_gate": impact.get(
                "independent_confirmation_text_gate"
            ),
            "internal_market_trigger_id": impact.get(
                "internal_market_trigger_id"
            ),
            "internal_market_trigger_symbol": impact.get(
                "internal_market_trigger_symbol"
            ),
            "internal_trigger_provider_lineage": impact.get(
                "internal_trigger_provider_lineage", []
            ),
            "causal_confidence": impact.get("causal_confidence"),
            "causal_claim_allowed": impact.get(
                "causal_claim_allowed", False
            ),
            "publication_mode": impact.get("publication_mode"),
            "public_evidence_bundle_id": impact.get(
                "public_evidence_bundle_id"
            ),
            "publication_candidate_id": impact.get(
                "publication_candidate_id"
            ),
            "publication_evidence_ids": impact.get(
                "publication_evidence_ids", []
            ),
            "structured_publication_status": impact.get(
                "structured_publication_status"
            ),
            "structured_output_validation": impact.get(
                "structured_output_validation"
            ),
            "structured_post_value": impact.get("structured_post_value"),
            "independent_source_title": impact.get(
                "independent_source_title"
            ),
            "independent_source_url": impact.get("independent_source_url"),
            "independent_source_name": impact.get("independent_source_name"),
            "twelvedata_internal_trigger": impact.get(
                "twelvedata_internal_trigger", False
            ),
            "diagram_value_score": impact.get("diagram_value_score"),
            "diagram_structure_type": impact.get("diagram_structure_type"),
            "diagram_reason": impact.get("diagram_reason"),
            "diagram_png_attached": impact.get("diagram_png_attached", False),
            "experiment_variant": impact.get("experiment_variant"),
            "experiment_hypothesis": impact.get("experiment_hypothesis"),
            "generated_model": os.getenv("OPENAI_GENERATE_MODEL", ""),
            "rationale": impact.get("reason", ""),
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
    if impact and impact.get("radar_run_id"):
        try:
            from common.xai_integration import record_downstream_event
            record_downstream_event(
                str(impact.get("radar_run_id") or ""),
                "post_created",
                tweet_id=str(tweet_id),
            )
        except Exception:
            logger.warning("xAI投稿成果イベントの記録に失敗（投稿履歴は維持）")
    if impact and impact.get("xai_run_id") and impact.get("xai_event_id"):
        try:
            from common.xai_social_intelligence import record_post_outcome
            record_post_outcome(
                run_id=str(impact.get("xai_run_id") or ""),
                event_id=str(impact.get("xai_event_id") or ""),
                tweet_id=str(tweet_id),
                title=item.title,
            )
        except Exception:
            logger.warning("xAI投稿outcome記録に失敗（投稿履歴は維持）")
    if impact and impact.get("xai_integrated_analysis_id"):
        try:
            from common.xai_social_intelligence import record_integrated_analysis_use
            record_integrated_analysis_use(
                analysis_id=str(impact.get("xai_integrated_analysis_id") or ""),
                use_type="post_created",
                reference_id=str(tweet_id),
            )
        except Exception:
            logger.warning("xAI統合分析の投稿成果記録に失敗（投稿履歴は維持）")
    logger.info(f"投稿履歴を保存しました: {item.url} (tweet_id={tweet_id})")
