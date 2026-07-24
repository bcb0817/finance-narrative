"""X投稿の1h/6h/24hパフォーマンスを安全に収集する。"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from runtime import JST, state_dir, log_error
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir, log_error

logger = logging.getLogger(__name__)
STAGES = (("1h", 1.0), ("6h", 6.0), ("24h", 24.0))
WINDOW_ENV = {
    "1h": ("METRICS_1H_WINDOW_START_MINUTES", "45", "METRICS_1H_WINDOW_END_MINUTES", "90"),
    "6h": ("METRICS_6H_WINDOW_START_MINUTES", "330", "METRICS_6H_WINDOW_END_MINUTES", "420"),
    "24h": ("METRICS_24H_WINDOW_START_MINUTES", "1380", "METRICS_24H_WINDOW_END_MINUTES", "1620"),
}


def _path() -> Path:
    return state_dir() / "metrics_snapshots.jsonl"


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=JST)).astimezone(JST)
    except (TypeError, ValueError):
        return None


def load_snapshots(path: Path | None = None) -> list[dict]:
    rows: list[dict] = []
    p = path or _path()
    if not p.exists():
        return rows
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("metrics snapshots read failed: %s", exc)
    return rows


def save_snapshot(snapshot: dict, path: Path | None = None) -> bool:
    """tweet_id + metrics_collected_at の重複を防いで追記する。"""
    p = path or _path()
    tid = str(snapshot.get("tweet_id") or "")
    collected = str(snapshot.get("metrics_collected_at") or "")
    if not tid or not collected:
        return False
    if any(str(r.get("tweet_id")) == tid and str(r.get("metrics_collected_at")) == collected
           for r in load_snapshots(p)):
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        logger.warning("metrics snapshot write failed: %s", type(exc).__name__)
        return False


def select_stage_snapshot(rows: list[dict], tweet_id: str, stage_hours: float) -> dict | None:
    candidates = [r for r in rows if str(r.get("tweet_id")) == str(tweet_id)
                  and r.get("post_age_hours") is not None]
    if not candidates:
        return None
    eligible = [r for r in candidates if float(r["post_age_hours"]) >= stage_hours]
    pool = eligible or candidates
    return min(pool, key=lambda r: abs(float(r["post_age_hours"]) - stage_hours))


def due_stage(age_hours: float, completed_stages: set[str]) -> str | None:
    """取り逃した過去stageを現在値で埋めず、現在の計測窓だけを返す。"""
    for stage in ("24h", "6h", "1h"):
        start_name,start_default,end_name,end_default=WINDOW_ENV[stage]
        start=float(os.getenv(start_name,start_default))/60.0
        end=float(os.getenv(end_name,end_default))/60.0
        if start <= age_hours <= end and stage not in completed_stages:
            return stage
    return None


def missed_stages(age_hours: float, completed_stages: set[str]) -> list[str]:
    result=[]
    for stage in ("1h","6h","24h"):
        _sn,_sd,end_name,end_default=WINDOW_ENV[stage]
        if age_hours > float(os.getenv(end_name,end_default))/60.0 and stage not in completed_stages:
            result.append(stage)
    return result


def _deadline_hours(stage: str) -> float:
    return float(os.getenv(WINDOW_ENV[stage][2], WINDOW_ENV[stage][3])) / 60.0


def _missed_reason(now: datetime) -> str:
    heartbeat = state_dir() / "daemon_heartbeat.json"
    try:
        value = json.loads(heartbeat.read_text(encoding="utf-8"))
        updated = _parse_dt(value.get("updated_at", ""))
        if not updated or (now - updated).total_seconds() > 15 * 60:
            return "daemon_unavailable"
    except (OSError, json.JSONDecodeError):
        return "daemon_unavailable"
    return "collection_window_expired"


def enrich_metrics(raw: dict, posted_at: str, collected_at: datetime) -> dict:
    posted = _parse_dt(posted_at)
    age = max((collected_at - posted).total_seconds() / 3600, 0.01) if posted else None
    impressions = raw.get("impressions")
    engagement = sum(int(raw.get(k) or 0) for k in ("likes", "reposts", "replies", "quotes"))
    follows=raw.get("follows"); profile=raw.get("profile_clicks")
    follow_conversion=(float(follows)/float(profile)) if follows is not None and profile not in (None,0) else None
    profile_click_rate=(float(profile)/float(impressions)) if profile is not None and impressions not in (None,0) else None
    repost_rate=(float(raw.get("reposts") or 0)/float(impressions)) if impressions not in (None,0) else None
    quote_rate=(float(raw.get("quotes") or 0)/float(impressions)) if impressions not in (None,0) else None
    reply_rate=(float(raw.get("replies") or 0)/float(impressions)) if impressions not in (None,0) else None
    like_rate=(float(raw.get("likes") or 0)/float(impressions)) if impressions not in (None,0) else None
    iph=(float(impressions)/age) if impressions is not None and age else None
    available=[v for v in (follow_conversion,profile_click_rate,repost_rate,iph) if v is not None]
    growth_score=(sum(available)/len(available)) if available else None
    return {
        **raw,
        "status":"collected",
        "post_age_hours": round(age, 3) if age is not None else None,
        "impressions_per_hour": round(iph,3) if iph is not None else None,
        "follow_conversion":round(follow_conversion,6) if follow_conversion is not None else None,
        "follow_conversion_status":"measured" if follow_conversion is not None else "unavailable",
        "follow_conversion_source":"x_api" if follow_conversion is not None else None,
        "estimated_follow_conversion":None,
        "profile_click_rate":round(profile_click_rate,6) if profile_click_rate is not None else None,
        "repost_rate":round(repost_rate,6) if repost_rate is not None else None,
        "quote_rate":round(quote_rate,6) if quote_rate is not None else None,
        "reply_rate":round(reply_rate,6) if reply_rate is not None else None,
        "like_rate":round(like_rate,6) if like_rate is not None else None,
        "growth_score":round(growth_score,6) if growth_score is not None else None,
        "engagement_rate": round(engagement / float(impressions), 6)
        if impressions not in (None, 0) else None,
        "metrics_collected_at": collected_at.isoformat(),
    }


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "response_code", None)


def _fetch_batch(client, ids: list[str], attempts: int = 3):
    fields = ["public_metrics", "created_at"]
    for attempt in range(attempts):
        try:
            return client.get_tweets(ids=ids, tweet_fields=fields)
        except Exception as exc:
            code = _status_code(exc)
            logger.warning("X metrics fetch failed status=%s attempt=%s", code or "unknown", attempt + 1)
            if code == 429 or code is None or code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
            raise


def collect_metrics(*, client=None, now: datetime | None = None) -> dict:
    if os.getenv("X_METRICS_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return {"status": "disabled", "saved": 0}
    now = (now or datetime.now(JST)).astimezone(JST)
    max_age = float(os.getenv("X_METRICS_MAX_POST_AGE_HOURS", "48"))
    history_path = state_dir() / "posted_history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    existing = load_snapshots()
    completed = {(str(r.get("tweet_id")), str(r.get("stage"))) for r in existing}
    due: list[tuple[dict, str]] = []
    for post in history if isinstance(history, list) else []:
        tid, posted = str(post.get("tweet_id") or ""), _parse_dt(post.get("posted_at", ""))
        if not tid or not posted:
            continue
        age = (now - posted).total_seconds() / 3600
        if age < 0:
            continue
        done={s for t,s in completed if t==tid}
        for missed in missed_stages(age,done):
            row={"tweet_id":tid,"stage":missed,"status":"missed","reason":_missed_reason(now),
                 "expected_window_start_minutes":int(os.getenv(WINDOW_ENV[missed][0],WINDOW_ENV[missed][1])),
                 "expected_window_end_minutes":int(os.getenv(WINDOW_ENV[missed][2],WINDOW_ENV[missed][3])),
                 "metrics_collected_at":now.isoformat(),"post_age_hours":round(age,3)}
            if save_snapshot(row): completed.add((tid,missed))
        if age > max_age: continue
        stage = due_stage(age, {s for t, s in completed if t == tid})
        if stage:
            due.append((post, stage))
    due.sort(key=lambda item: (
        (_parse_dt(item[0].get("posted_at", "")) or now) + timedelta(hours=_deadline_hours(item[1]))
    ))
    if not due:
        return {"status": "ok", "saved": 0, "due": 0}
    if client is None:
        try:
            from x_client import get_metrics_client
        except ImportError:
            from common.x_client import get_metrics_client
        try:
            client = get_metrics_client()
        except Exception as exc:
            logger.warning("metrics client unavailable; daemon continues: %s", type(exc).__name__)
            return {"status": "error", "saved": 0, "error_type": type(exc).__name__}
    saved = 0
    posts_by_id = {str(p[0].get("tweet_id")): p[0] for p in due}
    stages_by_id: dict[str, list[str]] = {}
    for post, stage in due:
        stages_by_id.setdefault(str(post["tweet_id"]), []).append(stage)
    try:
        tweets = []
        ids = list(posts_by_id)
        for index in range(0, len(ids), 100):
            response = _fetch_batch(client, ids[index:index + 100])
            tweets.extend(response.data or [])
        for tweet in tweets:
            tid = str(tweet.id)
            post = posts_by_id.get(tid, {})
            public = getattr(tweet, "public_metrics", None) or {}
            base = {
                "tweet_id": tid,
                "impressions": public.get("impression_count"),
                "likes": public.get("like_count"), "reposts": public.get("retweet_count"),
                "replies": public.get("reply_count"), "quotes": public.get("quote_count"),
                "bookmarks": public.get("bookmark_count"),
                "url_clicks": None, "profile_clicks": None, "follows": None,
            }
            for stage in stages_by_id.get(tid, []):
                row = enrich_metrics({**base, "stage": stage}, post.get("posted_at", ""), now)
                saved += int(save_snapshot(row))
        returned = {str(tweet.id) for tweet in tweets}
        for tid in set(posts_by_id) - returned:
            post = posts_by_id[tid]
            posted = _parse_dt(post.get("posted_at", ""))
            age = (now - posted).total_seconds() / 3600 if posted else None
            for stage in stages_by_id.get(tid, []):
                saved += int(save_snapshot({
                    "tweet_id": tid, "stage": stage, "status": "unavailable",
                    "reason": "post_missing_or_deleted", "post_age_hours": round(age, 3) if age is not None else None,
                    "metrics_collected_at": now.isoformat(),
                }))
    except Exception as exc:
        code = _status_code(exc)
        log_error({"bot": "metrics", "status_code": code, "error_type": type(exc).__name__})
        logger.exception("metrics collector continued after X API failure")
        return {"status": "error", "saved": saved, "status_code": code}
    return {"status": "ok", "saved": saved, "due": len(due)}


def metrics_status() -> dict:
    rows=load_snapshots(); counts={}; stages={}; reasons={}
    for row in rows:
        status=row.get("status","collected"); counts[status]=counts.get(status,0)+1
        stage=str(row.get("stage") or "unknown"); stages.setdefault(stage,{})
        stages[stage][status]=stages[stage].get(status,0)+1
        if row.get("reason"): reasons[row["reason"]]=reasons.get(row["reason"],0)+1
    completed=counts.get("collected",0); missed=counts.get("missed",0)
    denominator=completed+missed
    return {"total":len(rows),"by_status":counts,"by_stage":stages,"missed_reasons":reasons,
            "collection_success_rate":round(completed/denominator,4) if denominator else None,
            "missed_count":missed,
            "available_kpis":["follow_conversion","profile_click_rate","repost_rate","impressions_per_hour",
                              "quote_rate","reply_rate","like_rate","total_impressions"],
            "unavailable_values_are_null":True}
