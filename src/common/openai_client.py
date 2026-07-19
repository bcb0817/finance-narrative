"""
common/openai_client.py
OpenAI関連の共通処理（クライアント生成・本文生成・レビュー）。
"""

import os
import json
import logging

from openai import OpenAI

try:
    from safety import clean_text
except ImportError:  # pragma: no cover
    from common.safety import clean_text
try:
    from api_costs import ensure_openai_budget, record_openai_usage
except ImportError:  # pragma: no cover
    from common.api_costs import ensure_openai_budget, record_openai_usage

logger = logging.getLogger(__name__)

try:
    from performance_learning import with_performance_learning
except ImportError:  # pragma: no cover
    from common.performance_learning import with_performance_learning


OPENAI_GENERATE_MODEL = os.getenv("OPENAI_GENERATE_MODEL", "gpt-5-mini")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5-nano")


class _BudgetedCompletions:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create(self, *args, **kwargs):
        ensure_openai_budget()
        response = self._wrapped.create(*args, **kwargs)
        record_openai_usage(response, str(kwargs.get("model", "unknown")))
        return response


class _ChatProxy:
    def __init__(self, wrapped):
        self.completions = _BudgetedCompletions(wrapped.completions)


class _BudgetedOpenAI:
    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.chat = _ChatProxy(wrapped.chat)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def get_openai_client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("環境変数が未設定です: OPENAI_API_KEY")
    return _BudgetedOpenAI(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))


def generate_by_openai(prompt: str, max_tokens: int = 2000) -> str:
    client = get_openai_client()
    prompt = with_performance_learning(prompt)
    response = client.chat.completions.create(
        model=OPENAI_GENERATE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=max_tokens,
        reasoning_effort="minimal",
    )
    text = response.choices[0].message.content or ""
    return clean_text(text)


def shorten_tweet_with_openai(text: str, max_chars: int = 240) -> str:
    """280字超の投稿を 180〜max_chars 字に短縮リライトする。
    意味と重要な数字は保持。新しい事実・URL・ハッシュタグ・絵文字は追加しない。
    投資助言・売買推奨にしない。失敗時は元テキストを返す（呼び出し側で再チェック）。
    """
    try:
        client = get_openai_client()
        prompt = (
            f"次のX投稿を、意味と重要な数字を保ったまま日本語で{max_chars}字以内"
            f"（目安180〜{max_chars}字）に短縮してください。\n"
            "- 新しい事実・数字・URL・ハッシュタグ・絵文字を追加しない\n"
            "- 投資助言・売買推奨・断定にしない\n"
            "- 改行は最小限、投稿本文のみ返す\n\n"
            f"本文:\n{text}"
        )
        response = client.chat.completions.create(
            model=OPENAI_GENERATE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
            reasoning_effort="minimal",
        )
        out = clean_text(response.choices[0].message.content or "")
        return out or text
    except Exception as e:
        logger.warning(f"短縮リライト失敗、元テキストを使用: {e}")
        return text


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

【審査基準（金融Bot向け・厳格に）】
- 投資助言・売買推奨になっていないか（「買え」「売れ」「買うべき」「売るべき」等は禁止）
- ニュースにない数字や事実を捏造していないか
- 株価・時価総額・金利・為替などの水準を、取得データの裏づけなく断定していないか
- 価格予測を断定していないか（「〜見られやすい」「〜意識されやすい」等の表現ならOK）
- 誤解を招く内容・過度な煽りでないか

以下のJSON形式のみで返答してください。説明文は不要です。
{{
  "ok_to_post": true,
  "risk_level": "low",
  "reason": "投稿してよい理由またはNG理由",
  "contains_investment_advice": false,
  "contains_buy_sell_recommendation": false,
  "contains_unverified_numbers": false,
  "contains_price_prediction": false,
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
        # fail closed: 危険フラグがどれか1つでも立てば投稿不可にする
        danger_keys = (
            "contains_investment_advice", "contains_buy_sell_recommendation",
            "contains_unverified_numbers", "contains_price_prediction",
            "too_aggressive",
        )
        if any(bool(result.get(k)) for k in danger_keys) or result.get("risk_level") == "high":
            result["ok_to_post"] = False
        result.setdefault("contains_price_prediction", False)
        return result
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
