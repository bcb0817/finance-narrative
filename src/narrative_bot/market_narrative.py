"""
market_narrative.py
機関投資家向けストラテジスト兼・金融メディア編集長レイヤー。

4ソース（米国株ニュース / 経済指標 / 決算 / Reddit議論）を集約し、
「投資家が今何を気にしているか」を抽出する。市場全体・主要セクターに
影響する材料だけを採用し、ローカル/話題性のみ/日本ローカルは無視する。

AIには取得済みシグナルだけを渡し、存在しないニュース・銘柄・数値を作らせない。
複数のナラティブ候補を抽出し、最も投稿価値・市場インパクトが大きい1つ
（top_narrative）だけを選んで画像化・投稿する。3カード表示・最重要テーマ欄は廃止。

出力JSON: candidates[] （各候補は以下を持つ）
  title, stance(強気/弱気/中立), impact(1-10), post_value(1-10),
  what(80-120字), why(80-120字), market_effect(60-100字),
  watch_points[](3点以内), tickers[], source_titles[]
"""

import json
import logging

logger = logging.getLogger(__name__)

POST_VALUE_THRESHOLD = 8
X_POST_MAX = 280  # 文字数ガード（重み付き上限は build_caption 側で270に制御）


def _gather_news(limit: int = 15) -> list[dict]:
    """既存 news.py の仕組みで直近ニュースを集約（1件選定ではなく一覧）。"""
    try:
        from news import RSS_FEEDS, fetch_feed, deduplicate, is_recent, score_item
    except Exception as e:
        logger.warning(f"news読み込み失敗: {e}")
        return []
    items = []
    for name, cfg in RSS_FEEDS.items():
        try:
            items.extend(fetch_feed(name, cfg))
        except Exception as e:
            logger.warning(f"news取得失敗 {name}: {e}")
    items = [i for i in items if is_recent(i, hours=36)]
    items.sort(key=score_item, reverse=True)
    items = deduplicate(items)[:limit]
    return [{"title": i.title, "source": i.source, "group": i.source_group} for i in items]


def _gather_events(limit: int = 12) -> list[dict]:
    """今週の経済指標・決算（weekly_events）。"""
    try:
        from weekly_events import fetch_weekly_events
        from weekly_normalizer import normalize_events
        raw = fetch_weekly_events()
        evs = normalize_events(raw)
        out = []
        for e in evs[:limit]:
            out.append({
                "date_jst": e["display_date_jst"], "time_jst": e["time_jst"],
                "category": e["category"], "title": e["title"],
            })
        return out
    except Exception as e:
        logger.warning(f"events取得失敗: {e}")
        return []


def _gather_reddit(limit: int = 15) -> list[dict]:
    try:
        from reddit_signals import fetch_reddit_signals
        posts = fetch_reddit_signals(limit_total=limit)
        return [{"subreddit": p["subreddit"], "title": p["title"],
                 "score": p["score"], "comments": p["comments"]} for p in posts]
    except Exception as e:
        logger.warning(f"reddit取得失敗: {e}")
        return []


def gather_signals() -> dict:
    """4ソースを集約して返す。"""
    signals = {
        "news": _gather_news(),
        "events": _gather_events(),
        "reddit": _gather_reddit(),
    }
    logger.info(
        "signals: news=%d / events=%d / reddit=%d",
        len(signals["news"]), len(signals["events"]), len(signals["reddit"]),
    )
    return signals


def _build_prompt(signals: dict) -> str:
    news = "\n".join(f'- [{n["group"]}] {n["title"]}（{n["source"]}）' for n in signals["news"]) or "（なし）"
    events = "\n".join(f'- {e["date_jst"]} {e["time_jst"]} [{e["category"]}] {e["title"]}' for e in signals["events"]) or "（なし）"
    reddit = "\n".join(f'- {r["subreddit"]} ↑{r["score"]} 💬{r["comments"]}: {r["title"]}' for r in signals["reddit"]) or "（なし）"

    return f"""あなたは機関投資家向けのストラテジスト兼・金融メディア編集長です。
目的はニュースの要約ではなく、「投資家が今何を気にしているか」を抽出し、
米国株式市場に大きな影響を与える材料だけを選別することです。

【採用ルール】市場全体または主要セクターに影響する内容のみ。以下は無視：
地方経済指標 / 地域ニュース / 影響の小さい企業IR / 話題性だけのニュース / 日本ローカル情報。

【取得済みシグナル（この中だけを根拠にする。存在しないニュース・銘柄・数値を作らない）】
■ニュース:
{news}

■経済指標・決算（JST）:
{events}

■Reddit議論（話題性）:
{reddit}

市場ナラティブの「候補」を複数（2〜5件）抽出してください。最終的に画像化するのは
最も価値の高い1件だけですが、ここでは候補を出し切ってください。
以下のJSONのみを返す（説明文・Markdown禁止）。日本語で記述。
{{
  "candidates": [
    {{
      "title": "テーマ名（簡潔に）",
      "stance": "強気" or "弱気" or "中立",
      "impact": 1〜10の整数,
      "post_value": 1〜10の整数,
      "conclusion": "結論を一言で（40字以内・結論ファースト・断定しすぎない）",
      "what": "何が起きているか（80〜120字、取得シグナルに基づく）",
      "why": "なぜ重要か（80〜120字）",
      "market_effect": "市場への影響（60〜100字、どのセクター/資産にどう効くか）",
      "watch_points": ["見るべきポイント（3点以内、各短く）"],
      "tickers": ["影響銘柄のティッカー（シグナルから読み取れる範囲。無ければ空配列）"],
      "source_titles": ["根拠にした取得シグナルのタイトル（実在するものだけ）"]
    }}
  ]
}}

【post_value 基準（厳格に適用）】
- 10: 米国株市場全体を動かす最重要テーマ
- 9: NASDAQ/S&P500、金利、ドル、半導体、大型テックに強く影響
- 8: 主要セクターや大型株に明確な影響がある
- 7: 重要ではあるが、投稿するほどではない
- 6以下: ノイズ、局所的、材料不足
投稿対象は post_value が 8 以上のみ。7以下は投稿しない前提で、誠実に採点すること。

【post_value を 8 未満（投稿しない側）に下げるべきケース】
次のいずれかに当てはまるなら、たとえ話題性があっても 7 以下にする：
- 根拠のない因果の断定がある（取得シグナルで裏づけられない）
- 取得シグナルに無い市場解説を作っている（出所のない解釈の追加）
- 投資助言・推奨に見える表現がある
- 市場への影響が弱い、または局所的（個別株・地方・ニッチ）
- 出所不明の材料を中心に組み立てている
各項目の文字数目安（超過しない）：what 80〜120字 / why 80〜120字 /
market_effect 60〜100字 / watch_points は3点以内。
投稿数より質を優先し、迷ったら低めに採点すること。"""


# 米国株指数・金利・ドル・指標・半導体・大型テック・原油・地政学に関わるキーワード
_PRIORITY_KEYWORDS = [
    "s&p", "sp500", "s&p500", "nasdaq", "ナスダック", "ダウ", "指数",
    "金利", "利上げ", "利下げ", "fed", "frb", "fomc", "国債", "利回り", "yield",
    "ドル", "為替", "dxy",
    "pce", "cpi", "ppi", "雇用", "payroll", "jobs", "gdp", "ism", "pmi",
    "半導体", "semiconductor", "chip", "ai", "人工知能",
    "nvda", "nvidia", "micron", "mu", "amd", "avgo", "tsm", "asml",
    "apple", "microsoft", "google", "amazon", "meta", "tesla", "大型テック", "メガキャップ",
    "原油", "oil", "crude", "opec", "エネルギー", "energy",
    "地政学", "geopolitic", "中東", "台湾", "関税", "tariff", "制裁",
]


def _affects_core_market(c: dict) -> bool:
    text = " ".join([
        c.get("title", ""), c.get("what", ""), c.get("why", ""),
        c.get("market_effect", ""), " ".join(c.get("tickers", []) or []),
    ]).lower()
    return any(k in text for k in _PRIORITY_KEYWORDS)


def select_top_narrative(candidates: list[dict]) -> tuple[dict | None, list[dict]]:
    """
    候補から優先順位で top_narrative を1つ選ぶ。
    優先順位:
      1. post_value 最大
      2. impact 最大
      3. 米国株指数/金利/ドル/半導体/大型テックに影響しやすい
      4. source_titles が複数
      5. 根拠が明確（source_titles と market_effect が埋まっている）
    戻り値: (top_narrative or None, rejected[{title, reason}])
    """
    if not candidates:
        return None, []

    def sort_key(c: dict):
        return (
            int(c.get("post_value", 0)),                 # 1
            int(c.get("impact", 0)),                      # 2
            1 if _affects_core_market(c) else 0,          # 3
            len(c.get("source_titles", []) or []),        # 4
            1 if (c.get("source_titles") and c.get("market_effect")) else 0,  # 5
        )

    ranked = sorted(candidates, key=sort_key, reverse=True)
    top = ranked[0]
    rejected = []
    for c in ranked[1:]:
        reasons = []
        if int(c.get("post_value", 0)) < int(top.get("post_value", 0)):
            reasons.append(f"post_value低い({c.get('post_value')}<{top.get('post_value')})")
        elif int(c.get("impact", 0)) < int(top.get("impact", 0)):
            reasons.append(f"impact低い({c.get('impact')}<{top.get('impact')})")
        elif _affects_core_market(top) and not _affects_core_market(c):
            reasons.append("主要市場(指数/金利/半導体等)への影響がtopより弱い")
        elif len(c.get("source_titles", []) or []) < len(top.get("source_titles", []) or []):
            reasons.append("根拠ソースがtopより少ない")
        else:
            reasons.append("総合優先度でtopに劣る")
        rejected.append({"title": c.get("title", ""), "reason": " / ".join(reasons)})
    return top, rejected


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _normalize_candidate(c: dict) -> dict:
    c.setdefault("title", "")
    c["stance"] = c.get("stance", "中立")
    try:
        c["impact"] = int(c.get("impact", 0))
    except Exception:
        c["impact"] = 0
    try:
        c["post_value"] = int(c.get("post_value", 0))
    except Exception:
        c["post_value"] = 0
    c["what"] = _clip(c.get("what", ""), 120)
    c["why"] = _clip(c.get("why", ""), 120)
    c["market_effect"] = _clip(c.get("market_effect", ""), 100)
    c["conclusion"] = _clip(c.get("conclusion", "") or c.get("title", ""), 60)
    wp = c.get("watch_points", []) or []
    c["watch_points"] = [_clip(w, 40) for w in wp[:3]]
    c["tickers"] = (c.get("tickers", []) or [])[:6]
    c["source_titles"] = c.get("source_titles", []) or []
    return c


def build_caption(top: dict) -> str:
    """top_narrative から X投稿本文を組み立てる（結論ファースト・URLなし・ソース名のみ）。
    形式:
        結論：…
        何が起きた：…
        なぜ重要：…
        見るべき点：…
    X重み付き文字数(<=270)に収まるよう各セクションを切り詰める。
    """
    try:
        from safety import weighted_len
    except Exception:
        def weighted_len(s):  # フォールバック（CJK=2近似）
            return sum(1 if ord(c) < 0x1100 else 2 for c in s or "")

    conclusion = (top.get("conclusion") or top.get("title") or "").strip()
    what = (top.get("what") or "").strip()
    why = (top.get("why") or "").strip()
    wps = top.get("watch_points") or []
    watch = (wps[0] if wps else (top.get("market_effect") or "")).strip()

    def line(label, text, budget):
        text = _clip(text, budget)
        return f"{label}{text}" if text else ""

    # 初期予算（必要なら全体で詰める）
    parts = [
        line("結論：", conclusion, 50),
        line("何が起きた：", what, 60),
        line("なぜ重要：", why, 60),
        line("見るべき点：", watch, 50),
    ]
    caption = "\n".join(p for p in parts if p)

    # X重み付き270を超えるなら、後ろのセクションから削って収める
    cap_budget = 270
    while weighted_len(caption) > cap_budget and len(parts) > 1:
        # 末尾の非空セクションを1つ落とす
        for i in range(len(parts) - 1, 0, -1):
            if parts[i]:
                parts[i] = ""
                break
        caption = "\n".join(p for p in parts if p)
    # それでも長い場合は結論だけにして丸める
    if weighted_len(caption) > cap_budget:
        caption = _clip(f"結論：{conclusion}", 120)
    return caption


def analyze_market(signals: dict | None = None) -> dict:
    """編集長AIで候補を生成し、正規化して返す。{'candidates':[...]}"""
    from post import get_openai_client, OPENAI_GENERATE_MODEL

    if signals is None:
        signals = gather_signals()

    prompt = _build_prompt(signals)
    try:
        from performance_learning import with_performance_learning
    except ImportError:
        from common.performance_learning import with_performance_learning
    prompt = with_performance_learning(prompt)
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=3500,
        response_format={"type": "json_object"},
        reasoning_effort="minimal",
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    cands = data.get("candidates", []) or []
    cands = [_normalize_candidate(c) for c in cands if isinstance(c, dict)]
    return {"candidates": cands}
