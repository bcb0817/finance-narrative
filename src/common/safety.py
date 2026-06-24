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


# 全Bot共通の投稿価値しきい値（これ未満は投稿しない）
POST_VALUE_THRESHOLD = 8


def weighted_len(text: str) -> int:
    """X(Twitter)の重み付き文字数（CJK等は2、半角は1）の近似。"""
    n = 0
    for ch in text or "":
        n += 1 if ord(ch) < 0x1100 else 2
    return n


def format_decision_log(
    *,
    selected_post_type: str = "",
    post_value="",
    skip_reason: str = "",
    source_titles=None,
    ticker: str = "",
    final_caption: str = "",
    image_path: str = "",
    tweet_id: str = "",
) -> str:
    """要件で指定された投稿判断ログを1行に整形する（全Bot共通フォーマット）。"""
    if source_titles is None:
        srcs = ""
    elif isinstance(source_titles, (list, tuple)):
        srcs = " | ".join(str(s) for s in source_titles)[:300]
    else:
        srcs = str(source_titles)[:300]
    cap = (final_caption or "").replace("\n", " / ")
    return (
        "[DECISION] "
        f"selected_post_type={selected_post_type or '-'} | "
        f"post_value={post_value if post_value != '' else '-'} | "
        f"skip_reason={skip_reason or '-'} | "
        f"source_titles={srcs or '-'} | "
        f"ticker={ticker or '-'} | "
        f"final_caption={cap or '-'} | "
        f"image_path={image_path or '-'} | "
        f"tweet_id={tweet_id or '-'}"
    )
