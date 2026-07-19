"""
common/report.py
投稿実績（インプレッション/いいね/RT/返信）をX APIから取得し、
「何が伸びたか」を集計してレポートする。

- posted_history.json の tweet_id を使って X API v2 から metrics を取得
- 取得した実績は data/metrics_history.json にキャッシュ（再取得しても増分だけ）
- テーマ(market_scope) / スコア帯(post_value) / 通過経路(pass_path) / 時間帯 別に集計

注意:
- non_public_metrics（インプレッション）は「自分の投稿」かつ有料プランでのみ取得可能。
  取得できない場合は public_metrics（いいね/RT/返信）だけで集計する。
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from runtime import state_dir, JST, log_run
except ImportError:  # pragma: no cover
    from common.runtime import state_dir, JST, log_run


def _metrics_file() -> Path:
    return state_dir() / "metrics_history.json"


def _history_file() -> Path:
    return state_dir() / "posted_history.json"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def fetch_metrics(max_age_days: int = 30) -> list[dict]:
    """posted_history の tweet_id について X API から実績を取得する。

    戻り値: [{tweet_id, impressions, likes, retweets, replies, ...}, ...]
    """
    import tweepy
    try:
        from x_client import get_tweepy_client
    except ImportError:
        from common.x_client import get_tweepy_client

    history = _load(_history_file(), [])
    cached_metrics = _load(_metrics_file(), [])
    cached_by_id = {
        str(item.get("tweet_id")): item
        for item in cached_metrics if item.get("tweet_id")
    }
    now = datetime.now(JST)
    cutoff = now - timedelta(days=max_age_days)

    targets = []
    seven_day_candidates = []
    target_stage: dict[str, str] = {}
    for e in history:
        tid = str(e.get("tweet_id") or "").strip()
        if not tid:
            continue
        try:
            posted = datetime.fromisoformat(e["posted_at"])
        except (KeyError, ValueError):
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=JST)
        if posted < cutoff:
            continue
        age = now - posted
        cached = cached_by_id.get(tid)
        if cached is None:
            if age >= timedelta(hours=24):
                targets.append((tid, e))
                target_stage[tid] = "24h"
        elif age >= timedelta(days=7) and not cached.get("seven_day_fetched_at"):
            score = sum(int(cached.get(key, 0) or 0) for key in (
                "likes", "retweets", "replies", "quotes", "bookmarks"))
            seven_day_candidates.append((score, tid, e))

    if seven_day_candidates:
        seven_day_candidates.sort(reverse=True, key=lambda item: item[0])
        top_count = max(1, (len(cached_by_id) + 4) // 5)
        for _score, tid, entry in seven_day_candidates[:top_count]:
            targets.append((tid, entry))
            target_stage[tid] = "7d"

    if not targets:
        logger.info("X metrics refresh is not due; using cached metrics")
        return list(cached_by_id.values())

    client = get_tweepy_client()
    results: list[dict] = []

    # X API v2 は最大100件ずつ取得できる
    for i in range(0, len(targets), 100):
        chunk = targets[i:i + 100]
        ids = [t[0] for t in chunk]
        try:
            resp = client.get_tweets(
                ids=ids,
                tweet_fields=["public_metrics", "non_public_metrics", "created_at"],
            )
        except tweepy.TweepyException as e:
            # non_public_metrics は権限不足だと失敗する → public のみで再試行
            logger.warning(f"non_public_metrics取得に失敗、public_metricsのみで再試行: {e}")
            try:
                resp = client.get_tweets(
                    ids=ids, tweet_fields=["public_metrics", "created_at"])
            except tweepy.TweepyException as e2:
                logger.error(f"実績取得に失敗しました: {e2}")
                continue

        by_id = {t[0]: t[1] for t in chunk}
        for tw in (resp.data or []):
            tid = str(tw.id)
            entry = by_id.get(tid, {})
            pm = getattr(tw, "public_metrics", None) or {}
            npm = getattr(tw, "non_public_metrics", None) or {}
            previous = cached_by_id.get(tid, {})
            stage = target_stage.get(tid, "24h")
            record = {
                "tweet_id": tid,
                "text": getattr(tw, "text", None) or entry.get("text", ""),
                "title": entry.get("title", ""),
                "posted_at": entry.get("posted_at", ""),
                "mode": entry.get("mode", ""),
                "bot": entry.get("bot", "news"),
                "source": entry.get("source", ""),
                "post_value": entry.get("post_value"),
                "market_scope": entry.get("market_scope"),
                "pass_path": entry.get("pass_path"),
                # APIプラン/レスポンス差異に備え、non_public → public の順で見る
                "impressions": (
                    npm.get("impression_count")
                    if npm.get("impression_count") is not None
                    else pm.get("impression_count")
                ),
                "likes": pm.get("like_count", 0),
                "retweets": pm.get("retweet_count", 0),
                "replies": pm.get("reply_count", 0),
                "quotes": pm.get("quote_count", 0),
                "bookmarks": pm.get("bookmark_count", 0),
                "fetched_at": datetime.now(JST).isoformat(),
                "first_fetched_at": previous.get("first_fetched_at") or datetime.now(JST).isoformat(),
                "seven_day_fetched_at": (
                    datetime.now(JST).isoformat()
                    if stage == "7d" else previous.get("seven_day_fetched_at")
                ),
                "fetch_stage": stage,
            }
            cached_by_id[tid] = record
            results.append(record)

    merged = list(cached_by_id.values())

    try:
        _metrics_file().write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning(f"metrics_history の保存に失敗（続行）: {e}")

    logger.info("X metrics refreshed=%d cached_total=%d", len(results), len(merged))
    return merged


def _engagement(m: dict) -> int:
    return (m.get("likes", 0) + m.get("retweets", 0)
            + m.get("replies", 0) + m.get("quotes", 0) + m.get("bookmarks", 0))


def _fmt(v) -> str:
    return "-" if v is None else f"{v:,}"


def _group_summary(metrics: list[dict], key: str, label: str) -> list[str]:
    """key別にインプレッション/エンゲージメントの平均を出す。"""
    groups = defaultdict(list)
    for m in metrics:
        k = m.get(key)
        if k in (None, ""):
            k = "(不明)"
        groups[str(k)].append(m)

    rows = []
    for k, items in groups.items():
        imps = [m["impressions"] for m in items if m.get("impressions") is not None]
        engs = [_engagement(m) for m in items]
        rows.append((
            k, len(items),
            statistics.mean(imps) if imps else None,
            statistics.mean(engs) if engs else 0,
        ))
    # 平均インプレ（無ければエンゲージ）降順
    rows.sort(key=lambda r: (r[2] if r[2] is not None else -1, r[3]), reverse=True)

    out = [f"--- {label}別 ---"]
    for k, n, avg_imp, avg_eng in rows:
        ai = f"{avg_imp:,.0f}" if avg_imp is not None else "-"
        out.append(f"  {k:16s} 投稿{n:3d}件  平均imp={ai:>9s}  平均エンゲージ={avg_eng:.1f}")
    return out


def build_report(days: int = 1) -> str:
    """直近days日の投稿実績レポートを文字列で返す。"""
    metrics = fetch_metrics(max_age_days=30)
    if not metrics:
        return "レポート対象の投稿がありません（まだ実投稿していない可能性があります）。"

    cutoff = datetime.now(JST) - timedelta(days=days)
    recent = []
    for m in metrics:
        try:
            if datetime.fromisoformat(m["posted_at"]) >= cutoff:
                recent.append(m)
        except (KeyError, ValueError):
            continue

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"投稿実績レポート  {datetime.now(JST):%Y-%m-%d %H:%M} JST")
    lines.append("=" * 60)

    has_imp = any(m.get("impressions") is not None for m in metrics)
    if not has_imp:
        lines.append("[注意] インプレッションを取得できませんでした。")
        lines.append("       X APIプランの権限（non_public_metrics）をご確認ください。")
        lines.append("       いいね/RT/返信のみで集計します。")
        lines.append("")

    # --- 直近days日のサマリ ---
    lines.append(f"■ 直近{days}日（{len(recent)}件）")
    if recent:
        imps = [m["impressions"] for m in recent if m.get("impressions") is not None]
        lines.append(f"  合計インプレッション : {_fmt(sum(imps)) if imps else '-'}")
        lines.append(f"  平均インプレッション : {f'{statistics.mean(imps):,.0f}' if imps else '-'}")
        lines.append(f"  合計いいね           : {sum(m['likes'] for m in recent):,}")
        lines.append(f"  合計リポスト         : {sum(m['retweets'] for m in recent):,}")
        lines.append(f"  合計返信             : {sum(m['replies'] for m in recent):,}")
    else:
        lines.append("  投稿なし")
    lines.append("")

    # --- 伸びた投稿 TOP5（全期間） ---
    ranked = sorted(
        metrics,
        key=lambda m: (m.get("impressions") if m.get("impressions") is not None else -1,
                       _engagement(m)),
        reverse=True,
    )
    lines.append("■ 伸びた投稿 TOP5（過去30日）")
    for i, m in enumerate(ranked[:5], 1):
        lines.append(
            f"  {i}. imp={_fmt(m.get('impressions')):>9s} "
            f"♡{m['likes']:<3d} RT{m['retweets']:<3d} 返信{m['replies']:<3d}"
        )
        lines.append(f"     {m['title'][:60]}")
        lines.append(
            f"     scope={m.get('market_scope','-')} / post_value={m.get('post_value','-')} "
            f"/ 経路={m.get('pass_path','-')}"
        )
    lines.append("")

    # --- 伸びなかった投稿 TOP3 ---
    lines.append("■ 伸びなかった投稿 WORST3（過去30日）")
    for i, m in enumerate(ranked[-3:][::-1], 1):
        lines.append(
            f"  {i}. imp={_fmt(m.get('impressions')):>9s} "
            f"♡{m['likes']:<3d} RT{m['retweets']:<3d}  {m['title'][:50]}"
        )
    lines.append("")

    # --- テーマ/スコア帯/経路 別の分析 ---
    lines.append("■ 何が伸びたか（過去30日）")
    lines += _group_summary(metrics, "market_scope", "テーマ(market_scope)")
    lines.append("")
    lines += _group_summary(metrics, "post_value", "post_valueスコア")
    lines.append("")
    lines += _group_summary(metrics, "pass_path", "通過経路")
    lines.append("")
    lines += _group_summary(metrics, "mode", "投稿モード")
    lines.append("")

    # --- 時間帯別 ---
    by_hour = defaultdict(list)
    for m in metrics:
        try:
            h = datetime.fromisoformat(m["posted_at"]).hour
        except (KeyError, ValueError):
            continue
        by_hour[h].append(m)
    if by_hour:
        lines.append("--- 時間帯別(JST) ---")
        rows = []
        for h, items in by_hour.items():
            imps = [m["impressions"] for m in items if m.get("impressions") is not None]
            rows.append((h, len(items), statistics.mean(imps) if imps else None,
                         statistics.mean([_engagement(m) for m in items])))
        rows.sort(key=lambda r: (r[2] if r[2] is not None else -1, r[3]), reverse=True)
        for h, n, avg_imp, avg_eng in rows[:8]:
            ai = f"{avg_imp:,.0f}" if avg_imp is not None else "-"
            lines.append(f"  {h:02d}時台  投稿{n:3d}件  平均imp={ai:>9s}  平均エンゲージ={avg_eng:.1f}")
    lines.append("")

    # --- 示唆 ---
    lines.append("■ 示唆")
    scope_rows = defaultdict(list)
    for m in metrics:
        k = m.get("market_scope") or "(不明)"
        v = m.get("impressions")
        if v is not None:
            scope_rows[k].append(v)
    if scope_rows:
        best = max(scope_rows.items(), key=lambda kv: statistics.mean(kv[1]))
        worst = min(scope_rows.items(), key=lambda kv: statistics.mean(kv[1]))
        lines.append(f"  伸びるテーマ : {best[0]}（平均imp {statistics.mean(best[1]):,.0f}）")
        lines.append(f"  伸びないテーマ: {worst[0]}（平均imp {statistics.mean(worst[1]):,.0f}）")
        lines.append("  → 伸びないテーマは .env の閾値を上げて絞ると効率が上がります。")
    else:
        lines.append("  インプレッション未取得のため、傾向分析はエンゲージメント基準です。")
    lines.append("")

    # --- 直近24時間 TOP3 を日次学習データへ ---
    try:
        from performance_learning import update_daily_learning
    except ImportError:
        from common.performance_learning import update_daily_learning

    try:
        top_n = int(__import__("os").environ.get("PERFORMANCE_LEARNING_TOP_N", "3") or 3)
        lookback = int(__import__("os").environ.get(
            "PERFORMANCE_LEARNING_LOOKBACK_HOURS", "24") or 24)
        learning = update_daily_learning(
            metrics, lookback_hours=lookback, top_n=top_n)
        lines.append("■ 日次学習")
        lines.append(f"  status={learning.get('status', '-')}")
        lines.append(f"  {learning.get('message') or learning.get('reason') or '-'}")
        for post in learning.get("top_posts", [])[:top_n]:
            lines.append(
                f"  {post.get('rank', '-')}. imp={_fmt(post.get('impressions'))} "
                f"imp/h={post.get('impressions_per_hour', '-')} "
                f"bot={post.get('bot', '-')} {str(post.get('title') or post.get('text') or '')[:50]}"
            )
    except Exception as e:
        logger.exception("日次学習の更新に失敗しました")
        lines.append("■ 日次学習")
        lines.append(f"  更新失敗（レポート処理は継続）: {type(e).__name__}: {e}")

    lines.append("=" * 60)

    report = "\n".join(lines)
    try:
        log_run({"bot": "report", "posts_analyzed": len(metrics), "recent": len(recent)})
    except Exception:
        pass
    return report
