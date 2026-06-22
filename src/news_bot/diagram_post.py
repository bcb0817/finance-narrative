"""
図解画像投稿のパイプライン。

image投稿では「毎回 type をランダム強制」してレイアウトのバリエーションを担保する：
1. IMAGE_TYPE_POOL から desired_type を random.choice で1つ選ぶ
2. その type に対応するJSON構造だけをLLMに生成させる（type固定・構造強制）
3. 返ってきたJSONを検証し、構造が合わなければ最大2回まで再生成（flowには逃がさない）
4. type が desired_type と違えば desired_type に上書き
5. JSON を整形して diagram_image.render_diagram で PNG を描画

main.py からは generate_diagram_image() を呼ぶだけ。
画像のアップロード・投稿は main.py 側（既存の tweepy）で行う。
"""

import json
import random
import logging

from diagram_image import render_diagram, TYPE_TO_RENDERER

logger = logging.getLogger(__name__)

HANDLE = "@singa9999"          # 自分のアカウント（固定）
IMAGE_PATH = "/tmp/diagram.png"
MAX_RETRIES = 2                # 構造不一致時の再生成回数（初回 + 2回 = 最大3回）

# image投稿で使う type プール（4レイアウトにバランスよく散らす）
IMAGE_TYPE_POOL = [
    "flow",
    "compare",
    "bull_bear",
    "before_after",
    "stat",
    "earnings",
    "macro_indicator",
    "market_snapshot",
    "timeline",
    "calendar",
    "event_sequence",
    "policy_path",
]

# 各 type で許す文字数の目安（描画崩れ防止）
_LIMITS = {
    "title": 46,
    "node_text": 70,
    "point": 28,
    "stat_label": 14,
    "event_text": 60,
}


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _family(t: str) -> str:
    """表向きの type を内部レイアウトの系（flow/compare/stat/timeline）に変換"""
    return TYPE_TO_RENDERER.get(t, "flow")


# ===== 系ごとの必須JSON構造（プロンプト用） =====
def _structure_spec(desired_type: str, renderer_key: str) -> str:
    if renderer_key == "flow":
        return f"""{{
  "type": "{desired_type}",
  "tag": "8文字以内の短いラベル（例: 市場メモ）",
  "title": "25〜45文字の日本語タイトル",
  "nodes": [
    {{"label": "材料/市場の見方/注目点 など", "text": "60字以内"}},
    {{"label": "...", "text": "60字以内"}},
    {{"label": "...", "text": "60字以内"}}
  ],
  "hashtags": ["#xxx", "#yyy"],
  "handle": "{HANDLE}"
}}
※ nodes は必ず3〜4個。各 node に label と text を必ず入れる。"""

    if renderer_key == "compare":
        return f"""{{
  "type": "{desired_type}",
  "tag": "8文字以内の短いラベル（例: 強気と弱気）",
  "title": "25〜45文字の日本語タイトル",
  "left":  {{"title": "左の見出し", "points": ["25字以内", "25字以内", "25字以内"]}},
  "right": {{"title": "右の見出し", "points": ["25字以内", "25字以内", "25字以内"]}},
  "hashtags": ["#xxx", "#yyy"],
  "handle": "{HANDLE}"
}}
※ left/right それぞれに title と points（各3個）を必ず入れる。対比になる見出しにする。"""

    if renderer_key == "stat":
        return f"""{{
  "type": "{desired_type}",
  "tag": "8文字以内の短いラベル（例: 決算速報）",
  "title": "25〜45文字の日本語タイトル",
  "stats": [
    {{"value": "+62", "unit": "%", "label": "項目名(14字以内)", "dir": "up"}},
    {{"value": "351", "unit": "億$", "label": "項目名(14字以内)", "dir": "up"}}
  ],
  "context": "80字以内の補足",
  "hashtags": ["#xxx", "#yyy"],
  "handle": "{HANDLE}"
}}
※ stats は必ず2〜3個。各要素に value, unit, label, dir(up/down/flat) を入れる。
※ ニュースに具体的な数字が無い場合は value を "—"、dir を "flat" にする（数字を捏造しない）。"""

    # timeline
    return f"""{{
  "type": "{desired_type}",
  "tag": "8文字以内の短いラベル（例: 今週の予定）",
  "title": "25〜45文字の日本語タイトル",
  "events": [
    {{"when": "火曜/11月/発表後 など", "text": "50字以内"}},
    {{"when": "...", "text": "50字以内"}},
    {{"when": "...", "text": "50字以内"}}
  ],
  "hashtags": ["#xxx", "#yyy"],
  "handle": "{HANDLE}"
}}
※ events は必ず3〜5個。各要素に when と text を必ず入れる。時系列・順序になるようにする。"""


def build_diagram_prompt(item, desired_type: str) -> str:
    renderer_key = _family(desired_type)
    spec = _structure_spec(desired_type, renderer_key)
    return f"""あなたは金融SNS向けの図解デザイナー兼アナリストです。
以下の金融ニュースを、指定された type の図解JSONに変換してください。

ニュース：
{item.title}

ソース：
{item.source}

【今回の type（厳守）】: "{desired_type}"
- type は必ず "{desired_type}" にすること。他の type を選んではいけない。
- 下記の構造を必ず守ること（キー名・個数を守る）。

【返すJSONの構造】
{spec}

【厳守ルール】
- 上記JSONだけを返す。Markdown（```）禁止、説明文・前置き・後置き一切禁止。JSONのみ。
- ニュースに無い数字や事実は作らない。
"""


# ===== 構造検証（rendererに合うか） =====
def _valid_for_renderer(data: dict, renderer_key: str) -> bool:
    if not isinstance(data, dict):
        return False
    if not data.get("title"):
        return False

    if renderer_key == "flow":
        nodes = data.get("nodes")
        return (isinstance(nodes, list) and len(nodes) >= 2
                and all(isinstance(n, dict) and n.get("text") for n in nodes))

    if renderer_key == "compare":
        l, r = data.get("left"), data.get("right")
        return (isinstance(l, dict) and isinstance(r, dict)
                and isinstance(l.get("points"), list) and len(l["points"]) >= 2
                and isinstance(r.get("points"), list) and len(r["points"]) >= 2)

    if renderer_key == "stat":
        stats = data.get("stats")
        return (isinstance(stats, list) and len(stats) >= 1
                and all(isinstance(s, dict) and s.get("label") is not None
                        and s.get("value") is not None for s in stats))

    if renderer_key == "timeline":
        events = data.get("events")
        return (isinstance(events, list) and len(events) >= 2
                and all(isinstance(e, dict) and e.get("text") for e in events))

    return False


def _normalize(data: dict) -> dict:
    """LLM出力を描画用に整形・クリップ。type は系に変換して整形する。"""
    t = data.get("type", "flow")
    fam = _family(t)
    data["tag"] = _clip(data.get("tag", "市場メモ"), 8)
    data["title"] = _clip(data.get("title", ""), _LIMITS["title"])
    data["handle"] = HANDLE
    hashtags = data.get("hashtags") or []
    data["hashtags"] = [h if h.startswith("#") else f"#{h}" for h in hashtags][:2]

    if fam == "flow":
        nodes = data.get("nodes") or []
        data["nodes"] = [{"label": _clip(n.get("label", ""), 10),
                          "text": _clip(n.get("text", ""), _LIMITS["node_text"])}
                         for n in nodes[:4]] or [{"label": "要点", "text": data["title"]}]
    elif fam == "compare":
        for side in ("left", "right"):
            c = data.get(side) or {}
            c["title"] = _clip(c.get("title", ""), 14)
            c["points"] = [_clip(p, _LIMITS["point"]) for p in (c.get("points") or [])][:3]
            data[side] = c
    elif fam == "stat":
        out = []
        for s in (data.get("stats") or [])[:3]:
            out.append({"value": _clip(str(s.get("value", "")), 8),
                        "unit": _clip(str(s.get("unit", "")), 4),
                        "label": _clip(s.get("label", ""), _LIMITS["stat_label"]),
                        "dir": s.get("dir", "flat")})
        data["stats"] = out
        data["context"] = _clip(data.get("context", ""), 90)
    elif fam == "timeline":
        evs = data.get("events") or []
        data["events"] = [{"when": _clip(e.get("when", ""), 8),
                           "text": _clip(e.get("text", ""), _LIMITS["event_text"])}
                          for e in evs[:5]]
    return data


def _review_text(data: dict) -> str:
    """コンプラ審査に回す全テキスト（系ごとに本文を集約）"""
    parts = [data.get("title", "")]
    fam = _family(data.get("type", "flow"))
    if fam == "flow":
        parts += [n["text"] for n in data.get("nodes", [])]
    elif fam == "compare":
        parts += data.get("left", {}).get("points", []) + data.get("right", {}).get("points", [])
    elif fam == "stat":
        parts += [f'{s["value"]}{s["unit"]} {s["label"]}' for s in data.get("stats", [])]
        parts.append(data.get("context", ""))
    elif fam == "timeline":
        parts += [e["text"] for e in data.get("events", [])]
    return " / ".join(p for p in parts if p)


def _caption(data: dict) -> str:
    cap = data["title"]
    tags = " ".join(data.get("hashtags", []))
    if tags:
        cap = f"{cap}\n\n{tags}"
    return cap[:280]


def build_image_from_data(data: dict):
    """JSON(dict) → (画像パス, キャプション, 審査用テキスト, type)。テスト用にネット不要で呼べる。"""
    data = _normalize(data)
    render_diagram(data, IMAGE_PATH)
    return IMAGE_PATH, _caption(data), _review_text(data), data["type"]


def generate_diagram_image(item, openai_client, model: str):
    """
    image投稿用：type をランダム強制し、構造が合うJSONが得られるまで最大3回試行。
    成功時は (image_path, caption, review_text, type) を返す。
    全試行失敗時は None を返す（呼び出し側でスキップ）。
    """
    desired_type = random.choice(IMAGE_TYPE_POOL)
    renderer_key = _family(desired_type)
    last_reason = ""

    for attempt in range(1, MAX_RETRIES + 2):   # 初回 + MAX_RETRIES
        prompt = build_diagram_prompt(item, desired_type)
        try:
            resp = openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=2000,
                reasoning_effort="minimal",
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last_reason = f"JSONパース失敗: {e}"
            logger.warning(f"[図解 試行{attempt}] {last_reason} → 再生成")
            continue
        except Exception as e:
            last_reason = f"API呼び出し失敗: {e}"
            logger.warning(f"[図解 試行{attempt}] {last_reason} → 再生成")
            continue

        returned_type = data.get("type")
        if returned_type != desired_type:
            logger.info(f"[図解] returned_type={returned_type} を desired_type={desired_type} に上書き")
            data["type"] = desired_type            # 要件6: desired_type に上書き

        if not _valid_for_renderer(data, renderer_key):
            last_reason = f"{renderer_key} の構造要件を満たさない"
            logger.warning(f"[図解 試行{attempt}] {last_reason} → 再生成")
            continue

        image_path, caption, review_text, dtype = build_image_from_data(data)
        logger.info(
            "[図解 生成OK] desired_type=%s / returned_type=%s / renderer_key=%s / "
            "caption=%r / image_path=%s",
            desired_type, returned_type, renderer_key, caption, image_path,
        )
        return image_path, caption, review_text, dtype

    logger.error(
        f"[図解 生成失敗] desired_type={desired_type} を {MAX_RETRIES + 1}回試行するも構造を満たせず。"
        f"最後の理由: {last_reason}。今回の投稿はスキップします。"
    )
    return None
