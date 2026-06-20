"""
図解画像投稿のパイプライン。

1. LLM にニュースを渡し、最適な type を選ばせて構造化JSONを返させる
2. JSON を検証・整形して diagram_image.render_diagram で PNG を描画
3. 投稿用キャプション と コンプラ審査用テキスト を組み立てて返す

main.py からは generate_diagram_image() を呼ぶだけ。
画像のアップロード・投稿は main.py 側（既存の tweepy）で行う。
"""

import json
import logging

from diagram_image import render_diagram, TYPE_TO_RENDERER, DIAGRAM_TYPES

logger = logging.getLogger(__name__)

HANDLE = "@singa9999"          # 自分のアカウント（固定）
IMAGE_PATH = "/tmp/diagram.png"

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


def _type_menu() -> str:
    """DIAGRAM_TYPES から系ごとの type 一覧をプロンプト用に生成（単一情報源）"""
    fam_label = {
        "flow":     "【flow系：因果・連鎖・整理 → nodes を使う】",
        "compare":  "【compare系：2つを対比 → left / right を使う】",
        "stat":     "【stat系：数字が主役 → stats / context を使う】",
        "timeline": "【timeline系：時系列・順序 → events を使う】",
    }
    lines = []
    for fam in ("flow", "compare", "stat", "timeline"):
        lines.append(fam_label[fam])
        for t, desc in DIAGRAM_TYPES.items():
            if TYPE_TO_RENDERER.get(t) == fam:
                lines.append(f'  - "{t}" … {desc}')
    return "\n".join(lines)


def build_diagram_prompt(item) -> str:
    return f"""あなたは金融SNS向けの図解デザイナー兼アナリストです。
以下の金融ニュースを、最も伝わりやすい「図解」の構造化データ(JSON)に変換してください。

ニュース：
{item.title}

ソース：
{item.source}

■ ステップ1：内容に最適な type を、次の一覧から1つだけ選ぶ。
{_type_menu()}

■ type選びの指針：
- 数字（金額・%・件数など）がニュースに具体的に含まれ、それが主役なら stat系
  （earnings=決算、macro_indicator=CPI/雇用統計など、market_snapshot=指数/為替/原油など）
- 日付・曜日・順序・複数イベントが主題なら timeline系
  （calendar=予定表、event_sequence=発表→反応→次、policy_path=中銀見通し）
- 2つの立場・対象を比べるなら compare系（bull_bear=強弱、sector_compare=セクター等）
- それ以外の因果・波及・整理は flow系（chain=連鎖、takeaway=要点、driver=ドライバー等）
- 数字が無いニュースで stat系は選ばない。迷ったら flow系。

■ ステップ2：選んだ type の「系」に対応するJSONだけを返す（説明文・コードブロック禁止）。

共通フィールド（全系で必須）：
- "type": ステップ1で選んだ type 名そのもの
- "tag": 短いラベル（例「市場メモ」「決算速報」「今週の予定」）8文字以内
- "title": 日本語で簡潔に（25〜45文字、ニュースを言い換える。英語タイトルは訳す）
- "hashtags": 最大2個（例 ["#米国株","#金利"]）

系別フィールド（選んだ type の系のものだけ入れる）：
- flow系:     "nodes": [{{"label":"材料/見方/注目点 等","text":"60字以内"}}] を2〜4個
- compare系:  "left":{{"title":"左の見出し","points":["25字以内", ...]}},
              "right":{{"title":"右の見出し","points":["25字以内", ...]}}（各2〜3個）
- stat系:     "stats":[{{"value":"+62","unit":"%","label":"項目名","dir":"up/down/flat"}}] を1〜3個,
              "context":"補足を80字以内"
- timeline系: "events":[{{"when":"火曜/11月 等","text":"50字以内"}}] を2〜5個

ルール：
- ニュースに無い数字や事実は作らない（特に stat系の value はニュースに数字がある場合のみ）
- 「絶対」「確実」「今すぐ買え」等の断定・煽りは禁止
- 中立的に、日本の個人投資家向けに専門的かつ簡潔に
"""


def _normalize(data: dict) -> dict:
    """LLM出力を描画用に整形・クリップ。type は系に変換して検証し、欠損時はflowにフォールバック。"""
    t = data.get("type", "flow")
    fam = _family(t)                       # ← type名ではなく系で分岐
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
        if not data["left"].get("points") or not data["right"].get("points"):
            return _fallback(data)
    elif fam == "stat":
        stats = data.get("stats") or []
        out = []
        for s in stats[:3]:
            out.append({"value": _clip(str(s.get("value", "")), 8),
                        "unit": _clip(str(s.get("unit", "")), 4),
                        "label": _clip(s.get("label", ""), _LIMITS["stat_label"]),
                        "dir": s.get("dir", "flat")})
        data["stats"] = out
        data["context"] = _clip(data.get("context", ""), 90)
        if not out:
            return _fallback(data)
    elif fam == "timeline":
        evs = data.get("events") or []
        data["events"] = [{"when": _clip(e.get("when", ""), 8),
                           "text": _clip(e.get("text", ""), _LIMITS["event_text"])}
                          for e in evs[:5]]
        if not data["events"]:
            return _fallback(data)
    else:
        return _fallback(data)
    return data


def _fallback(data: dict) -> dict:
    """型が壊れていたら最低限の flow にする"""
    data["type"] = "flow"
    data["nodes"] = [{"label": "ポイント", "text": _clip(data.get("title", ""), 70)}]
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
    """JSON(dict) → (画像パス, キャプション, 審査用テキスト)。テスト用にネット不要で呼べる。"""
    data = _normalize(data)
    render_diagram(data, IMAGE_PATH)
    return IMAGE_PATH, _caption(data), _review_text(data), data["type"]


def generate_diagram_image(item, openai_client, model: str):
    """本番用：OpenAI に投げて JSON を得て、画像まで作る。"""
    prompt = build_diagram_prompt(item)
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=2000,
        reasoning_effort="minimal",
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    logger.info(f"図解type: {data.get('type')} / title: {data.get('title')}")
    return build_image_from_data(data)
