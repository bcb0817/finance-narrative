import os
import sys
import json
import re
import logging
from datetime import datetime, timezone
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
    normalize_generated_post_text, generated_post_quality_error,
    NEWS_BOT_POST_VALUE_THRESHOLD, format_decision_log,
)
from openai_client import (
    OPENAI_GENERATE_MODEL, OPENAI_REVIEW_MODEL,
    get_openai_client, generate_by_openai, review_tweet_with_openai,
    shorten_tweet_with_openai,
)
from x_client import (
    get_tweepy_client, get_tweepy_api_v1, post_tweet, post_tweet_with_image,
)
from post_registry import hours_since_last_post, posting_inactive
from openai_config import OpenAIRole, model_for
from openai_service import semantic_duplicate
from post_style import choose_style, enforce_hashtag_limit, generation_rules
from dynamic_posting import posting_window

# --- news_bot 内のモジュール ---
from news import fetch_news, NewsItem
from posted_history import add_posted_entry, get_posted_urls
from diagram_post import assess_diagram_value, generate_diagram_image


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
- 構成は「結論→理由→注目点」の順にする
- 内容に合う絵文字を0〜3個だけ使ってよい（飾りだけの絵文字は禁止）
- 長い箇条書き、無意味な矢印、図解風の装飾は禁止
- 株式市場、金利、為替、マクロ経済への影響を中立的に説明
- 数字・データがニュースタイトルに含まれる場合のみ使う
{PROMPT_SAFETY_RULES}
- ハッシュタグは最大2個
- URLは含めない
- 投稿本文のみ返答する

おすすめの型：
結論：〇〇
理由：〇〇
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
            model=model_for(OpenAIRole.CLASSIFY),
            openai_role="classify",
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
        format_block = "「結論→理由→注目点」の順にし、最後に「次の確認：」を簡潔に。"
    else:
        length_rule = "120文字から240文字以内"
        format_block = "「結論→理由→注目点」の順にし、最後に「次の確認：」を簡潔に。"

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
- 内容に合う絵文字を0〜3個だけ使ってよい（飾りだけの絵文字は禁止）
- 長い箇条書き、無意味な矢印、図解風の装飾は禁止
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


def _integrated_prompt_context(research_context: dict | None) -> str:
    context = research_context or {}
    if not context.get("xai_integrated_context_used"):
        return ""
    summary = str(context.get("xai_integrated_summary") or "")[:500]
    quality = str(context.get("xai_integrated_evidence_quality") or "low")
    readiness = str(context.get("xai_integrated_posting_readiness") or "")
    requires_confirmation = bool(
        context.get("xai_integrated_requires_confirmation")
    )
    return f"""

【X上の反応を含む統合リサーチ（事実ソースではなく編集参考）】
- 統合された市場の見方: {summary}
- 証拠品質: {quality}
- 判定: {readiness}
- 追加確認が必要: {str(requires_confirmation).lower()}

利用ルール:
- ニュースタイトルと正式ソースを事実の基準にする
- 統合リサーチは「市場では〜との見方」のような解釈整理にだけ使う
- 追加確認が必要な内容、因果関係、未確認の数字を本文で断定しない
- 反応数をX全体や市場全体の総意として表現しない
"""


def _provider_isolated_prompt_context(research_context: dict | None) -> str:
    context = research_context or {}
    if not context.get("provider_isolated_editorial"):
        return ""
    return """

【公式ソース限定の市場解説】
- この投稿の根拠は上記の公式ソースと見出しだけ
- Twelve Dataその他の市場データAPI、内部検知、価格、騰落率、時間足を使わない
- 現在の相場方向、現在値、急騰・急落を推測または追加しない
- 見出しにない数字を追加しない
- チャートやリアルタイムデータを参照したような表現をしない
- 発表の意味、注目点、次に確認する公式情報を中心に説明する
"""


def _independent_confirmation_prompt_context(
    research_context: dict | None,
) -> str:
    context = research_context or {}
    if not context.get("independent_confirmation"):
        return ""
    return """

【独立確認ソース限定】
- 内部の市場監視は、このニュースを調べるきっかけにすぎない
- 投稿の事実・数値・解説は、上記ニュースタイトルと独立ソースだけを根拠にする
- 内部監視の価格、騰落率、方向、時間幅、チャートを推測・引用・再計算しない
- タイトルにない数値やリアルタイム相場を追加しない
- 原因と値動きの因果関係を断定しない
- 「別の公開情報で確認できた事実」と「考えられる市場の注目点」を分ける
"""


def _choose_prompt(
    item: NewsItem, *, with_link: bool = False, diagram: bool = False,
    research_context: dict | None = None,
) -> str:
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
    try:
        from common.teacher_data import prompt_context
        prompt += prompt_context(item.title)
    except Exception as exc:
        logger.warning("teacher context unavailable; generation continues: %s", type(exc).__name__)
    prompt += _integrated_prompt_context(research_context)
    prompt += _provider_isolated_prompt_context(research_context)
    prompt += _independent_confirmation_prompt_context(research_context)
    return prompt


def generate_tweet_with_link(
    item: NewsItem, *, research_context: dict | None = None
) -> str:
    prompt = _choose_prompt(item, with_link=True, research_context=research_context)
    text = generate_by_openai(prompt, max_tokens=2000)
    return f"{text}\n{item.url}"


def generate_tweet_without_link(
    item: NewsItem, *, research_context: dict | None = None
) -> str:
    prompt = _choose_prompt(item, with_link=False, research_context=research_context)
    return generate_by_openai(prompt, max_tokens=2000)


def generate_tweet_diagram(
    item: NewsItem, *, research_context: dict | None = None
) -> str:
    prompt = _choose_prompt(item, diagram=True, research_context=research_context)
    return generate_by_openai(prompt, max_tokens=4000)


def create_tweet(
    mode: str, item: NewsItem, *, style: str = "breaking_news",
    research_context: dict | None = None,
) -> str:
    if mode == "link":
        logger.info("リンクあり投稿を生成中...")
        return generate_tweet_with_link(item, research_context=research_context)
    if mode == "diagram":
        logger.info("図解形式の投稿を生成中...")
        return generate_tweet_diagram(item, research_context=research_context)
    logger.info("リンクなし投稿を生成中...")
    prompt = _choose_prompt(
        item, with_link=False, research_context=research_context
    ) + "\n\n【今回の編集ルール】\n" + generation_rules(style)
    return enforce_hashtag_limit(generate_by_openai(prompt, max_tokens=2000))


def _news_log(
    *, selected_news_title="", source="-", selected_post_type="news_summary",
    post_value="-", us_equity_relevance="-", social_buzz_score="-",
    narrative_value="-", theme_relevance="-", market_scope="-",
    threshold=None, should_post="-",
    skip_reason="-", safety_check_result="-", shortened=False, tweet_id="-",
    dry_run=None, actual_post_attempted=False,
    evaluated_history_hit=False, duplicate_title_hit=False,
) -> None:
    """通常Botの判断ログ（要件の全フィールドを毎run出す）。"""
    if threshold is None:
        threshold = NEWS_BOT_POST_VALUE_THRESHOLD
    if dry_run is None:
        dry_run = not _post_enabled_now()
    logger.info(
        "[NEWS] selected_news_title=%r | source=%s | selected_post_type=%s | "
        "post_value=%s | us_equity_relevance=%s | social_buzz_score=%s | "
        "narrative_value=%s | theme_relevance=%s | market_scope=%s | threshold=%s | "
        "post_enabled=%s | dry_run=%s | should_post=%s | actual_post_attempted=%s | "
        "skip_reason=%s | evaluated_history_hit=%s | duplicate_title_hit=%s | "
        "safety_check_result=%s | shortened=%s | tweet_id=%s",
        selected_news_title, source, selected_post_type,
        post_value, us_equity_relevance, social_buzz_score,
        narrative_value, theme_relevance, market_scope, threshold,
        str(_post_enabled_now()).lower(), str(bool(dry_run)).lower(),
        str(should_post).lower() if isinstance(should_post, bool) else should_post,
        str(bool(actual_post_attempted)).lower(),
        skip_reason or "-", str(bool(evaluated_history_hit)).lower(),
        str(bool(duplicate_title_hit)).lower(),
        safety_check_result or "-",
        str(bool(shortened)).lower(), tweet_id or "-",
    )
    try:
        from runtime import log_decision
        log_decision({
            "bot": "news", "selected_news_title": selected_news_title, "source": source,
            "selected_post_type": selected_post_type, "post_value": post_value,
            "us_equity_relevance": us_equity_relevance, "social_buzz_score": social_buzz_score,
            "narrative_value": narrative_value, "theme_relevance": theme_relevance,
            "market_scope": market_scope, "threshold": threshold,
            "should_post": should_post, "skip_reason": skip_reason,
            "safety_check_result": safety_check_result, "shortened": bool(shortened),
            "tweet_id": tweet_id, "post_enabled": _post_enabled_now(),
            "dry_run": bool(dry_run) if dry_run is not None else (not _post_enabled_now()),
            "actual_post_attempted": bool(actual_post_attempted),
            "evaluated_history_hit": bool(evaluated_history_hit),
            "duplicate_title_hit": bool(duplicate_title_hit),
        })
    except Exception:
        pass


def ensure_postable(text: str, *, max_chars: int = 240) -> tuple[bool, str, str, bool]:
    """投稿可否を確定する。長すぎる場合のみOpenAIで短縮リライトして再チェック。
    戻り値: (ok, text, safety_check_result, shortened)
      - 空文字  → ok=False / "empty"
      - NGワード → ok=False / "ng_word:<w>"
      - 長すぎ  → 短縮を試し、OKなら ok=True/"ok_after_shorten"/shortened=True
                 だめなら ok=False/"too_long_after_shorten:<n>" など
    """
    text = normalize_generated_post_text(text)
    if not text:
        return False, text, "empty", False
    quality_error = generated_post_quality_error(text)
    if quality_error:
        return False, text, f"malformed:{quality_error}", False
    for w in NG_WORDS:
        if w in text:
            return False, text, f"ng_word:{w}", False
    if len(text) <= MAX_POST_LENGTH:
        return True, text, "ok", False

    # 280字超 → 180〜240字に短縮リライト
    logger.info(f"本文が長いため短縮リライトを試行: {len(text)}字 → 目安{max_chars}字以内")
    shortened_text = normalize_generated_post_text(
        shorten_tweet_with_openai(text, max_chars=max_chars)
    )
    shortened = True
    if not shortened_text or not shortened_text.strip():
        return False, text, "empty_after_shorten", shortened
    for w in NG_WORDS:
        if w in shortened_text:
            return False, shortened_text, f"ng_word_after_shorten:{w}", shortened
    quality_error = generated_post_quality_error(shortened_text)
    if quality_error:
        return False, shortened_text, f"malformed_after_shorten:{quality_error}", shortened
    if len(shortened_text) > MAX_POST_LENGTH:
        return False, shortened_text, f"too_long_after_shorten:{len(shortened_text)}", shortened
    return True, shortened_text, "ok_after_shorten", shortened


def append_source_attribution(
    text: str, source: str, *, max_chars: int = 280
) -> str:
    """Keep an independent source visible without cutting it off."""
    label = f"\n出典: {str(source or '公開情報').strip()}"
    body_limit = max(1, max_chars - len(label))
    body = str(text or "").strip()
    if len(body) > body_limit:
        candidate = body[:body_limit].rstrip()
        sentence_end = max(
            candidate.rfind("。"),
            candidate.rfind("！"),
            candidate.rfind("？"),
            candidate.rfind("\n"),
        )
        if sentence_end >= 100:
            candidate = candidate[:sentence_end + 1].rstrip()
        else:
            candidate = candidate.rstrip("、,，:：;； ") + "。"
        body = candidate[:body_limit].rstrip()
    return f"{body}{label}"


def handle_image_post(item: NewsItem, impact: dict | None = None) -> None:
    impact = impact or {}
    _src = item.source

    def _log(**kw):
        _news_log(
            selected_news_title=item.title, source=_src, selected_post_type="image",
            post_value=impact.get("post_value", "-"),
            us_equity_relevance=impact.get("us_equity_relevance", "-"),
            social_buzz_score=impact.get("social_buzz_score", "-"),
            narrative_value=impact.get("narrative_value", "-"),
            theme_relevance=impact.get("theme_relevance", "-"),
            market_scope=impact.get("market_scope", "-"),
            should_post=True, **kw,
        )

    oai = get_openai_client()
    result = generate_diagram_image(item, oai, OPENAI_GENERATE_MODEL)
    if result is None:
        _log(skip_reason="diagram_generation_failed")
        return
    image_path, caption, review_text, dtype = result
    logger.info(f"図解type={dtype} / caption={caption!r}")

    # 文字数オーバーは即スキップせず短縮を試す
    ok, caption, safety_result, shortened = ensure_postable(caption, max_chars=240)
    if not ok:
        _log(skip_reason=safety_result, safety_check_result=safety_result, shortened=shortened)
        return

    review = review_tweet_with_openai(review_text, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")
    if not review.get("ok_to_post", False):
        _log(skip_reason=f"ai_review_ng:{review.get('reason','')}",
             safety_check_result=safety_result, shortened=shortened)
        return

    # #7 240字超（X重み付き）はスレッド投稿。News Botは短文優先だが、超えた場合の保険。
    from safety import build_x_thread_text, weighted_len as _wl
    use_thread = _wl(caption) > 240
    if use_thread:
        from x_client import post_tweet_thread_with_image
        parent_text, reply_texts = build_x_thread_text(caption)
        tweet_ids = post_tweet_thread_with_image(parent_text, image_path, reply_texts)
        tweet_id = tweet_ids[0] if tweet_ids else ""
        logger.info(
            "[NEWS-THREAD] use_thread=true | parent_tweet_id=%s | reply_tweet_ids=%s | "
            "final_caption_length=%s | each_post_length=%s",
            tweet_id or "-", ",".join(tweet_ids[1:]) or "-",
            len(caption), [len(parent_text)] + [len(r) for r in reply_texts],
        )
    else:
        tweet_id = post_tweet_with_image(caption, image_path)
        logger.info("[NEWS-THREAD] use_thread=false | parent_tweet_id=%s | "
                    "final_caption_length=%s", tweet_id or "-", len(caption))
    add_posted_entry(item, tweet_id=tweet_id, mode="image", impact=impact, text=caption)
    _log(skip_reason=("-" if tweet_id else "dry_run_not_posted"),
         safety_check_result=safety_result, shortened=shortened, tweet_id=tweet_id,
         dry_run=DRY_RUN, actual_post_attempted=_post_enabled_now())


# 通常ニュースBotは「高投稿価値だけ」方針。post_value>=7 かつ 米国株関連度>=8 のみ投稿。
IMPACT_SKIP_LEVEL = "low"  # 後方互換（未使用化）

# should_post を許可する market_scope（直接インパクト経路）
NEWS_BOT_ALLOWED_SCOPES = {"market_wide", "sector", "major_company"}


def _env_int(name: str, default: int) -> int:
    """.env から整数しきい値を読む（未設定・不正時は default）。"""
    v = os.environ.get(name, "").strip()
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def candidate_scan_limits() -> tuple[int, int]:
    """Return new-candidate assessment and ranked-pool scan limits."""
    assess_limit = max(1, min(25, _env_int("NEWS_MAX_CANDIDATES", 15)))
    configured_pool = max(1, _env_int("NEWS_CANDIDATE_POOL_SIZE", 75))
    scan_limit = max(assess_limit, min(150, configured_pool))
    return assess_limit, scan_limit


def failed_market_enrichment_is_veto(provider_isolated_editorial: bool) -> bool:
    """Only non-isolated publication sources require market evidence to pass."""
    return not bool(provider_isolated_editorial)


# しきい値は .env で調整可能（通過率チューニング用）。既定は通過率30%前後を狙った緩め設定。
try:
    from common.daily_post_goal import effective_int as _goal_effective_int
except ImportError:  # pragma: no cover
    from daily_post_goal import effective_int as _goal_effective_int

NEWS_BOT_RELEVANCE_THRESHOLD = _goal_effective_int("NEWS_RELEVANCE_THRESHOLD", 7)
# 話題性・ナラティブ経路のしきい値
def _post_enabled_now() -> bool:
    return os.environ.get("POST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


# 起動時に main() で更新（dry_run = not POST_ENABLED）
DRY_RUN = not _post_enabled_now()

# #6 汎用まとめ/ランキング記事の判定（1日1回まで）
_ROUNDUP_PAT = re.compile(
    r"(best|top\s*\d+|ranking|round\s*-?up|roundup|rates today|まとめ|ランキング|"
    r"best rates|today['’]s best|what to watch|things to know)",
    re.IGNORECASE,
)


def _is_roundup_title(title: str) -> bool:
    return bool(_ROUNDUP_PAT.search(title or ""))


def _roundup_posted_today() -> bool:
    """本日(JST)、まとめ/ランキング記事を既に投稿済みか。posted_history を見る。"""
    try:
        from posted_history import load_history
        today = datetime.now(JST).date().isoformat()
        for e in load_history():
            if str(e.get("posted_at", "")).startswith(today) and _is_roundup_title(e.get("title", "")):
                return True
    except Exception:
        pass
    return False


NEWS_BOT_BUZZ_THRESHOLD = _goal_effective_int("NEWS_BUZZ_THRESHOLD", 7)
NEWS_BOT_NARRATIVE_THRESHOLD = _goal_effective_int("NEWS_NARRATIVE_THRESHOLD", 7)
NEWS_BOT_THEME_THRESHOLD = _goal_effective_int("NEWS_THEME_THRESHOLD", 6)
# safety.py の既定(7)を .env で上書き（既定6=緩め）
NEWS_BOT_POST_VALUE_THRESHOLD = _goal_effective_int("NEWS_POST_VALUE_THRESHOLD", 6)

NEWS_IDLE_FALLBACK_HOURS = max(
    0, _goal_effective_int("NEWS_IDLE_FALLBACK_HOURS", 3)
)


def idle_fallback_allowed(impact: dict, title: str) -> bool:
    """3時間無投稿でも最低品質と話題性／一次情報重要度を満たす場合だけ許可。"""
    return (
        int(impact.get("post_value", 0)) >= 6
        and int(impact.get("us_equity_relevance", 0)) >= 5
        and bool(impact.get("has_independent_angle", False))
        and (float(impact.get("x_topic_acceleration", 0) or 0)
             >= float(os.getenv("X_TOPIC_ACCELERATION_MINIMUM", "1.25") or 1.25)
             or int(impact.get("primary_source_importance", 0)) >= 9)
        and not _is_roundup_title(title)
    )


def assess_market_impact(item: NewsItem) -> dict:
    """
    投稿候補ニュースを多軸で採点する。
      - post_value(投稿価値) / us_equity_relevance(直接の米国株関連度)
      - social_buzz_score(X話題性) / narrative_value(相場ナラティブ価値) / theme_relevance(テーマ接続度)
    source priority では投稿価値を底上げしない（公式ソースでも価値が低ければ落とす）。

    投稿条件（OR）:
      (A) 直接: post_value>=7 and us_equity_relevance>=8 and market_scope in 許可scope
      (B) 話題: post_value>=7 and social_buzz_score>=8 and narrative_value>=8
    → AI相場・半導体・IPO等のテーマは、指数/金利に直接効かなくても (B) で投稿可能。
    AI失敗時は should_post=False（フェイルクローズ）。
    """
    prompt = f"""あなたは米国株クラスタ（X/Twitter）向け金融SNSの編集者です。
次のニュースを、(A)市場への直接影響 と (B)Xで伸びやすい話題性・ナラティブ価値 の両面で採点してください。
ソースが公式（EIA/BEA/Fed等）でも、米国株クラスタへの価値が低ければ投稿しない方針です。
一方で、指数や金利に直接効かなくても、AI相場・半導体・IPO市場・大型テックへの連想で
X金融クラスタが話題化しやすいテーマは、投稿価値があります。

ニュースタイトル: {item.title}
ソース: {item.source}（種別: {getattr(item, 'source_group', 'market_news')}）

【post_value 1〜10（投稿する価値）】
- 10: 米国株市場全体を動かす最重要材料 / 8: 主要セクター・大型株に明確 / 7: 投稿価値あり / 6以下: 薄い

【us_equity_relevance 1〜10（米国株式市場との"直接"関連度）】
- 10: S&P500/NASDAQ/米金利/ドル/大型テックに直接影響 / 9: 半導体・AI・大型株・FRB・CPI/PCE/雇用 /
  8: 主要セクター・大型株に明確 / 7: 一部セクター限定 / 6以下: 業界・地域・個別性が強い

【social_buzz_score 1〜10（X金融クラスタで話題になりやすいか）】高くする例:
- OpenAI / Anthropic / xAI / DeepSeek / Meta AI などAI企業
- AIバブル、AI設備投資、データセンター、半導体需要
- Nvidia / Microsoft / Oracle / Broadcom / AMD / Micron などへの連想が強い話題
- IPO延期・IPO観測・未上場AI企業バリュエーション
- SOXL / 半導体ETF / NASDAQセンチメントに波及しやすい話題
- X上で議論・ミーム化しやすい金融テーマ

【narrative_value 1〜10（"市場の物語"として語りやすいか）】高くする例:
- AI相場の持続性 / 半導体サイクル / データセンター投資 / IPO市場の過熱・冷却 /
  バリュエーション不安 / 規制・輸出管理・地政学 / 大型テーマ株への連想

【theme_relevance 1〜10（金融クラスタの関心テーマ＝AI/半導体/大型テック/IPO/金利/規制等への接続度）】

【market_scope（いずれか1つ）】
"market_wide" / "sector" / "major_company" / "ai_theme" / "semis_theme" /
"ipo_theme" / "single_name" / "niche_energy" / "none"

【話題性で通す場合の品質条件（すべて満たすときだけ buzz/narrative を高くする）】
- AI/半導体/大型テック/IPO/金利/規制など、金融クラスタの関心テーマに接続できる
- 投稿にすると「で？」ではなく、相場ナラティブとして読める
- 読者がリポスト・引用したくなる論点がある
- 投資助言ではなく、テーマ整理として投稿できる
単なる有名企業の人事・製品・提携など、相場ナラティブに接続できないものは narrative_value を低くすること。

【原則スキップ】地域電力市場 / NY ISO・PJM・ERCOT / 小規模太陽光・電力需要・設備容量 /
米国株指数・大型株・金利・ドル・原油・インフレに波及しにくいEIA記事 / 業界関係者向けで関心が低いもの。
EIAで投稿可は「原油/ガソリン/天然ガス在庫・WTI/Brent需給・OPEC+・エネルギー株/インフレ/金利波及・XOM/CVX/SLB関連」に限定。

以下のJSONのみ返す（説明文・Markdown禁止）。
{{
  "post_value": 1〜10の整数,
  "us_equity_relevance": 1〜10の整数,
  "social_buzz_score": 1〜10の整数,
  "narrative_value": 1〜10の整数,
  "theme_relevance": 1〜10の整数,
  "market_scope": "上記のいずれか",
  "post_type": "breaking_news / misconception / second_order_effect / comparison / scheduled_summary のいずれか",
  "has_independent_angle": true または false,
  "primary_source_importance": 1〜10の整数,
  "reason": "日本語1文で理由",
  "skip_reason": "スキップする場合の理由（投稿可なら空文字）"
}}"""
    try:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model=model_for(OpenAIRole.CLASSIFY),
            openai_role="classify",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
            reasoning_effort="minimal",
        )
        data = json.loads(resp.choices[0].message.content or "{}")

        def _i(key):
            try:
                return int(data.get(key, 0))
            except Exception:
                return 0
        pv = _i("post_value")
        rel = _i("us_equity_relevance")
        buzz = _i("social_buzz_score")
        narr = _i("narrative_value")
        theme = _i("theme_relevance")
        scope = str(data.get("market_scope", "none"))
        data["post_type"] = str(data.get("post_type", "breaking_news"))
        data["has_independent_angle"] = bool(data.get("has_independent_angle", False))
        data["primary_source_importance"] = _i("primary_source_importance")

        # #6 通常Botの投稿条件:
        # post_value>=7 かつ (直接関連度>=8 or 話題性+テーマ or ナラティブ+テーマ)
        direct_ok = rel >= NEWS_BOT_RELEVANCE_THRESHOLD
        buzz_ok = (buzz >= NEWS_BOT_BUZZ_THRESHOLD and theme >= NEWS_BOT_THEME_THRESHOLD)
        narr_ok = (narr >= NEWS_BOT_NARRATIVE_THRESHOLD and theme >= NEWS_BOT_THEME_THRESHOLD)
        should = (pv >= NEWS_BOT_POST_VALUE_THRESHOLD) and (direct_ok or buzz_ok or narr_ok)

        if should:
            skip_reason = ""
        elif pv < NEWS_BOT_POST_VALUE_THRESHOLD:
            skip_reason = f"post_value<{NEWS_BOT_POST_VALUE_THRESHOLD}"
        else:
            skip_reason = (
                f"gate未通過(rel{rel}/buzz{buzz}/narr{narr}/theme{theme})"
            )

        data.update({
            "post_value": pv, "us_equity_relevance": rel,
            "social_buzz_score": buzz, "narrative_value": narr,
            "theme_relevance": theme, "market_scope": scope,
            "should_post": should, "skip_reason": skip_reason,
            "pass_path": ("direct" if direct_ok else ("buzz" if buzz_ok else ("narrative" if narr_ok else "-"))),
        })
        return data
    except Exception as e:
        logger.warning(f"インパクト判定API失敗、高価値方針によりスキップ（フェイルクローズ）: {e}")
        return {"post_value": 0, "us_equity_relevance": 0, "social_buzz_score": 0,
                "narrative_value": 0, "theme_relevance": 0, "market_scope": "unknown",
                "reason": f"判定不能: {e}", "should_post": False,
                "skip_reason": "assess_failed", "pass_path": "-"}


def main(mode: str = "image") -> None:
    logger.info(f"mode: {mode}")
    global DRY_RUN
    DRY_RUN = not _post_enabled_now()
    logger.info(
        f"POST_ENABLED={str(_post_enabled_now()).lower()} / dry_run={str(DRY_RUN).lower()}"
        + ("（dry-run: 投稿判定は行うが実投稿はしません）" if DRY_RUN else "")
    )
    # 通常ニュース要約は格下げ。リンクは付けず（ソース名のみ）、高価値だけ投稿。
    if mode == "link":
        logger.info("linkモードは廃止（ソース名のみ・URLなし方針）。no-linkに切替。")
        mode = "normal"

    posted_urls = get_posted_urls()
    logger.info(f"投稿済みURL数: {len(posted_urls)}")

    # 候補を順番に評価し、最初に基準を通ったニュースを投稿対象にする。
    # 評価済み・低価値の候補で、その回全体を終了しない。
    from news import fetch_news_candidates
    from posted_history import recently_evaluated, record_evaluated

    max_candidates, candidate_pool_size = candidate_scan_limits()

    candidates = fetch_news_candidates(
        posted_urls=posted_urls,
        limit=candidate_pool_size,
    )
    if not candidates:
        logger.error("ニュース取得失敗")
        _news_log(selected_post_type=mode, skip_reason="no_news", should_post=False)
        return

    item = None
    impact = None
    pv = rel = buzz = narr = theme = 0
    scope = "-"
    checked_count = 0
    assessed_count = 0
    fallback = None

    try:
        from xai_radar import load_cache
        radar_topics = load_cache()
    except Exception:
        radar_topics = []
    if radar_topics:
        try:
            from common.xai_integration import prioritize_candidates
            candidates = prioritize_candidates(candidates, radar_topics)
        except Exception:
            logger.warning("xAI話題による候補優先順位付けに失敗（元の順序を維持）")

    for rank, cand in enumerate(candidates, start=1):
        checked_count += 1
        logger.info(
            f"候補評価 {rank}/{len(candidates)}: "
            f"[{cand.source}] {cand.title[:100]}"
        )

        ev_url_hit, ev_title_hit = recently_evaluated(cand.url, cand.title)
        if ev_url_hit or ev_title_hit:
            reason = "evaluated_recently_url" if ev_url_hit else "evaluated_recently_title"
            logger.info(f"評価済みのため次候補へ: {reason} :: {cand.title[:50]}")
            _news_log(
                selected_news_title=cand.title, source=cand.source,
                selected_post_type=mode, should_post=False, skip_reason=reason,
                evaluated_history_hit=True, duplicate_title_hit=ev_title_hit,
                dry_run=DRY_RUN, actual_post_attempted=False,
            )
            continue

        if assessed_count >= max_candidates:
            logger.info(
                "新規候補の評価上限に到達: assessed=%s scanned=%s pool=%s",
                assessed_count, checked_count, len(candidates),
            )
            break
        assessed_count += 1

        cand_impact = assess_market_impact(cand)
        cand_pv = cand_impact.get("post_value", 0)
        cand_rel = cand_impact.get("us_equity_relevance", 0)
        cand_buzz = cand_impact.get("social_buzz_score", 0)
        cand_narr = cand_impact.get("narrative_value", 0)
        cand_theme = cand_impact.get("theme_relevance", 0)
        cand_scope = cand_impact.get("market_scope", "-")
        cand_should = cand_impact.get("should_post", False)
        try:
            from common.data_governance import provider_isolated_editorial_decision
            editorial_decision = provider_isolated_editorial_decision(
                source_url=cand.url,
                source_group=getattr(cand, "source_group", ""),
                provider_lineage=[],
            )
        except Exception:
            editorial_decision = {
                "allowed": False, "reason": "editorial_governance_unavailable"
            }
        editorial_text = f"{cand.title} {cand.source}".lower()
        editorial_topic = (
            "fx_official_context"
            if any(word in editorial_text for word in (
                "currency", "foreign exchange", "dollar", "yen", "為替", "ドル", "円"
            ))
            else "market_official_context"
        )
        cand_impact["provider_isolated_editorial"] = bool(
            editorial_decision.get("allowed")
        )
        cand_impact["provider_isolated_reason"] = editorial_decision.get("reason")
        cand_impact["provider_isolated_source_host"] = editorial_decision.get(
            "source_host"
        )
        cand_impact["provider_isolated_topic"] = editorial_topic
        cand_impact["market_data_provider_lineage"] = []
        cand_impact["twelvedata_used_for_post"] = False
        cand_impact["live_price_used"] = False
        cand_impact["provider_chart_used"] = False

        try:
            from market_data.editorial_bridge import match_candidate
            from common.data_governance import independent_confirmation_decision
            from market_data.evidence_flow import (
                evaluate_candidate,
                get_trigger,
                record_suppression,
            )
            independent_match = match_candidate(cand)
            trigger_evidence = (
                get_trigger(str(independent_match.get("trigger_id") or ""))
                if independent_match else None
            )
            independent_decision = (
                independent_confirmation_decision(
                    source_url=cand.url,
                    source_group=getattr(cand, "source_group", ""),
                    publication_provider_lineage=[],
                    internal_trigger_providers=[
                        str(independent_match.get("internal_trigger_provider") or "")
                    ],
                    includes_trigger_values=False,
                    includes_trigger_chart=False,
                )
                if independent_match
                else {"allowed": False, "reason": "no_internal_trigger_match"}
            )
            evidence_result = (
                evaluate_candidate(cand, trigger_evidence)
                if independent_decision.get("allowed") and trigger_evidence
                else None
            )
            public_bundle = (
                evidence_result.get("bundle") if evidence_result else None
            )
            evidence_allowed = bool(
                public_bundle
                and public_bundle.get("validation", {}).get("allowed")
            )
            if independent_match and not evidence_allowed:
                record_suppression(
                    str(independent_match.get("trigger_id") or ""),
                    reason=(
                        public_bundle.get("validation", {}).get("reason")
                        if public_bundle else
                        independent_decision.get("reason")
                    ),
                    confidence=(
                        evidence_result.get("causal", {}).get(
                            "causal_confidence", "unknown"
                        )
                        if evidence_result else "unknown"
                    ),
                    mode=(
                        public_bundle.get("content_mode", "unknown_cause")
                        if public_bundle else "unknown_cause"
                    ),
                )
        except Exception as exc:
            independent_match = None
            trigger_evidence = None
            evidence_result = None
            public_bundle = None
            evidence_allowed = False
            independent_decision = {
                "allowed": False,
                "reason": f"independent_confirmation_unavailable:{type(exc).__name__}",
            }
        cand_impact["independent_confirmation"] = bool(
            independent_match
            and independent_decision.get("allowed")
            and evidence_allowed
        )
        cand_impact["independent_confirmation_decision"] = independent_decision
        cand_impact["public_evidence_bundle"] = public_bundle
        if independent_match and not evidence_allowed:
            evidence_reason = (
                "market_trigger_evidence_blocked:"
                + str(
                    (public_bundle or {}).get("validation", {}).get("reason")
                    or independent_decision.get("reason")
                    or "causal_confidence_insufficient"
                )
            )
            cand_impact["independent_confirmation_suppressed"] = True
            cand_impact["independent_confirmation_suppression_reason"] = evidence_reason
            # Internal market data is optional enrichment for an independently
            # publishable RSS/article source.  Failed enrichment must not veto
            # that source's editorial assessment.  Non-isolated sources remain
            # fail-closed and still require a valid public evidence bundle.
            if failed_market_enrichment_is_veto(
                cand_impact["provider_isolated_editorial"]
            ):
                cand_should = False
                cand_impact["should_post"] = False
                cand_impact["skip_reason"] = evidence_reason
        if cand_impact["independent_confirmation"]:
            cand_impact.update({
                "internal_market_trigger_id": independent_match.get("trigger_id"),
                "internal_market_trigger_symbol": independent_match.get("symbol"),
                "internal_trigger_provider_lineage": [
                    independent_match.get("internal_trigger_provider")
                ],
                "independent_source_title": independent_match.get(
                    "independent_source_title"
                ),
                "independent_source_url": independent_match.get(
                    "independent_source_url"
                ),
                "independent_source_name": independent_match.get(
                    "independent_source_name"
                ),
                "market_data_provider_lineage": [],
                "twelvedata_internal_trigger": (
                    independent_match.get("internal_trigger_provider")
                    == "twelvedata"
                ),
                "twelvedata_used_for_post": False,
                "live_price_used": False,
                "provider_chart_used": False,
                "causal_confidence": evidence_result.get("causal", {}).get(
                    "causal_confidence"
                ),
                "causal_claim_allowed": evidence_result.get("causal", {}).get(
                    "causal_claim_allowed", False
                ),
                "publication_mode": public_bundle.get("content_mode"),
                "public_evidence_bundle_id": public_bundle.get("bundle_id"),
                "publication_candidate_id": public_bundle.get("candidate_id"),
                "publication_evidence_ids": public_bundle.get("evidence_ids", []),
            })

        try:
            from common.xai_social_intelligence import (
                cost_attribution, match_integrated_analysis, match_news_event,
                record_content_opportunity_use, record_integrated_analysis_use,
                shadow_record_news,
            )
            social_match = match_news_event(
                title=cand.title,
                url=cand.url,
                tickers=cand_impact.get("tickers") or (),
                event_type=str(cand_impact.get("event_type") or ""),
                published_at=str(getattr(cand, "published", "") or ""),
            )
            integrated_match = match_integrated_analysis(
                title=cand.title,
                tickers=cand_impact.get("tickers") or (),
                event_id=str((social_match or {}).get("candidate_id") or ""),
            )
        except Exception:
            social_match=None
            integrated_match=None
        try:
            from common.xai_integration import match_topic
            legacy_match=match_topic(cand.title,radar_topics)
        except Exception:
            legacy_match=None
        observation=(social_match or {}).get("observation") or {}
        social_metrics=observation.get("metrics") or {}
        social_delta=observation.get("delta") or {}
        interpretation=observation.get("interpretation") or {}
        matched=social_match or legacy_match
        cand_impact["x_topic_velocity"] = float(
            social_delta.get("observed_velocity_score")
            or (legacy_match or {}).get("velocity_score",0) or 0)
        cand_impact["x_topic_acceleration"] = float(
            social_delta.get("observed_acceleration_score")
            or (legacy_match or {}).get("acceleration_score",0) or 0)
        cand_impact["news_confirmation_status"] = "rss_corroborated" if matched else "not_radar_sourced"
        cand_impact["radar_influenced"] = False
        cand_impact["xai_researched"] = bool(social_match)
        cand_impact["xai_signal_used"] = bool(matched)
        cand_impact["xai_signal_reason"] = "event_match_shadow_only" if social_match else ("legacy_observed_match" if legacy_match else "no_match")
        cand_impact["xai_priority_applied"] = False
        cand_impact["radar_run_id"] = observation.get("run_id") or (legacy_match or {}).get("radar_run_id")
        cand_impact["xai_run_id"] = observation.get("run_id")
        cand_impact["xai_event_id"] = (social_match or {}).get("candidate_id")
        cand_impact["radar_topic"] = (social_match or {}).get("canonical_topic") or (legacy_match or {}).get("topic")
        cand_impact["xai_cost_attribution_usd"] = (
            cost_attribution(str(observation.get("run_id") or ""))
            if social_match else
            (legacy_match or {}).get("xai_cost_attribution_usd",0)
        )
        cand_impact["observed_velocity_score"] = social_delta.get("observed_velocity_score") or (legacy_match or {}).get("velocity_score")
        cand_impact["observed_acceleration_score"] = social_delta.get("observed_acceleration_score") or (legacy_match or {}).get("acceleration_score")
        cand_impact["unique_accounts"] = social_metrics.get("unique_accounts")
        cand_impact["independent_commentary_count"] = social_metrics.get("independent_commentary_count")
        cand_impact["dominant_narrative"] = interpretation.get("dominant_narrative")
        cand_impact["dissent_present"] = bool(interpretation.get("strongest_dissent"))
        cand_impact["misconception_present"] = bool(interpretation.get("common_misconception"))
        cand_impact["official_participation"] = social_metrics.get("official_account_participation")
        cand_impact["xai_confidence"] = interpretation.get("confidence")
        cand_impact["source_confirmation"] = (legacy_match or {}).get("source_confirmation")
        cand_impact["xai_integrated_analysis_id"] = (
            integrated_match or {}
        ).get("analysis_id")
        cand_impact["xai_integrated_summary"] = (
            integrated_match or {}
        ).get("integrated_summary")
        cand_impact["xai_integrated_evidence_quality"] = (
            (integrated_match or {}).get("evidence") or {}
        ).get("quality")
        cand_impact["xai_integrated_posting_readiness"] = (
            integrated_match or {}
        ).get("posting_readiness")
        cand_impact["xai_integrated_requires_confirmation"] = bool(
            (integrated_match or {}).get("facts_needing_confirmation")
            or (integrated_match or {}).get("potentially_false_claims")
        )
        cand_impact["xai_integrated_context_used"] = bool(integrated_match)
        cand_impact["xai_integrated_priority_applied"] = False
        if social_match:
            try:
                shadow_record_news(
                    title=cand.title, matched=social_match,
                    original_rank=rank, hypothetical_rank=max(1,rank-1),
                )
                record_content_opportunity_use(
                    run_id=str(observation.get("run_id") or ""),
                    event_id=str((social_match or {}).get("candidate_id") or ""),
                    use_type="news_candidate_match",
                    reference_id=f"{cand.url}|{cand.title}",
                )
            except Exception:
                logger.warning("xAI shadow順位記録に失敗（選定は継続）")
        if integrated_match:
            try:
                record_integrated_analysis_use(
                    analysis_id=str(integrated_match.get("analysis_id") or ""),
                    use_type="news_candidate_context",
                    reference_id=f"{cand.url}|{cand.title}",
                )
            except Exception:
                logger.warning("xAI統合分析の利用記録に失敗（選定は継続）")

        try:
            duplicate = semantic_duplicate(cand.title, title=cand.title, source_url=cand.url)
        except Exception as exc:
            logger.warning("意味的重複判定に失敗したため安全側で候補を保留: %s", type(exc).__name__)
            record_evaluated(cand.url, cand.title, skip_reason="embedding_unavailable", should_post=False)
            continue
        if duplicate["status"] == "block":
            logger.info("意味的重複のため候補除外: similarity=%.3f", duplicate["similarity"])
            record_evaluated(cand.url, cand.title, skip_reason="semantic_duplicate", should_post=False)
            continue
        if duplicate["status"] == "warn":
            logger.warning("類似投稿警告（別論点として審査継続）: similarity=%.3f", duplicate["similarity"])
        if matched:
            try:
                from common.xai_integration import record_downstream_event
                record_downstream_event(
                    str(observation.get("run_id") or matched.get("radar_run_id") or ""),
                    "news_candidate",
                    candidate_id=f"{cand.url}|{cand.title}",
                )
            except Exception:
                logger.warning("xAI候補化イベントの記録に失敗（選定は継続）")

        # 通常ゲートを落ちた候補も、長時間無投稿時に備えて最良1件だけ保持する。
        # AI評価そのものが失敗した候補は、品質を判断できないため対象外。
        if (not cand_should and cand_impact.get("skip_reason") != "assess_failed"):
            fallback_score = (cand_pv, max(cand_rel, cand_buzz, cand_narr), cand_theme)
            if fallback is None or fallback_score > fallback[0]:
                fallback = (fallback_score, cand, cand_impact, rank)

        if not cand_should:
            skip_reason = cand_impact.get("skip_reason") or "low_value"
            record_evaluated(
                cand.url, cand.title,
                skip_reason=skip_reason, should_post=False,
            )
            _news_log(
                selected_news_title=cand.title, source=cand.source,
                selected_post_type=mode, post_value=cand_pv,
                us_equity_relevance=cand_rel, social_buzz_score=cand_buzz,
                narrative_value=cand_narr, theme_relevance=cand_theme,
                market_scope=cand_scope, should_post=False,
                skip_reason=skip_reason, dry_run=DRY_RUN,
                actual_post_attempted=False, evaluated_history_hit=False,
                duplicate_title_hit=False,
            )
            logger.info(
                f"候補{rank}は基準未満。次候補へ: "
                f"{cand_impact.get('reason', '')}"
            )
            continue

        record_evaluated(cand.url, cand.title, skip_reason="", should_post=True)
        item = cand
        impact = cand_impact
        pv = cand_pv
        rel = cand_rel
        buzz = cand_buzz
        narr = cand_narr
        theme = cand_theme
        scope = cand_scope
        logger.info(
            f"投稿対象を選択: rank={rank}/{len(candidates)} / "
            f"経路={cand_impact.get('pass_path', '-')} / {cand.title}"
        )
        break

    if item is None and fallback is not None and posting_inactive(NEWS_IDLE_FALLBACK_HOURS):
        _, fallback_item, fallback_impact, rank = fallback
        fallback_ok = idle_fallback_allowed(fallback_impact, fallback_item.title)
        if fallback_ok:
            item, impact = fallback_item, fallback_impact
        else:
            logger.info(
                "%s時間フォールバック最低条件未達のため正常スキップ",
                NEWS_IDLE_FALLBACK_HOURS,
            )
    if item is not None and impact is not None and not impact.get("should_post",False):
        impact = {**impact, "should_post": True, "skip_reason": "",
                  "pass_path": "idle_fallback"}
        pv = impact.get("post_value", 0)
        rel = impact.get("us_equity_relevance", 0)
        buzz = impact.get("social_buzz_score", 0)
        narr = impact.get("narrative_value", 0)
        theme = impact.get("theme_relevance", 0)
        scope = impact.get("market_scope", "-")
        elapsed = hours_since_last_post()
        record_evaluated(item.url, item.title, skip_reason="", should_post=True)
        logger.warning(
            "無投稿フォールバックを適用: elapsed_hours=%s / threshold=%s / "
            "rank=%s / post_value=%s / title=%s",
            "history_none" if elapsed is None else f"{elapsed:.2f}",
            NEWS_IDLE_FALLBACK_HOURS, rank, pv, item.title,
        )

    if item is None or impact is None:
        logger.info(
            f"候補{checked_count}件を走査・新規{assessed_count}件を評価したが、"
            "投稿基準を通るニュースなし"
        )
        return

    logger.info(f"取得ニュース: {item.title}")
    logger.info(f"ソース: {item.source}")

    def _gate_log(**kw):
        _news_log(
            selected_news_title=item.title, source=item.source,
            selected_post_type=mode, post_value=pv,
            us_equity_relevance=rel, social_buzz_score=buzz,
            narrative_value=narr, theme_relevance=theme,
            market_scope=scope, **kw,
        )

    logger.info(
        f"投稿可（経路={impact.get('pass_path', '-')}）: "
        f"{impact.get('reason', '')}"
    )

    # #6 汎用まとめ/ランキング記事は1日1回まで
    if _is_roundup_title(item.title) and _roundup_posted_today():
        _gate_log(should_post=False, skip_reason="roundup_daily_cap",
                  dry_run=DRY_RUN, actual_post_attempted=False)
        logger.info("まとめ/ランキング記事は本日投稿済みのためスキップ（1日1回まで）")
        return

    if DRY_RUN:
        logger.info("[INFO] should_post=true だが POST_ENABLED=false のため dry-run（未投稿）")

    window=posting_window(float(impact.get("x_topic_acceleration",0) or 0))
    if not window["allow"]:
        _gate_log(should_post=False,skip_reason=f"dynamic_gap:{window['required_gap_minutes']}min",dry_run=DRY_RUN,actual_post_attempted=False)
        logger.info("動的投稿間隔により正常スキップ: %s",window)
        return

    try:
        from post_registry import _load_history
        recent_styles=[r.get("post_type","") for r in _load_history()[-5:]]
    except Exception: recent_styles=[]
    selected_style=choose_style(suggested=impact.get("post_type",""),recent_styles=recent_styles)
    impact.update({"post_type":selected_style,"experiment_variant":selected_style,
                   "experiment_hypothesis":"投稿タイプ別に話題速度補正後の24h実績を比較",
                   "source_type":getattr(item,"source_group","market_news"),"opinion_strength":"moderate"})
    if impact.get("provider_isolated_editorial") and mode in ("image", "diagram"):
        logger.info("公式ソース隔離投稿では市場データ風チャートを使わず通常文章へ変更")
        mode = "normal"
    if impact.get("independent_confirmation") and mode in ("image", "diagram"):
        logger.info(
            "独立確認投稿では内部市場データ由来の図表混入を防ぐため通常文章へ変更"
        )
        mode = "normal"

    if mode in ("image", "diagram"):
        diagram_judgement = assess_diagram_value(
            item, get_openai_client(), model_for(OpenAIRole.CLASSIFY),
        )
        logger.info(
            "図解価値=%s/10 / clear=%s / structure=%s / facts=%s / numeric=%s / "
            "should_diagram=%s / reason=%s",
            diagram_judgement.get("score"),
            diagram_judgement.get("has_clear_structure"),
            diagram_judgement.get("structure_type"),
            diagram_judgement.get("fact_count"),
            diagram_judgement.get("numeric_fact_count"),
            diagram_judgement.get("should_diagram"),
            diagram_judgement.get("reason", ""),
        )
        if not diagram_judgement.get("should_diagram", False):
            logger.info("図解価値が基準未達のため、通常文章へ自動変更")
            mode = "normal"
        else:
            # Explicit `diagram` and automatic `image` use the same PNG
            # generation/upload path after the value gate passes.
            mode = "image"
            impact.update({
                "diagram_value_score": diagram_judgement.get("score"),
                "diagram_structure_type": diagram_judgement.get("structure_type"),
                "diagram_reason": diagram_judgement.get("reason"),
                "diagram_png_attached": True,
            })

    if mode == "image":
        handle_image_post(item, impact=impact)
        return

    if impact.get("independent_confirmation"):
        movement_id = str(impact.get("internal_market_trigger_id") or "")
        try:
            from market_data.evidence_flow import (
                generate_structured_publication,
                record_publication_result,
                record_suppression,
            )
        except Exception as exc:
            _gate_log(
                should_post=True,
                skip_reason=(
                    "structured_evidence_gate_unavailable:"
                    f"{type(exc).__name__}"
                ),
                dry_run=DRY_RUN,
                actual_post_attempted=False,
            )
            logger.exception(
                "Market-triggered publication stopped because the evidence "
                "gate could not be loaded"
            )
            return
        try:
            structured_publication = generate_structured_publication(
                impact.get("public_evidence_bundle") or {}
            )
        except Exception as exc:
            structured_publication = {
                "status": "rejected",
                "rejection_reason": (
                    f"structured_generation_failed:{type(exc).__name__}"
                ),
            }
        impact["structured_publication_status"] = structured_publication.get(
            "status"
        )
        impact["structured_output_validation"] = structured_publication.get(
            "validation"
        )
        impact["structured_post_value"] = structured_publication.get(
            "post_value"
        )
        minimum_value = int(os.getenv("MARKET_TRIGGER_MIN_POST_VALUE", "6"))
        structured_value = int(structured_publication.get("post_value") or 0)
        if (
            structured_publication.get("status") != "ready"
            or structured_value < minimum_value
        ):
            stop_reason = (
                structured_publication.get("rejection_reason")
                or structured_publication.get("validation", {}).get("reason")
                or "post_value_below_threshold"
            )
            record_suppression(
                movement_id,
                reason=f"structured_publication_blocked:{stop_reason}",
                confidence=str(impact.get("causal_confidence") or "unknown"),
                mode=str(impact.get("publication_mode") or "unknown_cause"),
            )
            record_publication_result(
                movement_id,
                posted=False,
                mode=str(impact.get("publication_mode") or "unknown_cause"),
                reason=str(stop_reason),
                post_value=structured_value,
            )
            _gate_log(
                should_post=True,
                skip_reason=f"structured_publication_blocked:{stop_reason}",
                dry_run=DRY_RUN,
                actual_post_attempted=False,
            )
            logger.warning(
                "Market-triggered publication stopped by structured evidence gate: %s",
                stop_reason,
            )
            return
        tweet = str(structured_publication.get("draft_text") or "").strip()
        impact["post_value"] = structured_value
        impact["structured_recommended_mode"] = structured_publication.get(
            "recommended_mode"
        )
        impact["structured_claims"] = structured_publication.get("claims", [])
    else:
        tweet = create_tweet(
            mode, item, style=selected_style, research_context=impact
        )

    def _record_independent_stop(reason: str) -> None:
        if not impact.get("independent_confirmation"):
            return
        try:
            from market_data.evidence_flow import (
                record_publication_result,
                record_suppression,
            )
            movement_id = str(
                impact.get("internal_market_trigger_id") or ""
            )
            publication_mode = str(
                impact.get("publication_mode") or "unknown_cause"
            )
            record_suppression(
                movement_id,
                reason=reason,
                confidence=str(
                    impact.get("causal_confidence") or "unknown"
                ),
                mode=publication_mode,
            )
            record_publication_result(
                movement_id,
                posted=False,
                mode=publication_mode,
                reason=reason,
                post_value=int(impact.get("post_value") or 0),
            )
        except Exception:
            logger.exception(
                "Market-trigger stop metric recording failed safely"
            )

    if impact.get("provider_isolated_editorial"):
        try:
            from common.data_governance import (
                validate_provider_isolated_editorial_text,
            )
            editorial_text_gate = validate_provider_isolated_editorial_text(
                tweet, source_title=item.title,
            )
        except Exception as exc:
            editorial_text_gate = {
                "allowed": False,
                "reason": f"editorial_text_gate_unavailable:{type(exc).__name__}",
            }
        impact["provider_isolated_text_gate"] = editorial_text_gate
        if not editorial_text_gate.get("allowed"):
            _record_independent_stop(
                "official_editorial_blocked:"
                f"{editorial_text_gate.get('reason')}"
            )
            _gate_log(
                should_post=True,
                skip_reason=f"official_editorial_blocked:{editorial_text_gate.get('reason')}",
                dry_run=DRY_RUN,
                actual_post_attempted=False,
            )
            logger.warning(
                "公式ソース隔離投稿を安全停止: %s", editorial_text_gate
            )
            return
    if impact.get("independent_confirmation"):
        try:
            from common.data_governance import (
                validate_provider_isolated_editorial_text,
            )
            independent_text_gate = validate_provider_isolated_editorial_text(
                tweet,
                source_title=item.title,
            )
        except Exception as exc:
            independent_text_gate = {
                "allowed": False,
                "reason": f"independent_text_gate_unavailable:{type(exc).__name__}",
            }
        impact["independent_confirmation_text_gate"] = independent_text_gate
        if not independent_text_gate.get("allowed"):
            _record_independent_stop(
                "independent_confirmation_blocked:"
                f"{independent_text_gate.get('reason')}"
            )
            _gate_log(
                should_post=True,
                skip_reason=(
                    "independent_confirmation_blocked:"
                    f"{independent_text_gate.get('reason')}"
                ),
                dry_run=DRY_RUN,
                actual_post_attempted=False,
            )
            logger.warning(
                "独立確認投稿を安全停止: %s",
                independent_text_gate,
            )
            return
        tweet = append_source_attribution(tweet, item.source)

    # 文字数オーバーは即スキップせず短縮を試す。NG/空はスキップ。
    ok, tweet, safety_result, shortened = ensure_postable(tweet, max_chars=240)
    if not ok:
        _record_independent_stop(f"safety_check:{safety_result}")
        _gate_log(should_post=True, skip_reason=safety_result,
                  safety_check_result=safety_result, shortened=shortened)
        logger.info(f"safety未通過のためスキップ: {safety_result}\n{tweet}")
        return

    review = review_tweet_with_openai(tweet, item.title, item.source)
    logger.info(f"レビュー結果: {json.dumps(review, ensure_ascii=False)}")
    if not review.get("ok_to_post", False):
        _record_independent_stop(
            f"ai_review_ng:{review.get('reason', '')}"
        )
        _gate_log(should_post=True, skip_reason=f"ai_review_ng:{review.get('reason','')}",
                  safety_check_result=safety_result, shortened=shortened)
        return

    tweet_id = post_tweet(tweet)
    add_posted_entry(item, tweet_id=tweet_id, mode=mode, impact=impact, text=tweet)
    if impact.get("independent_confirmation"):
        try:
            from market_data.evidence_flow import (
                get_trigger,
                record_publication_result,
            )
            trigger = get_trigger(
                str(impact.get("internal_market_trigger_id") or "")
            ) or {}
            detected_at = datetime.fromisoformat(
                str(trigger.get("detected_at") or "").replace("Z", "+00:00")
            )
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=timezone.utc)
            elapsed = max(
                0.0,
                (datetime.now(timezone.utc) - detected_at.astimezone(timezone.utc))
                .total_seconds(),
            )
            record_publication_result(
                str(impact.get("internal_market_trigger_id") or ""),
                posted=bool(tweet_id),
                mode=str(impact.get("publication_mode") or "unknown"),
                reason="posted" if tweet_id else "dry_run_not_posted",
                detection_to_post_seconds=elapsed,
                post_value=int(impact.get("post_value") or 0),
            )
        except Exception:
            logger.exception("Market-trigger publication metric recording failed safely")
    _gate_log(should_post=True, skip_reason=("-" if tweet_id else "dry_run_not_posted"),
              safety_check_result=safety_result, shortened=shortened, tweet_id=tweet_id,
              dry_run=DRY_RUN, actual_post_attempted=_post_enabled_now())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "image"
    main(mode)
