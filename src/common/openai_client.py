"""
common/openai_client.py
OpenAI関連の共通処理（クライアント生成・本文生成・レビュー）。
"""

import os
import json
import logging

from openai import OpenAI

from safety import clean_text

logger = logging.getLogger(__name__)

OPENAI_GENERATE_MODEL = os.getenv("OPENAI_GENERATE_MODEL", "gpt-5-mini")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5-nano")


def get_openai_client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("環境変数が未設定です: OPENAI_API_KEY")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


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
