"""
weekly_post.py
取得済みイベント（サンプル）→ 正規化 → 選別 → 画像生成。
さらに、選定イベントから「今週の見どころ」導入文を生成してXに画像投稿する。

- 画像レンダリングのみ（既定）: python weekly_post.py
- 実際にXへ投稿:               python weekly_post.py post
  （post時のみ OpenAI/レビュー/tweepy を post.py から遅延import）
"""

import os
import sys

# --- パス・ブートストラップ: src 配下の各機能ディレクトリを import 可能にする ---
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../src
for _sub in ("common", "news_bot", "weekly_bot", "narrative_bot", "scheduler"):
    _p = os.path.join(_SRC_DIR, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import sys
import json
import logging
from collections import defaultdict
from datetime import datetime

from weekly_normalizer import normalize_events
from weekly_selector import select_weekly_events, market_impact_score
from weekly_renderer import render_weekly

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUT_PATH = "/tmp/weekly_events.png"
CAPTION_TARGET = 260   # 目標上限
CAPTION_HARD = 280     # X上限


SAMPLE_RAW_EVENTS = [
    {"date": "2026-06-22", "country": "US", "category": "統計", "time_et": "10:00",
     "title": "米ISM製造業景況指数", "source_name": "ISM", "source_url": "https://www.ismworld.org/"},
    {"date": "2026-06-23", "country": "US", "category": "統計", "time_et": "08:30",
     "title": "米CPI（消費者物価指数）", "source_name": "BLS", "source_url": "https://www.bls.gov/"},
    {"date": "2026-06-24", "country": "US", "category": "統計", "time_et": "08:30",
     "title": "米耐久財受注", "source_name": "Census Bureau", "source_url": "https://www.census.gov/"},
    {"date": "2026-06-24", "country": "US", "category": "企業", "timing": "after market close",
     "title": "Micron Technology 決算発表", "note": "ガイダンス次第で半導体全体に波及",
     "source_name": "Nasdaq Earnings", "source_url": "https://www.nasdaq.com/market-activity/earnings"},
    {"date": "2026-06-24", "country": "US", "category": "中銀", "time_et": "14:00",
     "title": "FOMC 議事要旨 公表", "source_name": "Federal Reserve", "source_url": "https://www.federalreserve.gov/"},
    {"date": "2026-06-25", "country": "US", "category": "統計", "time_et": "08:30",
     "title": "米PCEデフレーター（コア）", "note": "FRBが重視するインフレ指標",
     "source_name": "BEA", "source_url": "https://www.bea.gov/"},
    {"date": "2026-06-25", "country": "US", "category": "統計", "time_et": "08:30",
     "title": "米新規失業保険申請件数", "source_name": "DOL", "source_url": "https://www.dol.gov/"},
    {"date": "2026-06-25", "country": "EU", "category": "中銀", "time_utc": "12:15",
     "title": "ECB理事会 政策金利発表", "source_name": "ECB", "source_url": "https://www.ecb.europa.eu/"},
    {"date": "2026-06-26", "country": "JP", "category": "中銀", "time_jst": "12:00",
     "title": "日銀 金融政策決定会合 結果公表", "source_name": "日本銀行", "source_url": "https://www.boj.or.jp/"},
    {"date": "2026-06-26", "country": "US", "category": "企業", "timing": "before market open",
     "title": "ナイキ 決算発表", "source_name": "Nasdaq Earnings", "source_url": "https://www.nasdaq.com/market-activity/earnings"},
    # 除外される想定
    {"date": "2026-06-26", "country": "JP", "category": "統計", "time_jst": "08:30",
     "title": "東京都区部CPI", "source_name": "総務省", "source_url": "https://www.stat.go.jp/"},
    {"date": "2026-06-22", "country": "JP", "category": "市場", "time_jst": "未定",
     "title": "東京市場 連休明け（海外材料を消化）", "tentative": True},
    {"date": "2026-06-23", "country": "JP", "category": "企業", "time_jst": "10:00",
     "title": "トヨタ 定時株主総会", "source_name": "適時開示", "source_url": "https://www.release.tdnet.info/"},
]


# ========== スケジュール構築 ==========
def _range_label(date_strs: list[str]) -> str:
    ds = sorted(date_strs)
    a = datetime.strptime(ds[0], "%Y-%m-%d")
    b = datetime.strptime(ds[-1], "%Y-%m-%d")
    if a.month == b.month:
        return f"{a.year}年{a.month}月{a.day}日〜{b.day}日"
    return f"{a.year}年{a.month}月{a.day}日〜{b.month}月{b.day}日"


def _md(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}/{d.day}"


def _weekday(date_str: str) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][datetime.strptime(date_str, "%Y-%m-%d").weekday()]


def build_weekly_schedule(raw_events: list[dict]) -> dict:
    events = normalize_events(raw_events)
    for ev in events:
        logger.info(
            "event: source_date=%s / timing=%r / display_date_jst=%s / time_jst=%s / verified=%s / title=%s",
            ev["source_date"], ev["timing"], ev["display_date_jst"], ev["time_jst"], ev["verified"], ev["title"],
        )
    selected = select_weekly_events(events, max_total=10, max_per_day=3)
    if not selected:
        logger.warning("掲載できる確認済みイベントがありません")
        return {"title": "今週の注目イベント", "month_label": "", "days": [], "selected": []}

    by_day = defaultdict(list)
    for ev in selected:
        by_day[ev["display_date_jst"]].append(ev)
    days = [{"date": _md(d), "weekday": _weekday(d), "events": by_day[d]} for d in sorted(by_day)]

    return {
        "title": "今週の注目イベント",
        "month_label": _range_label(list(by_day.keys())),
        "days": days,
        "selected": selected,   # キャプション生成用に保持
    }


# ========== キャプション生成（今週の見どころ） ==========
def _clean_caption(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("```", "").strip()
    if text.startswith("「") and text.endswith("」"):
        text = text[1:-1].strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    return text


def _shorten_caption(text: str, client, model: str) -> str:
    prompt = (
        "次の文章を、意味と最後の『引きの一文』を保ったまま、日本語で260文字以内に短くしてください。"
        "投資助言・断定表現は加えない。本文のみ返す。\n\n" + text
    )
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1000,
            reasoning_effort="minimal",
        )
        s = _clean_caption(r.choices[0].message.content or "")
        if s:
            text = s
    except Exception as e:
        logger.warning(f"キャプション短縮に失敗、ローカル切り詰め: {e}")
    if len(text) > CAPTION_TARGET:
        text = text[:CAPTION_TARGET - 1].rstrip() + "…"
    return text


def generate_weekly_event_caption(events: list[dict]) -> str:
    """選定イベントから米国株向けの『今週の見どころ』導入文を生成する。"""
    from post import get_openai_client, OPENAI_GENERATE_MODEL

    # 重要度順（impact_score優先）に最大3件
    def _rank(e):
        return e.get("impact_score", market_impact_score(e)[0])
    top = sorted(events, key=lambda e: -_rank(e))[:3]

    lines = []
    for e in top:
        note = f"（{e['note']}）" if e.get("note") else ""
        lines.append(
            f'- {e.get("weekday","")} {e.get("time_jst","")} {e.get("country","")} '
            f'{e.get("category","")}: {e["title"]}{note}'
        )
    events_block = "\n".join(lines)

    prompt = f"""あなたは米国株の個人投資家向けに、Xの週次プレビュー投稿を書くアナリストです。
以下は今週の注目イベント（画像に掲載済み）。最も重要なものから最大3つに触れ、
「なぜ今週が米国株にとって重要か」を伝える導入文を作ってください。

今週の注目イベント（この中だけを使う。新しいイベントや日時・数値を足さない）:
{events_block}

条件:
- 日本語、180〜260文字程度
- 米国株投資家向け。少し煽り気味でOK（ただし下品にしない）
- 投資助言・売買推奨・断定的予測は禁止。「買え」「売れ」「爆益」「暴落確定」「確実」等は使わない
- 画像に無いイベントを足さない。日時・数値を捏造しない
- FRB・金利・インフレ・半導体・AI・大型株などの文脈を自然に絡める
- 注目する曜日や時間帯（早朝・夜・週後半 など）に触れる
- 最後に「今週は〇〇が本番」「週後半に注目」のような引きのある一文で締める
- ハッシュタグやURLは付けない。本文のみ返す
"""
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=1200,
        reasoning_effort="minimal",
    )
    caption = _clean_caption(resp.choices[0].message.content or "")
    if len(caption) > CAPTION_HARD:
        caption = _shorten_caption(caption, client, OPENAI_GENERATE_MODEL)
    # 最終保証
    if len(caption) > CAPTION_HARD:
        caption = caption[:CAPTION_HARD - 1].rstrip() + "…"
    return caption


# ========== 画像生成（既存・投稿しない） ==========
def generate_weekly_image(raw_events: list[dict], out_path: str = OUT_PATH) -> str:
    schedule = build_weekly_schedule(raw_events)
    if not schedule["days"]:
        logger.warning("画像生成をスキップ（掲載イベント0件）")
        return ""
    n = sum(len(d["events"]) for d in schedule["days"])
    logger.info(f"週間スケジュール生成: {len(schedule['days'])}日 / {n}イベント / 期間={schedule['month_label']}")
    path = render_weekly(schedule, out_path)
    logger.info(f"画像を出力しました: {path}")
    return path


# ========== 画像＋キャプションでX投稿 ==========
def post_weekly_events(raw_events: list[dict], out_path: str = OUT_PATH):
    from post import review_tweet_with_openai, post_tweet_with_image, NG_WORDS

    schedule = build_weekly_schedule(raw_events)
    if not schedule["days"]:
        logger.warning("掲載イベント0件のため週次投稿をスキップ")
        return None

    selected = schedule["selected"]
    image_path = render_weekly(schedule, out_path)

    caption = generate_weekly_event_caption(selected)
    if len(caption) > CAPTION_HARD:
        caption = caption[:CAPTION_HARD - 1].rstrip() + "…"

    # 要件: ログ出力
    logger.info("selected_events=%s", json.dumps([e["title"] for e in selected], ensure_ascii=False))
    logger.info("generated_caption=%r", caption)
    logger.info("caption_length=%d", len(caption))

    # NGワード（断定・煽りすぎ）チェック
    for w in NG_WORDS:
        if w in caption:
            logger.warning(f"NGワード検出のため週次投稿を中止: {w}")
            return None

    review = review_tweet_with_openai(caption, "今週の注目イベント（週次プレビュー）", "週次イベント")
    logger.info("review_result=%s", json.dumps(review, ensure_ascii=False))
    if not review.get("ok_to_post", False):
        logger.warning(f"AIレビューにより週次投稿中止: {review.get('reason', '理由なし')}")
        return None

    tweet_id = post_tweet_with_image(caption, image_path)
    logger.info(f"週次イベント投稿成功: {tweet_id}")
    return tweet_id


def get_weekly_raw_events() -> list[dict]:
    """実データ(Finnhub)を優先。キー未設定/空/失敗ならサンプルにフォールバック。"""
    try:
        from weekly_events import fetch_weekly_events
        ev = fetch_weekly_events()
        if ev:
            logger.info(f"実データ取得: {len(ev)}件（Finnhub）")
            return ev
        logger.warning("実データが空のためサンプルにフォールバックします")
    except Exception as e:
        logger.warning(f"weekly_events取得失敗、サンプル使用: {e}")
    return SAMPLE_RAW_EVENTS


if __name__ == "__main__":
    raw = get_weekly_raw_events()
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        post_weekly_events(raw)
    else:
        generate_weekly_image(raw)
