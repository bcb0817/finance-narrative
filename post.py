import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import tweepy
from openai import OpenAI

from news import fetch_news, NewsItem
from posted_history import add_posted_entry, get_posted_urls
from diagram_post import generate_diagram_image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


OPENAI_GENERATE_MODEL = os.getenv("OPENAI_GENERATE_MODEL", "gpt-5-mini")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5-nano")

MAX_POST_LENGTH = 280

JST = timezone(timedelta(hours=9))

NG_WORDS: list[str] = [
    "絶対",
    "確実",
    "爆益",
    "爆上げ",
    "暴落確定",
    "急騰確定",
    "今すぐ買え",
    "今すぐ売れ",
    "買い一択",
    "売り一択",
    "買うべき",
    "売るべき",
    "必ず上がる",
    "必ず下がる",
    "テンバガー確定",
]

PROMPT_SAFETY_RULES = """
- ニュースにない数字や事実は作らない"""


def get_tweepy_client() -> tweepy.Client:
    required_envs = [
        "API_KEY",
        "API_KEY_SECRET",
        "ACCESS_TOKEN",
        "ACCESS_TOKEN_SECRET",
    ]
    for key in required_envs:
        if not os.getenv(key):
            raise RuntimeError(f"環境変数が未設定です: {key}")

    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )


def get_tweepy_api_v1() -> tweepy.API:
    """画像アップロード用の v1.1 クライアント（OAuth 1.0a）"""
    auth = tweepy.OAuth1UserHandler(
        os.environ["API_KEY"], os.environ["API_KEY_SECRET"],
        os.environ["ACCESS_TOKEN"], os.environ["ACCESS_TOKEN_SECRET"],
    )
    return tweepy.API(auth)


def get_openai_client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("環境変数が未設定です: OPENAI_API_KEY")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def is_night_time_jst() -> bool:
    """JST 00:00〜04:29 は投稿禁止時間帯"""
    now_jst = datetime.now(JST)
    minutes = now_jst.hour * 60 + now_jst.minute
    return 0 <= minutes < (4 * 60 + 30)


def clean_text(text: str) -> str:
    text = text.strip()
    if text.startswith("「") and text.endswith("」"):
        text = text[1:-1].strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    return text


def safety_check(text: str) -> None:
    if not text or not text.strip():
        raise ValueError("投稿本文が空です")
    if len(text) > MAX_POST_LENGTH:
        raise ValueError(f"投稿本文が長すぎます: {len(text)}文字")
    for word in NG_WORDS:
        if word in text:
            raise ValueError(f"NGワードを検出しました: {word}")


def generate_by_openai(prompt: str, max_tokens: int = 2000) -> str:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=max_tokens,
        reasoning_effort="minimal",
    )
    text = response.choices[0].message.content or ""
    return clean_text(text)


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


def review_tweet_with_openai(text: str, news_title: str, source: str) -> dict:
    """投稿前にAIで内容をレビューし、投稿可否をJSONで返す"""
    client = get_openai_client()

    review_prompt = f"""あなたは金融SNS投稿のコンプライアンス審査担当です。
以下のX投稿文を審査し、投稿してよいか判断してください。

【元ニュース】
タイトル: {news_title}
ソース: {source}

【審査対象の投稿文】
{text}

【審査基準】
- ニュースにない数字や事実を捏造していないか
- 誤解を招く内容でないか

以下のJSON形式のみで返答してください。説明文は不要です。
{{
  "ok_to_post": true,
  "risk_level": "low",
  "reason": "投稿してよい理由またはNG理由",
  "contains_investment_advice": false,
  "contains_buy_sell_recommendation": false,
  "contains_unverified_numbers": false,
  "too_aggressive": false
}}

risk_level は "low" / "medium" / "high" のいずれかにしてください。"""

    try:
        response = client.chat.completions.create(
            model=OPENAI_REVIEW_MODEL,
            messages=[{"role": "user", "content": review_prompt}],
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
            reasoning_effort="minimal",
        )
        raw = response.choices[0].message.content or "{}"
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"レビュー結果のJSONパース失敗: {e}")
        return {
            "ok_to_post": False,
            "risk_level": "high",
            "reason": f"レビュー結果のパースに失敗: {e}",
            "contains_investment_advice": False,
            "contains_buy_sell_recommendation": False,
            "contains_unverified_numbers": False,
            "too_aggressive": False,
        }
    except Exception as e:
        logger.error(f"レビューAPI呼び出し失敗: {e}")
        return {
            "ok_to_post": False,
            "risk_level": "high",
            "reason": f"レビューAPIエラー: {e}",
            "contains_investment_advice": False,
            "contains_buy_sell_recommendation": False,
            "contains_unverified_numbers": False,
            "too_aggressive": False,
        }

    return result


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


def _choose_prompt(
    item: NewsItem,
    *,
    with_link: bool = False,
    diagram: bool = False,
    needs: bool | None = None,
    reason: str = "",
) -> str:
    """背景解説が必要かを判定し、適切なプロンプトを返す（ログ付き）。
    needs が渡されればそれを使い、無ければ needs_background_context() を呼ぶ。"""
    if needs is None:
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


def generate_tweet_with_link(item: NewsItem, needs_bg: bool | None = None, bg_reason: str = "") -> str:
    prompt = _choose_prompt(item, with_link=True, needs=needs_bg, reason=bg_reason)
    text = generate_by_openai(prompt, max_tokens=2000)
    return f"{text}\n{item.url}"


def generate_tweet_without_link(item: NewsItem, needs_bg: bool | None = None, bg_reason: str = "") -> str:
    prompt = _choose_prompt(item, with_link=False, needs=needs_bg, reason=bg_reason)
    return generate_by_openai(prompt, max_tokens=2000)


def generate_tweet_diagram(item: NewsItem, needs_bg: bool | None = None, bg_reason: str = "") -> str:
    prompt = _choose_prompt(item, diagram=True, needs=needs_bg, reason=bg_reason)
    return generate_by_openai(prompt, max_tokens=4000)


def assess_market_impact(item: NewsItem) -> dict:
    """
    ニュースが株式市場・相場に与えるインパクトを OPENAI_REVIEW_MODEL で判定する。
    low（市場影響が小さい/文脈が弱い）なら投稿しない。
    戻り値dict: should_post, impact_level, reason, market_scope,
                affected_assets, is_material, needs_background_context
    API失敗時は既定で投稿可（should_post=True, medium）に退避する。
    """
    judge_prompt = f"""あなたは機関投資家向けニュースデスクの編集者です。
次のニュースを、米国株・日本株トレーダー向けに「投稿する価値があるか（相場への影響度）」で判定してください。

ニュースタイトル: {item.title}
ソース: {item.source}（種別: {getattr(item, "source_group", "market_news")}）

【投稿すべき内容】
- FRB・金利・CPI・PCE・雇用統計・GDP・PMI などのマクロ材料
- S&P500・NASDAQ・Dow・Russell・VIX・米国債・ドル円・原油・金 に影響する材料
- Nvidia・Apple・Microsoft・Amazon・Meta・Tesla・Google など大型株の重要材料
- 半導体・AI・銀行・エネルギー・防衛・ヘルスケア など主要セクターに影響する材料
- 決算・ガイダンス・業績修正・売上警告
- 大型M&A・規制・訴訟・制裁・関税・地政学
- 増資・希薄化・上場廃止・破産・債務不履行 など株価に大きく影響しうる材料
- 株価が大きく動いている理由を説明できる材料

【投稿しない内容（low）】
- 単なるSEC提出通知
- 8-K・10-Q・10-K の提出だけで中身が不明なもの
- 軽微なIR・イベント参加・カンファレンス登壇
- 小規模な提携や受注で金額・業績影響が不明なもの
- 軽微な人事異動
- 既出情報の再掲
- 市場全体にも主要セクターにも影響しない小ネタ
- 投稿しても「で？」となるニュース

【特に重要】「8-Kを提出した」だけでは投稿しない（low）。
8-Kの中身が上場維持・増資・希薄化・債務・決算・重要契約・経営陣変更など
市場影響のある内容だと確認できる場合のみ medium 以上にする。

以下のJSONのみを返す（説明文・Markdown禁止）。
{{
  "should_post": true,
  "impact_level": "high",
  "reason": "投稿すべき/しない理由を日本語で1文",
  "market_scope": "market_wide",
  "affected_assets": ["米国株", "金利"],
  "is_material": true,
  "needs_background_context": false
}}

- impact_level: "high"(相場全体/主要指数/主要セクター/大型株に影響) /
                "medium"(個別株や特定テーマに一定の影響) /
                "low"(市場影響が小さい・文脈が弱い→投稿しない)
- market_scope: "market_wide" / "sector" / "major_company" / "small_company" / "noise"
- impact_level が "low" のときは should_post を false にする。"""

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
    except Exception as e:
        logger.warning(f"市場インパクト判定APIに失敗、既定で投稿します: {e}")
        return {
            "should_post": True,
            "impact_level": "medium",
            "reason": f"判定APIエラーのため既定で投稿（要確認）: {e}",
            "market_scope": "major_company",
            "affected_assets": [],
            "is_material": True,
            "needs_background_context": True,
        }

    # 既定値で補完し、low は必ず should_post=False に正規化
    data.setdefault("impact_level", "medium")
    data.setdefault("market_scope", "")
    data.setdefault("reason", "")
    data.setdefault("affected_assets", [])
    data.setdefault("is_material", True)
    data.setdefault("needs_background_context", False)
    if data["impact_level"] == "low":
        data["should_post"] = False
    else:
        data.setdefault("should_post", True)
    return data


def create_tweet(mode: str, item: NewsItem, needs_bg: bool | None = None, bg_reason: str = "") -> str:
    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        return generate_tweet_with_link(item, needs_bg, bg_reason)
    if mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        return generate_tweet_diagram(item, needs_bg, bg_reason)
    logger.info("リンクなし投稿を生成中...")
    return generate_tweet_without_link(item, needs_bg, bg_reason)


def post_tweet(text: str) -> str:
    client = get_tweepy_client()
    try:
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])
        logger.info(f"投稿成功: {tweet_id}")
        logger.info(f"内容: {text}")
        return tweet_id
    except tweepy.TweepyException as e:
        logger.error(f"投稿失敗: {e}")
        raise


def post_tweet_with_image(text: str, image_path: str) -> str:
    api_v1 = get_tweepy_api_v1()
    media = api_v1.media_upload(filename=image_path)   # v1.1 でアップ
    client = get_tweepy_client()                       # v2 で投稿
    response = client.create_tweet(text=text, media_ids=[media.media_id])
    tweet_id = str(response.data["id"])
    logger.info(f"画像つき投稿成功: {tweet_id}")
    logger.info(f"キャプション: {text}")
    return tweet_id


def handle_image_post(item: NewsItem) -> None:
    oai = get_openai_client()
    result = generate_diagram_image(item, oai, OPENAI_GENERATE_MODEL)
    if result is None:
        logger.warning("図解の生成に失敗したため、今回の投稿をスキップします")
        return
    image_path, caption, review_text, dtype = result
    logger.info(f"図解type={dtype} / caption={caption!r}")

    if len(caption) > MAX_POST_LENGTH:
        logger.error(f"キャプションが長すぎます: {len(caption)}文字")
        return
    for word in NG_WORDS:
        if word in review_text:
            logger.error(f"NGワードを検出しました: {word}")
            return

    review = review_tweet_with_openai(review_text, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")
    if not review.get("ok_to_post", False):
        logger.warning(f"AIレビューにより投稿中止: {review.get('reason', '理由なし')}")
        return

    tweet_id = post_tweet_with_image(caption, image_path)
    add_posted_entry(item, tweet_id=tweet_id, mode="image")


def main(mode: str = "dry-run") -> None:
    logger.info(f"mode: {mode}")

    if mode not in ("dry-run", "image", "diagram") and is_night_time_jst():
        now_jst = datetime.now(JST).strftime("%H:%M")
        logger.info(f"深夜帯（JST {now_jst}）のため投稿をスキップします（00:00〜04:29は禁止）")
        return

    posted_urls = get_posted_urls()
    logger.info(f"投稿済みURL数: {len(posted_urls)}")

    item: Optional[NewsItem] = fetch_news(posted_urls=posted_urls)
    if not item:
        logger.error("ニュース取得失敗")
        return

    logger.info(f"取得ニュース: {item.title}")
    logger.info(f"ソース: {item.source}")

    # === 市場インパクト判定（low は投稿しない） ===
    impact = assess_market_impact(item)
    impact_level = impact.get("impact_level", "medium")
    market_scope = impact.get("market_scope", "")
    logger.info(
        f"market_impact: impact_level={impact_level} / market_scope={market_scope} / "
        f"affected_assets={impact.get('affected_assets')} / should_post={impact.get('should_post')}"
    )

    should_post = impact.get("should_post", True) and impact_level != "low"
    # dry-run は検証用なので low でもスキップせず生成プレビューを出す（判定はログに残す）
    if not should_post and mode != "dry-run":
        reason = impact.get("reason", "市場影響が確認できないため")
        logger.info(f"Market impact low. Skip post: {reason}")
        logger.info(
            f"skip_reason={reason!r} / impact_level={impact_level} / "
            f"market_scope={market_scope} / title={item.title!r} / source={item.source}"
        )
        return

    # 市場インパクト判定が返した背景フラグを背景解説モードに使い回す
    needs_bg = bool(impact.get("needs_background_context", False))
    bg_reason = "市場インパクト判定で背景文脈が必要と判断" if needs_bg else "見出しで意味が伝わると判断"

    if mode == "image":
        handle_image_post(item)
        return

    tweet = create_tweet(mode, item, needs_bg=needs_bg, bg_reason=bg_reason)

    try:
        safety_check(tweet)
    except ValueError as e:
        logger.error(f"safety_check NG: {e}")
        logger.info(f"投稿スキップ。生成文:\n{tweet}")
        return

    review = review_tweet_with_openai(tweet, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")

    if not review.get("ok_to_post", False):
        logger.warning(f"AIレビューにより投稿中止: {review.get('reason', '理由なし')}")
        logger.info(f"投稿スキップ。生成文:\n{tweet}")
        return

    if mode == "dry-run":
        logger.info("=== DRY RUN: 投稿はしません ===")
        logger.info(f"文字数: {len(tweet)}")
        logger.info(f"内容:\n{tweet}")
        return

    if mode in ["post", "normal", "link", "diagram"]:
        tweet_id = post_tweet(tweet)
        add_posted_entry(item, tweet_id=tweet_id, mode=mode)
        return

    logger.error(f"不明なmodeです: {mode}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    main(mode)
