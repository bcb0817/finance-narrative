"""
common/safety.py
安全チェック・JST時間ガード・文字数チェックなどの共通処理。
"""

from datetime import datetime, timezone, timedelta

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
