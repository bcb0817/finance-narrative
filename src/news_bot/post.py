import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional

# --- パス・ブートストラップ: src 配下の各機能ディレクトリを import 可能にする ---
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../src
for _sub in ("common", "news_bot", "weekly_bot", "narrative_bot", "scheduler"):
    _p = os.path.join(_SRC_DIR, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import tweepy  # noqa: F401  （post_tweet 等の例外型互換のため残す）

# --- 共通処理（common/）---
from safety import (
    MAX_POST_LENGTH, JST, NG_WORDS, PROMPT_SAFETY_RULES,
    is_night_time_jst, clean_text, safety_check,
    POST_VALUE_THRESHOLD, format_decision_log,
)
from openai_client import (
    OPENAI_GENERATE_MODEL, OPENAI_REVIEW_MODEL,
    get_openai_client, generate_by_openai, review_tweet_with_openai,
)
from x_client import (
    get_tweepy_client, get_tweepy_api_v1, post_tweet, post_tweet_with_image,
)

# --- news_bot 内のモジュール ---
from news import fetch_news, NewsItem
from posted_history import add_posted_entry, get_posted_urls
from diagram_post import generate_diagram_image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def build_finance_prompt(
    item: NewsItem,
    *,
    with_link: bool = False,
    diagram: bool = False,
) -> str:
    if diagram:
        return f"""以下の金融ニュースを元に、Xに投稿する日本語の図解風ポストを1つ作成してください。

ニュース：
{item.title}

ソース：
{item.source}

条件：
- 180文字から240文字以内
- 金融クラスタ向けに専門的かつ簡潔に
- 図解風に、矢印・箇条書き・改行を使ってわかりやすく
- 数字・データがニュースタイトルに含まれる場合のみ使う
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

型の例：
【市場メモ】
材料：〇〇
　↓
市場の見方：〇〇
　↓
注目点：〇〇

#株式市場 #米国株
"""

    if with_link:
        length_rule = "100文字から180文字以内（URLは別行で付けるため短めに）"
    else:
        length_rule = "120文字から240文字以内"

    return f"""以下の金融ニュースを元に、Xに投稿する日本語のポストを1つ作成してください。

ニュース：
{item.title}

ソース：
{item.source}

条件：
- {length_rule}
- 日本の個人投資家・金融クラスタ向け
- 専門的だが、読みやすく簡潔に
- 株式市場、金利、為替、マクロ経済への影響を中立的に説明
- 数字・データがニュースタイトルに含まれる場合のみ使う
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

おすすめの型：
【市場メモ】
本文

注目点：〇〇
"""


def needs_background_context(item: NewsItem) -> tuple[bool, str]:
    """
    背景解説が必要なニュースかを抽象的に判定する。
    単純なキーワード一致ではなく、以下6観点を gpt-5-nano で評価する：
      - headline_only_clarity:      見出しだけで意味が伝わるか
      - market_relevance:           市場が反応する理由が明確か
      - required_prior_knowledge:   前提知識が必要か
      - company_context_needed:     企業固有の背景が必要か
      - macro_context_needed:       マクロ・金利・規制・業界文脈が必要か
      - misleading_without_context: 背景なしだと誤解されやすいか
    戻り値: (背景解説が必要か, 判断理由)
    API失敗時は軽量ヒューリスティックに退避する。
    """
    judge_prompt = f"""あなたは金融ニュースの編集者です。
次のニュースを、SNSで一般の個人投資家に伝えるとき「背景解説が必要か」を判定してください。

ニュースタイトル: {item.title}
ソース: {item.source}（種別: {getattr(item, "source_group", "market_news")}）

以下6観点を true/false で評価してください。
- headline_only_clarity:      見出しだけで「何が起きたか」と「なぜ重要か」が伝わる
- market_relevance:           市場が反応する理由が明確である
- required_prior_knowledge:   理解に前提知識が必要
- company_context_needed:     企業固有の背景（株価・業績・財務・資金繰り・継続課題）が必要
- macro_context_needed:       マクロ・金利・規制・業界構造の文脈が必要
- misleading_without_context: 背景なしだと過大/過小評価や誤解をされやすい

判定ルール:
- headline_only_clarity が false、または市場の意味が伝わりにくい、
  または required_prior_knowledge / company_context_needed / macro_context_needed /
  misleading_without_context のいずれかが true なら needs_background = true。

以下のJSONのみを返す（説明文・Markdown禁止）。
{{
  "headline_only_clarity": true,
  "market_relevance": true,
  "required_prior_knowledge": false,
  "company_context_needed": false,
  "macro_context_needed": false,
  "misleading_without_context": false,
  "needs_background": false,
  "reason": "日本語で1文、なぜそう判断したか"
}}"""

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_REVIEW_MODEL,
            messages=[{"role": "user", "content": judge_prompt}],
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
            reasoning_effort="minimal",
        )
        data = json.loads(response.choices[0].message.content or "{}")
        needs = bool(data.get("needs_background", False))
        # 観点からの導出も併用（モデルが needs_background を誤って false にした場合の保険）
        derived = (
            not data.get("headline_only_clarity", True)
            or data.get("required_prior_knowledge", False)
            or data.get("company_context_needed", False)
            or data.get("macro_context_needed", False)
            or data.get("misleading_without_context", False)
        )
        needs = needs or derived
        reason = str(data.get("reason", "")) or "観点評価により背景解説が必要と判断"
        return needs, reason
    except Exception as e:
        logger.warning(f"背景判定APIに失敗、ヒューリスティックに退避: {e}")
        return _needs_background_heuristic(item)


# 背景解説が必要になりやすい語（API失敗時のフォールバック専用）
_CONTEXT_SIGNALS: list[str] = [
    "8-k", "10-k", "10-q", "sec", "filing", "提出", "開示",
    "増資", "希薄化", "dilution", "delist", "上場廃止", "上場維持",
    "債務", "資金調達", "資金繰り", "破産", "chapter 11", "restructur", "リストラ",
    "ガイダンス", "guidance", "下方修正", "上方修正",
    "cpi", "ppi", "雇用統計", "fomc", "frb", "fed", "利上げ", "利下げ", "金利",
    "規制", "規制当局", "反トラスト", "antitrust", "関税", "tariff",
    "オプション", "etf", "信用取引", "空売り", "short squeeze",
]


def _needs_background_heuristic(item: NewsItem) -> tuple[bool, str]:
    text = (item.title + " " + getattr(item, "source_group", "")).lower()
    hits = [w for w in _CONTEXT_SIGNALS if w in text]
    if getattr(item, "source_group", "") in ("official_macro", "company_filings"):
        return True, f"ソース種別({item.source_group})が制度・開示・マクロ文脈を含むため"
    if hits:
        return True, f"背景を要する語を検出({', '.join(hits[:3])})"
    return False, "見出しのみで意味が伝わると判断（ヒューリスティック）"


def build_contextual_finance_prompt(
    item: NewsItem,
    *,
    with_link: bool = False,
    diagram: bool = False,
) -> str:
    """背景解説モードのプロンプト。表面要約を禁止し、文脈・意味・次の確認点を含めさせる。"""
    if diagram:
        length_rule = "180文字から240文字以内"
        format_block = """図解風に、矢印・箇条書き・改行を使ってわかりやすく。
おすすめの型：
【背景メモ】
何が起きた：〇〇
　↓
意味・文脈：〇〇（企業/業界/制度/マクロの背景）
　↓
注目点：〇〇
　↓
次の確認：〇〇"""
    elif with_link:
        length_rule = "100文字から170文字以内（URLは別行で付けるため短めに）"
        format_block = "本文のあと、改行して「注目点：」と「次の確認：」を簡潔に。"
    else:
        length_rule = "120文字から240文字以内"
        format_block = "本文のあと、改行して「注目点：」と「次の確認：」を簡潔に。"

    return f"""以下の金融ニュースを元に、Xに投稿する日本語の「背景解説つき」ポストを1つ作成してください。
表面的な要約だけでは、前提知識のない読者に重要性が伝わりません。背景と意味を補ってください。

ニュース：
{item.title}

ソース：
{item.source}

必ず次の流れを自然に織り込む（見出しの言い換えで終わらせない）：
1. 何が起きたか
2. それが何を意味するのか
3. 背景にある企業・業界・制度・マクロ環境（関係するもののみ）
4. 市場が注目しやすいポイント
5. 次に確認すべき点

厳守ルール：
- {length_rule}
- 日本の個人投資家・金融クラスタ向けに、中立的かつ簡潔に
- {format_block}
- ニュース本文・取得データにないことは断定しない。不確実なことは
  「可能性がある」「警戒されやすい」「注目されやすい」「確認したい」
  「市場が意識しやすい」「文脈で見られやすい」等の表現にする
- 数字・データはニュースタイトルに含まれる場合のみ使う（捏造禁止）
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

【禁止】表面要約だけの投稿（悪い例）：
「〇〇が8-Kを提出。詳細はSEC提出書類を確認。」
【目指す形（良い例）】：
「〇〇が8-Kを提出。単なる書類提出ではなく、同社の株価低迷や資金調達懸念の文脈で見られやすい材料。
注目点：開示が上場維持・希薄化・資金繰りに関係するか。次の確認：追加開示と次回決算。」
"""


def _choose_prompt(item: NewsItem, *, with_link: bool = False, diagram: bool = False) -> str:
    """背景解説が必要かを判定し、適切なプロンプトを返す（ログ付き）。"""
    needs, reason = needs_background_context(item)
    if needs:
        prompt_type = "contextual"
        prompt = build_contextual_finance_prompt(item, with_link=with_link, diagram=diagram)
    else:
        prompt_type = "standard"
        prompt = build_finance_prompt(item, with_link=with_link, diagram=diagram)
    logger.info(
        f"needs_background_context={str(needs).lower()} / "
        f"background_reason={reason!r} / selected_prompt_type={prompt_type}"
    )
    return prompt


def generate_tweet_with_link(item: NewsItem) -> str:
    prompt = _choose_prompt(item, with_link=True)
    text = generate_by_openai(prompt, max_tokens=2000)
    return f"{text}\n{item.url}"


def generate_tweet_without_link(item: NewsItem) -> str:
    prompt = _choose_prompt(item, with_link=False)
    return generate_by_openai(prompt, max_tokens=2000)


def generate_tweet_diagram(item: NewsItem) -> str:
    prompt = _choose_prompt(item, diagram=True)
    return generate_by_openai(prompt, max_tokens=4000)


def create_tweet(mode: str, item: NewsItem) -> str:
    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        return generate_tweet_with_link(item)
    if mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        return generate_tweet_diagram(item)
    logger.info("リンクなし投稿を生成中...")
    return generate_tweet_without_link(item)


def handle_image_post(item: NewsItem, post_value=0) -> None:
    oai = get_openai_client()
    result = generate_diagram_image(item, oai, OPENAI_GENERATE_MODEL)
    if result is None:
        logger.warning("図解の生成に失敗したため、今回の投稿をスキップします")
        logger.info(format_decision_log(
            selected_post_type="news_summary", post_value=post_value,
            skip_reason="diagram_generation_failed", source_titles=[item.title]))
        return
    image_path, caption, review_text, dtype = result
    logger.info(f"図解type={dtype} / caption={caption!r}")

    if len(caption) > MAX_POST_LENGTH:
        logger.error(f"キャプションが長すぎます: {len(caption)}文字")
        logger.info(format_decision_log(
            selected_post_type="news_summary", post_value=post_value,
            skip_reason="caption_too_long", source_titles=[item.title],
            image_path=image_path))
        return
    for word in NG_WORDS:
        if word in review_text:
            logger.error(f"NGワードを検出しました: {word}")
            logger.info(format_decision_log(
                selected_post_type="news_summary", post_value=post_value,
                skip_reason=f"ng_word:{word}", source_titles=[item.title],
                image_path=image_path))
            return

    review = review_tweet_with_openai(review_text, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")
    if not review.get("ok_to_post", False):
        logger.info(format_decision_log(
            selected_post_type="news_summary", post_value=post_value,
            skip_reason=f"ai_review_ng:{review.get('reason','')}",
            source_titles=[item.title], image_path=image_path))
        return

    tweet_id = post_tweet_with_image(caption, image_path)
    add_posted_entry(item, tweet_id=tweet_id, mode="image")
    logger.info(format_decision_log(
        selected_post_type="news_summary", post_value=post_value,
        source_titles=[item.title], final_caption=caption,
        image_path=image_path, tweet_id=tweet_id))


# 通常ニュースBotは「高投稿価値だけ」方針。post_value>=8 のみ投稿。
IMPACT_SKIP_LEVEL = "low"  # 後方互換（未使用化）


def assess_market_impact(item: NewsItem) -> dict:
    """
    投稿候補ニュースを post_value(1-10) で採点する。
    返り値: post_value(int), should_post(bool), impact_level, market_scope,
            is_material, reason, skip_reason
    AI失敗時は should_post=False（高価値だけ通す方針なのでフェイルクローズ）。
    """
    prompt = f"""あなたは機関投資家向けのニュース編集者です。
次の金融ニュースを「米国株の投資家に今すぐ伝える価値」で 1〜10 の post_value で採点してください。

ニュースタイトル: {item.title}
ソース: {item.source}（種別: {getattr(item, 'source_group', 'market_news')}）

【post_value 基準（厳格に）】
- 10: 米国株市場全体を動かす最重要材料（FOMC/CPI/雇用/主要ハイテク決算など）
- 9 : NASDAQ/S&P500、金利、ドル、半導体、大型テックに強く影響
- 8 : 主要セクター・大型株に明確な影響
- 7 : 重要だが投稿するほどではない
- 6以下: ノイズ、局所的、材料不足、単独の提出通知、〇〇が発表だけ

【8未満に下げるべき型】
- 「〇〇が発表」「市場は注目」など中身の薄い見出し
- 小型株の単独IR・8-K/SEC提出通知だけ
- 日本ローカル（東京都区部CPI、東京市場コメント等）
- 出所不明・うわさベース
- 取得シグナルで裏づけられない断定

以下のJSONのみ返す（説明文・Markdown禁止）。
{{
  "post_value": 1〜10の整数,
  "impact_level": "high" or "medium" or "low",
  "market_scope": "market_wide" or "sector" or "single_name" or "none",
  "is_material": true,
  "reason": "日本語1文で理由"
}}"""
    try:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model=OPENAI_REVIEW_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
            reasoning_effort="minimal",
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        try:
            pv = int(data.get("post_value", 0))
        except Exception:
            pv = 0
        data["post_value"] = pv
        data["should_post"] = pv >= POST_VALUE_THRESHOLD
        data["skip_reason"] = "" if data["should_post"] else f"post_value<{POST_VALUE_THRESHOLD}"
        return data
    except Exception as e:
        logger.warning(f"インパクト判定API失敗、高価値方針によりスキップ（フェイルクローズ）: {e}")
        return {"post_value": 0, "impact_level": "unknown", "market_scope": "unknown",
                "is_material": False, "reason": f"判定不能: {e}",
                "should_post": False, "skip_reason": "assess_failed"}


def main(mode: str = "image") -> None:
    logger.info(f"mode: {mode}")
    # 通常ニュース要約は格下げ。リンクは付けず（ソース名のみ）、高価値だけ投稿。
    if mode == "link":
        logger.info("linkモードは廃止（ソース名のみ・URLなし方針）。no-linkに切替。")
        mode = "normal"

    posted_urls = get_posted_urls()
    logger.info(f"投稿済みURL数: {len(posted_urls)}")

    item: Optional[NewsItem] = fetch_news(posted_urls=posted_urls)
    if not item:
        logger.error("ニュース取得失敗")
        logger.info(format_decision_log(
            selected_post_type="news_summary", skip_reason="no_news"))
        return

    logger.info(f"取得ニュース: {item.title}")
    logger.info(f"ソース: {item.source}")

    # 投稿価値ゲート（post_value>=8 のみ）。
    impact = assess_market_impact(item)
    pv = impact.get("post_value", 0)
    if not impact.get("should_post", False):
        logger.info(format_decision_log(
            selected_post_type="news_summary",
            post_value=pv,
            skip_reason=impact.get("skip_reason") or "low_value",
            source_titles=[item.title],
        ))
        logger.info(f"投稿価値が基準未満のためスキップ: {impact.get('reason')}")
        return

    if mode == "image":
        handle_image_post(item, post_value=pv)
        return

    tweet = create_tweet(mode, item)

    try:
        safety_check(tweet)
    except ValueError as e:
        logger.error(f"safety_check NG: {e}")
        logger.info(format_decision_log(
            selected_post_type="news_summary", post_value=pv,
            skip_reason=f"safety_check:{e}", source_titles=[item.title]))
        return

    review = review_tweet_with_openai(tweet, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")

    if not review.get("ok_to_post", False):
        logger.info(format_decision_log(
            selected_post_type="news_summary", post_value=pv,
            skip_reason=f"ai_review_ng:{review.get('reason','')}",
            source_titles=[item.title]))
        return

    tweet_id = post_tweet(tweet)
    add_posted_entry(item, tweet_id=tweet_id, mode=mode)
    logger.info(format_decision_log(
        selected_post_type="news_summary", post_value=pv,
        source_titles=[item.title], final_caption=tweet, tweet_id=tweet_id))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "image"
    main(mode)
