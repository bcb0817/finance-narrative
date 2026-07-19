"""
common/performance_learning.py
過去24時間のインプレッション上位投稿を日次レビューし、
再利用可能な「投稿設計ルール」をローカル学習データとして保存する。

これはモデルのファインチューニングではない。
knowledge/viral_patterns/latest_patterns.md を次回以降の生成プロンプトに
参考情報として差し込み、勝ちパターンを即日反映する方式。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

try:
    from runtime import REPO_ROOT, JST, log_run
except ImportError:  # pragma: no cover
    from common.runtime import REPO_ROOT, JST, log_run

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get(
        "PERFORMANCE_LEARNING_ENABLED", "true"
    ).strip().lower() in ("true", "1", "yes")


def _root() -> Path:
    path = REPO_ROOT / "knowledge" / "viral_patterns"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reviews_dir() -> Path:
    path = _root() / "reviews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _daily_jsonl() -> Path:
    return _root() / "daily_top3.jsonl"


def _latest_md() -> Path:
    return _root() / "latest_patterns.md"


def _engagement(m: dict) -> int:
    return sum(
        int(m.get(k, 0) or 0)
        for k in ("likes", "retweets", "replies", "quotes", "bookmarks")
    )


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt.astimezone(JST)
    except (TypeError, ValueError):
        return None


def _clip(text: str, limit: int) -> str:
    clean = (text or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _load_previous_patterns() -> str:
    path = _latest_md()
    if not path.exists():
        return "（まだ過去の学習ルールはありません）"
    try:
        return _clip(path.read_text(encoding="utf-8"), 5000)
    except OSError:
        return "（過去の学習ルールを読み込めませんでした）"


def _json_from_model(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "").strip()
    data = json.loads(text or "{}")
    return data if isinstance(data, dict) else {}


def _replace_daily_jsonl(record: dict) -> None:
    """同じ日付は上書きし、手動再実行で重複行を作らない。"""
    path = _daily_jsonl()
    rows: list[dict] = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict) and row.get("date") != record.get("date"):
                    rows.append(row)
        except (json.JSONDecodeError, OSError):
            rows = []
    rows.append(record)
    rows = rows[-90:]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _render_latest_markdown(review: dict, run_date: str) -> str:
    rolling = review.get("rolling_rules") or review.get("reusable_rules") or []
    avoid = review.get("avoid_patterns") or []
    summary = str(review.get("daily_summary") or "").strip()

    lines = [
        "# 投稿実績から学んだ最新ルール",
        "",
        f"更新日: {run_date} JST",
        "",
        "## 重要ルール",
    ]
    if rolling:
        for item in rolling[:10]:
            if isinstance(item, dict):
                rule = str(item.get("rule") or item.get("pattern") or "").strip()
                evidence = str(item.get("evidence") or "").strip()
            else:
                rule, evidence = str(item).strip(), ""
            if rule:
                lines.append(f"- {rule}" + (f"（根拠: {evidence}）" if evidence else ""))
    else:
        lines.append("- まだ十分な学習データがありません。")

    lines += ["", "## 避けるパターン"]
    if avoid:
        for item in avoid[:6]:
            if isinstance(item, dict):
                rule = str(item.get("rule") or item.get("pattern") or "").strip()
            else:
                rule = str(item).strip()
            if rule:
                lines.append(f"- {rule}")
    else:
        lines.append("- 現時点では追加ルールなし。")

    if summary:
        lines += ["", "## 直近24時間の要約", summary]

    lines += [
        "",
        "## 運用上の優先順位",
        "- このメモは表現・構成・テーマ選定の参考にだけ使う。",
        "- 元ニュースの事実、安全審査、投稿価値ゲート、重複回避を常に優先する。",
        "- 上位投稿の文章や断定表現をそのままコピーしない。",
        "",
    ]
    return "\n".join(lines)


def update_daily_learning(
    metrics: list[dict],
    *,
    lookback_hours: int = 24,
    top_n: int = 3,
) -> dict:
    """直近lookback_hoursのインプレ上位top_nをAIレビューして保存する。"""
    if not _enabled():
        return {"status": "disabled", "message": "日次学習は無効です"}

    now = datetime.now(JST)
    cutoff = now - timedelta(hours=max(1, int(lookback_hours)))
    candidates: list[dict] = []

    for metric in metrics:
        posted = _parse_dt(metric.get("posted_at", ""))
        impressions = metric.get("impressions")
        if posted is None or posted < cutoff or impressions is None:
            continue
        try:
            impressions = int(impressions)
        except (TypeError, ValueError):
            continue

        age_hours = max((now - posted).total_seconds() / 3600.0, 0.1)
        row = dict(metric)
        row["impressions"] = impressions
        row["age_hours"] = round(age_hours, 2)
        row["impressions_per_hour"] = round(impressions / age_hours, 2)
        row["engagement_total"] = _engagement(metric)
        candidates.append(row)

    candidates.sort(
        key=lambda m: (m.get("impressions", -1), m.get("engagement_total", 0)),
        reverse=True,
    )
    top = candidates[: max(1, int(top_n))]
    run_date = now.strftime("%Y-%m-%d")

    if not top:
        result = {
            "status": "skipped",
            "date": run_date,
            "reason": "直近24時間の投稿でインプレッションを取得できませんでした",
            "top_posts": [],
        }
        (_reviews_dir() / f"{run_date}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            log_run({"bot": "learning", "status": "skipped", "reason": result["reason"]})
        except Exception:
            pass
        return result

    raw_top = []
    prompt_rows = []
    for rank, m in enumerate(top, 1):
        text = _clip(m.get("text") or m.get("title") or "", 500)
        item = {
            "rank": rank,
            "tweet_id": str(m.get("tweet_id") or ""),
            "bot": m.get("bot") or "unknown",
            "mode": m.get("mode") or "",
            "title": _clip(m.get("title") or "", 180),
            "text": text,
            "posted_at": m.get("posted_at") or "",
            "impressions": m.get("impressions"),
            "impressions_per_hour": m.get("impressions_per_hour"),
            "age_hours": m.get("age_hours"),
            "likes": m.get("likes", 0),
            "retweets": m.get("retweets", 0),
            "replies": m.get("replies", 0),
            "quotes": m.get("quotes", 0),
            "bookmarks": m.get("bookmarks", 0),
            "market_scope": m.get("market_scope"),
            "post_value": m.get("post_value"),
        }
        raw_top.append(item)
        prompt_rows.append(json.dumps(item, ensure_ascii=False))

    previous = _load_previous_patterns()
    prompt = f"""あなたは金融Xアカウントのコンテンツ改善責任者です。
直近24時間の投稿をインプレッション順に並べた上位{len(raw_top)}件を分析し、
翌日以降に再利用できる投稿設計ルールを作ってください。

【重要な分析姿勢】
- インプレッション上位という相関だけから、因果を断定しない
- 投稿後経過時間と impressions_per_hour も考慮する
- ニュースの事実や投資判断を学習するのではなく、テーマ選定、冒頭のフック、
  情報構造、文字量、図解の見せ方など「表現設計」を学ぶ
- 上位投稿の文章をそのままコピーするルールは禁止
- 投資助言、売買推奨、誇張、未確認の数字を促すルールは禁止
- 安全審査、投稿価値ゲート、事実確認を弱めない

【これまでのローリング学習メモ】
{previous}

【直近24時間の上位投稿】
{chr(10).join(prompt_rows)}

次のJSONだけを返してください。Markdownや説明文は禁止です。
{{
  "daily_summary": "今日の勝ち筋を日本語2〜4文で",
  "top_posts": [
    {{
      "tweet_id": "対象ID",
      "winning_elements": ["効いた可能性がある要素"],
      "hook_pattern": "冒頭の型",
      "structure_pattern": "本文構造の型",
      "visual_or_format_signal": "図解・改行・文字量など",
      "caveat": "因果断定を避ける注意"
    }}
  ],
  "reusable_rules": [
    {{"rule": "明日から使える具体的ルール", "evidence": "どの投稿指標から推測したか"}}
  ],
  "rolling_rules": [
    {{"rule": "過去メモと今日の結果を統合した重要ルール", "evidence": "簡潔な根拠"}}
  ],
  "avoid_patterns": [
    {{"rule": "避けるべき表現・構成", "reason": "理由"}}
  ]
}}"""

    review: dict
    try:
        try:
            from openai_client import get_openai_client, OPENAI_GENERATE_MODEL
        except ImportError:
            from common.openai_client import get_openai_client, OPENAI_GENERATE_MODEL

        client = get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_GENERATE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=3000,
            response_format={"type": "json_object"},
            reasoning_effort="minimal",
        )
        review = _json_from_model(response.choices[0].message.content or "{}")
        status = "ok"
        error = ""
    except Exception as e:  # 学習失敗で日次レポート全体を止めない
        logger.exception("日次Top3レビューに失敗しました")
        review = {
            "daily_summary": "",
            "top_posts": [],
            "reusable_rules": [],
            "rolling_rules": [],
            "avoid_patterns": [],
        }
        status = "analysis_error"
        error = f"{type(e).__name__}: {e}"

    payload = {
        "status": status,
        "date": run_date,
        "generated_at": now.isoformat(),
        "lookback_hours": lookback_hours,
        "ranking": "impressions_desc",
        "top_posts": raw_top,
        "review": review,
        "error": error,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    (_reviews_dir() / f"{run_date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_daily_jsonl(payload)

    if status == "ok":
        _latest_md().write_text(
            _render_latest_markdown(review, run_date),
            encoding="utf-8",
        )

    try:
        log_run({
            "bot": "learning",
            "status": status,
            "top_count": len(raw_top),
            "top_tweet_ids": [p["tweet_id"] for p in raw_top],
        })
    except Exception:
        pass

    return {
        "status": status,
        "date": run_date,
        "top_count": len(raw_top),
        "top_posts": raw_top,
        "message": (
            f"インプレッション上位{len(raw_top)}件を学習データに保存しました"
            if status == "ok"
            else f"上位{len(raw_top)}件は保存しましたが、AIレビューに失敗しました: {error}"
        ),
    }


def load_learning_context(max_chars: int | None = None) -> str:
    """次回生成プロンプトへ差し込む学習メモ。未生成なら空文字。"""
    if not _enabled():
        return ""
    path = _latest_md()
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    try:
        limit = int(
            max_chars
            if max_chars is not None
            else os.environ.get("PERFORMANCE_LEARNING_MAX_CONTEXT_CHARS", "3500")
        )
    except (TypeError, ValueError):
        limit = 3500
    return _clip(text, max(500, limit))


def with_performance_learning(prompt: str) -> str:
    """生成タスクだけに学習メモを付与する。審査・ゲート判定には使わない。"""
    context = load_learning_context()
    if not context:
        return prompt
    return f"""【過去の投稿実績から得た表現設計メモ】
{context}

上記は表現・構成の参考情報です。
今回の元データ、事実確認、安全ルール、出力形式の指示を常に優先してください。
上位投稿の文章や未確認情報をコピーしないでください。

【今回の生成タスク】
{prompt}"""
