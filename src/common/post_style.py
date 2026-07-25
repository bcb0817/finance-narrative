"""5種類の投稿スタイル選択と生成ルール。固定テンプレートは持たない。"""
from __future__ import annotations
import json, random, re
from pathlib import Path

STYLES=("breaking_news","misconception","second_order_effect","comparison","scheduled_summary")

def load_weights(path: Path | None=None) -> dict[str,int]:
    path=path or Path(__file__).resolve().parents[2]/"config"/"post_style_weights.json"
    try: data=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): data={}
    return {name:max(0,int(data.get(name,0))) for name in STYLES}

def choose_style(*, suggested="", recent_styles=(), rng=None) -> str:
    if suggested in STYLES and suggested not in list(recent_styles)[-2:]: return suggested
    weights=load_weights(); recent=set(list(recent_styles)[-2:])
    choices=[s for s in STYLES if s not in recent and weights[s]>0] or list(STYLES)
    return (rng or random).choices(choices,weights=[weights.get(s,1) for s in choices],k=1)[0]

def generation_rules(style: str) -> str:
    focus={"breaking_news":"速報後すぐに、なぜ市場に重要かを示す", "misconception":"多数派の見方と、誤解しやすい点を根拠付きで分ける",
           "second_order_effect":"直接影響の先にある二次的影響と次に見る数字を示す", "comparison":"比較軸を明示し、差が意味することを説明する",
           "scheduled_summary":"複数材料を優先順位付きで短く整理する"}.get(style,"重要性を説明する")
    return f"""投稿タイプ={style}。{focus}。
冒頭20文字以内に企業名、結論、違和感のいずれかを置く。単なる要約は禁止。
「なぜ重要か」と独自の追加解釈を明示する。元資料にない数字・期日・顧客名を作らない。
ハッシュタグは原則0、必要でも1個。同じ定型句や『【市場メモ】』を反復しない。
自然な日本語で、煽り・釣り見出し・売買推奨を避ける。"""

def enforce_hashtag_limit(text: str) -> str:
    seen=0
    def replace(match):
        nonlocal seen
        seen+=1
        return match.group(0) if seen==1 else ""
    return re.sub(r"(?<!\w)#[^\s#]+",replace,text).rstrip()
