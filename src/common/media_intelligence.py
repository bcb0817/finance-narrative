"""時間補正ランキング、Shorts草案、週次メディア企画の生成。"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

try:
    from runtime import JST, output_dir, state_dir
    from metrics_collector import load_snapshots
except ImportError:  # pragma: no cover
    from common.runtime import JST, output_dir, state_dir
    from common.metrics_collector import load_snapshots


def rank_posts(posts: list[dict], snapshots: list[dict], *, stage="24h", count=3) -> dict:
    hours = {"1h": 1, "6h": 6, "24h": 24}.get(stage, 24)
    rows = []
    for post in posts:
        tweet_id = str(post.get("tweet_id", ""))
        matches = [row for row in snapshots
                   if str(row.get("tweet_id", "")) == tweet_id and row.get("stage") == stage]
        snap = max(matches, key=lambda row: str(row.get("metrics_collected_at") or ""), default=None)
        if not snap or (hours == 24 and float(snap.get("post_age_hours") or 0) < 24):
            continue
        rows.append({**post, **snap})
    rows.sort(key=lambda r: (r.get("impressions") if r.get("impressions") is not None else -1,
                             r.get("impressions_per_hour") or -1), reverse=True)
    n = max(1, count)
    return {"top": rows[:n], "bottom": list(reversed(rows[-n:])),
            "eligible": len(rows), "all": rows}


def build_daily_summary(posts: list[dict], snapshots: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    daily_snapshots = []
    for snapshot in snapshots:
        try:
            collected = datetime.fromisoformat(str(snapshot.get("metrics_collected_at") or ""))
            if collected.tzinfo is None:
                collected = collected.replace(tzinfo=JST)
            if collected.astimezone(JST) >= now - timedelta(hours=24):
                daily_snapshots.append(snapshot)
        except (TypeError, ValueError):
            continue
    recent = []
    for post in posts:
        try:
            dt = datetime.fromisoformat(post.get("posted_at", ""))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=JST)
            if dt >= now - timedelta(hours=24): recent.append(post)
        except ValueError: pass
    top_count = max(1, int(os.getenv("DAILY_TOP_COUNT", "3")))
    bottom_count = max(1, int(os.getenv("DAILY_BOTTOM_COUNT", "3")))
    stage = {}
    for name in ("1h", "6h", "24h"):
        ranked = rank_posts(posts, daily_snapshots, stage=name, count=max(top_count, bottom_count))
        ranked["top"] = ranked["top"][:top_count]
        ranked["bottom"] = ranked["bottom"][:bottom_count]
        stage[name] = ranked
    full = stage["24h"]
    unique = {str(r.get("tweet_id")): r for r in full["all"]}
    imps = [r["impressions"] for r in unique.values() if r.get("impressions") is not None]
    return {"date": now.strftime("%Y-%m-%d"), "recent_count": len(recent), "stages": stage,
            "data_status": "ok" if full["eligible"] else "データ不足",
            "known_impressions": sum(imps) if imps else None}


def write_daily_report(summary: dict) -> Path:
    p = output_dir("reports") / f"daily_performance_{summary['date']}.md"
    s24 = summary["stages"]["24h"]
    lines = [f"# 日次パフォーマンス {summary['date']}", "", f"- 対象投稿数: {s24['eligible']}",
             f"- データ状態: {summary['data_status']}",
             f"- 総インプレッション: {summary['known_impressions'] if summary['known_impressions'] is not None else 'データ不足'}",
             "", "## 24h 上位3投稿"]
    for i, row in enumerate(s24["top"], 1): lines.append(f"{i}. {row.get('text') or row.get('title')} — {row.get('impressions')} imp")
    lines += ["", "## 24h 下位3投稿"]
    for i, row in enumerate(s24["bottom"], 1): lines.append(f"{i}. {row.get('text') or row.get('title')} — {row.get('impressions')} imp")
    lines += ["", "## 翌日の実験仮説", "- データ不足時は仮説を断定しない。", "- hook_type別の長期比較を継続する。", "- 図解は構造価値がある題材だけで使う。", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def generate_shorts_drafts(top_posts: list[dict], run_date: str, generator=None) -> list[Path]:
    if os.getenv("YOUTUBE_SHORTS_DRAFT_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return []
    if os.getenv("YOUTUBE_AUTO_UPLOAD_ENABLED", "false").lower() in ("1", "true", "yes"):
        raise RuntimeError("YouTube自動公開は未実装です")
    out = output_dir("youtube_shorts") / run_date
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for rank, post in enumerate(top_posts[:3], 1):
        text = str(post.get("text") or post.get("title") or "")
        payload = {"source_tweet_id": str(post.get("tweet_id") or ""), "source_text": text,
            "selection_reason": "24時間実績上位", "hook_0_2s": "このニュース、重要なのは見出しの先です。",
            "fact_2_10s": text[:120], "market_impact_10_25s": "市場への影響を背景と比較から整理します。",
            "next_25_35s": "次に確認する数字と企業反応に注目です。",
            "narration_20_40s": f"{text[:120]}。重要なのは市場への波及です。次に関連銘柄と主要指標を確認します。",
            "captions": ["なぜ重要？", "市場への影響", "次に見る数字"],
            "scenes": [{"seconds": "0-2", "visual": "見出し"}, {"seconds": "2-25", "visual": "3要素図解"}, {"seconds": "25-35", "visual": "注目指標"}],
            "diagram_idea": "事実・影響対象・次に見る数字の3要素", "youtube_titles": [f"{text[:35]}を解説", "市場はここを見る", "投資家が次に見る数字"],
            "description": "金融ニュースの背景と市場への影響を短く解説します。", "hashtags": ["#米国株", "#金融ニュース"],
            "thumbnail_texts": ["なぜ重要？", "次に見る数字", "市場の本音"], "long_video_candidate": True, "article_candidate": True}
        if generator: payload = generator(post, payload)
        jp = out / f"rank{rank}.json"; mp = out / f"rank{rank}.md"
        jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mp.write_text(f"# Shorts案 Rank {rank}\n\n## ナレーション\n{payload['narration_20_40s']}\n", encoding="utf-8")
        paths += [jp, mp]
    return paths


def generate_short_payload_ai(post: dict, fallback: dict) -> dict:
    """元投稿の事実だけを使い、Shorts用JSONをAI生成。失敗時は安全な草案へ戻す。"""
    try:
        try:
            from openai_config import OpenAIRole
            from openai_service import OpenAIService
        except ImportError:
            from common.openai_config import OpenAIRole
            from common.openai_service import OpenAIService
        prompt = f"""次の金融X投稿から20〜40秒のYouTube Shorts企画を作ってください。
事実を追加・捏造せず、背景説明、市場への影響、銘柄間比較、次に見る数字のうち
元投稿から合理的に扱えるものを最低1つ含めてください。投資助言は禁止です。
元投稿: {post.get('text') or post.get('title') or ''}
次のキーをすべて持つJSONのみ返す: {json.dumps(fallback, ensure_ascii=False)}"""
        def schema_for(value):
            if isinstance(value, bool): return {"type":"boolean"}
            if isinstance(value, list): return {"type":"array","items":schema_for(value[0]) if value else {"type":"string"}}
            if isinstance(value, dict): return {"type":"object","properties":{k:schema_for(v) for k,v in value.items()},"required":list(value),"additionalProperties":False}
            return {"type":"string"}
        data = OpenAIService().structured(prompt, schema_for(fallback), role=OpenAIRole.GENERATE,
                                          operation="shorts_script")
        return {**fallback, **data}
    except Exception:
        pass
    return fallback


def write_weekly_media_plan(posts: list[dict], snapshots: list[dict], now: datetime | None = None) -> tuple[Path, Path] | None:
    if os.getenv("WEEKLY_MEDIA_PLAN_ENABLED", "true").lower() not in ("1", "true", "yes"): return None
    now = now or datetime.now(JST); date = now.strftime("%Y-%m-%d")
    cutoff = now - timedelta(days=7)
    weekly_posts = []
    for post in posts:
        try:
            posted = datetime.fromisoformat(post.get("posted_at", ""))
            if posted.tzinfo is None: posted = posted.replace(tzinfo=JST)
            if posted.astimezone(JST) >= cutoff: weekly_posts.append(post)
        except (TypeError, ValueError):
            continue
    ranked = rank_posts(weekly_posts, snapshots, stage="24h", count=20)["top"]
    themes = Counter(str(r.get("theme") or r.get("market_scope") or "other") for r in ranked)
    payload = {"date": date, "top_themes": themes.most_common(5),
      "major_narratives": ["AI・半導体・米国株の週間データから要確認"],
      "long_video_plans": [{"title": f"週間テーマ解説 {i}", "outline": ["背景", "市場への影響", "次週の注目点"]} for i in range(1,4)],
      "article_plans": ["週間AI株レビュー", "半導体サイクルの確認", "米国株の翌週注目材料"],
      "evergreen_article": "AI・半導体指標の読み方", "next_week_hypotheses": ["フック別比較を継続"],
      "reduce_topics": [r.get("theme") or "データ不足" for r in rank_posts(weekly_posts, snapshots, stage="24h", count=3)["bottom"]],
      "reproducibility": "複数週のデータが揃うまで暫定評価"}
    try:
        try: from experiments import variant_summary
        except ImportError: from common.experiments import variant_summary
        payload["experiments"] = variant_summary(weekly_posts, snapshots)
        payload["experiment_notes"] = {
            "winner": payload["experiments"][0]["variant"] if payload["experiments"] else None,
            "continue": [r["variant"] for r in payload["experiments"] if r["sample_size"] >= 5],
            "insufficient": [r["variant"] for r in payload["experiments"] if r["sample_size"] < 5],
        }
    except Exception:
        payload["experiments"] = []; payload["experiment_notes"] = {"winner":None,"continue":[],"insufficient":[]}
    if ranked:
        try:
            try:
                from openai_config import OpenAIRole
                from openai_service import OpenAIService
            except ImportError:
                from common.openai_config import OpenAIRole
                from common.openai_service import OpenAIService
            schema={"type":"object","additionalProperties":False,"properties":{
                "major_narratives":{"type":"array","items":{"type":"string"}},
                "article_plans":{"type":"array","items":{"type":"string"}},
                "evergreen_article":{"type":"string"},"next_week_hypotheses":{"type":"array","items":{"type":"string"}},
                "reduce_topics":{"type":"array","items":{"type":"string"}},"reproducibility":{"type":"string"}},
                "required":["major_narratives","article_plans","evergreen_article","next_week_hypotheses","reduce_topics","reproducibility"]}
            prompt="実測metricsを根拠に週次メディア企画をJSON化。データ不足は推測せず明記。\n"+json.dumps(ranked,ensure_ascii=False)
            payload.update(OpenAIService().structured(prompt,schema,role=OpenAIRole.ANALYZE,operation="weekly_media_plan"))
        except Exception:
            pass
    out = output_dir("media_plans"); jp = out / f"weekly_{date}.json"; mp = out / f"weekly_{date}.md"
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mp.write_text(f"# 週次メディア企画 {date}\n\n## 伸びたテーマTop5\n" + "\n".join(f"- {k}: {v}" for k,v in payload['top_themes']) + "\n", encoding="utf-8")
    return jp, mp
