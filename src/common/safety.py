"""
common/safety.py
安全チェック・JST時間ガード・文字数チェックなどの共通処理。
"""

from datetime import datetime, timezone, timedelta
import os
import re

MAX_POST_LENGTH = 280  # 後方互換（=X_MAX_CHARS）

# プラットフォーム別 文字数上限
X_MAX_CHARS = 280
X_SAFE_CHARS = 260
THREADS_MAX_CHARS = 500
THREADS_SAFE_CHARS = 480

# build_thread_text の 親/reply 予算（プラットフォーム別）
X_PARENT_BUDGET = 240
X_REPLY_BUDGET = 260
THREADS_PARENT_BUDGET = 480
THREADS_REPLY_BUDGET = 480

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


def safety_check(text: str, platform: str = "x") -> None:
    """投稿前チェック。platform で上限を切替（X=280字 / Threads=500字）。
    Xは日本語が重み2のため、重み付き長も280以下であることを要求する。
    """
    if not text or not text.strip():
        raise ValueError("投稿本文が空です")
    max_chars, _ = platform_limits(platform)
    if len(text) > max_chars:
        raise ValueError(f"投稿本文が長すぎます（{platform}）: {len(text)}文字 > {max_chars}")
    if platform == "x" and weighted_len(text) > X_MAX_CHARS:
        raise ValueError(f"投稿本文が長すぎます（x重み付き）: {weighted_len(text)} > {X_MAX_CHARS}")
    for word in NG_WORDS:
        if word in text:
            raise ValueError(f"NGワードを検出しました: {word}")


# 投稿価値しきい値（Botごとに分離）
# 通常Bot: post_value>=7 で投稿（<=6 のみスキップ）
# Narrative: post_value>=8 のみ投稿（7はスキップ）
NEWS_BOT_POST_VALUE_THRESHOLD = 7
NARRATIVE_POST_VALUE_THRESHOLD = 8
# 後方互換（既存 import 用。汎用の既定値）
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
    try:
        from runtime import log_decision
        log_decision({
            "selected_post_type": selected_post_type, "post_value": post_value,
            "skip_reason": skip_reason, "source_titles": srcs, "ticker": ticker,
            "final_caption": cap, "image_path": image_path, "tweet_id": tweet_id,
        })
    except Exception:
        pass
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


# =========================================================
# プラットフォーム別 文字数ユーティリティ
# =========================================================

def platform_limits(platform: str = "x") -> tuple[int, int]:
    """(max_chars, safe_chars) を返す。"""
    if (platform or "x").lower() == "threads":
        return THREADS_MAX_CHARS, THREADS_SAFE_CHARS
    return X_MAX_CHARS, X_SAFE_CHARS


def get_post_platform() -> str:
    """X専用運用。Threads対応は無効化済み（POST_PLATFORM は無視して常に "x"）。"""
    return "x"


def post_cost(text: str, platform: str = "x") -> int:
    """投稿コスト。Xは重み付き（日本語=2）、Threadsは素の文字数で数える。"""
    return weighted_len(text) if (platform or "x").lower() == "x" else len(text)


def _split_sentences(text: str) -> list[str]:
    """意味単位（文）に分割する。文末記号は保持し、改行は段落境界として扱う。"""
    out: list[str] = []
    for para in re.split(r"\n+", (text or "").strip()):
        para = para.strip()
        if not para:
            continue
        # 文末記号（。．.!！?？）で区切り、記号は文側に残す
        for s in re.findall(r"[^。．\.!！?？]*[。．\.!！?？]|[^。．\.!！?？]+$", para):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _split_clauses(sentence: str) -> list[str]:
    """1文が長すぎる場合の保険。読点・カンマ等の節境界で分割（記号は残す）。"""
    parts = re.findall(r"[^、，,;；]*[、，,;；]|[^、，,;；]+$", sentence)
    return [p.strip() for p in parts if p and p.strip()]


def smart_trim(text: str, max_cost: int, platform: str = "x") -> str:
    """max_cost 以内に収める。ただし「…」や文の途中切りは禁止。
    文（必要なら節）の単位で、入る分だけを連結して返す。
    """
    text = (text or "").strip()
    if post_cost(text, platform) <= max_cost:
        return text
    units = _split_sentences(text)
    picked: list[str] = []
    for u in units:
        cand = (" ".join(picked + [u])).strip() if picked else u
        if post_cost(cand, platform) <= max_cost:
            picked.append(u)
        else:
            break
    if picked:
        return " ".join(picked).strip()
    # 先頭の1文すら入らない → 節で詰める
    picked = []
    for c in _split_clauses(units[0] if units else text):
        cand = (" ".join(picked + [c])).strip() if picked else c
        if post_cost(cand, platform) <= max_cost:
            picked.append(c)
        else:
            break
    return " ".join(picked).strip()  # それでも空なら空文字（呼び出し側でスキップ）


def build_thread_text(full_text: str, platform: str = "x") -> tuple[str, list[str]]:
    """本文をスレッド用に分割する。「…」「文の途中切り」は使わず、意味単位で分ける。

    platform="x":      親=240字以内 / reply=260字以内（コストは重み付き）
    platform="threads": 親=480字以内 / reply=480字以内（最大500まで許容、素の文字数）

    戻り値: (親投稿, [reply, ...])
    """
    platform = (platform or "x").lower()
    if platform == "threads":
        parent_budget, reply_budget = THREADS_PARENT_BUDGET, THREADS_REPLY_BUDGET
    else:
        parent_budget, reply_budget = X_PARENT_BUDGET, X_REPLY_BUDGET

    sentences = _split_sentences(full_text)
    if not sentences:
        return (full_text or "").strip(), []

    posts: list[str] = []
    cur = ""
    budget = parent_budget  # 最初は親の予算

    def _flush():
        nonlocal cur
        if cur.strip():
            posts.append(cur.strip())
        cur = ""

    for s in sentences:
        # 1文が単独で予算超過 → 節分割し、それでも超えるなら smart_trim でその投稿だけ収める
        if post_cost(s, platform) > budget:
            _flush()
            budget = reply_budget if posts else parent_budget
            for c in _split_clauses(s):
                cand = (cur + " " + c).strip() if cur else c
                if post_cost(cand, platform) <= budget:
                    cur = cand
                else:
                    _flush()
                    budget = reply_budget
                    cur = c if post_cost(c, platform) <= budget else smart_trim(c, budget, platform)
            continue

        cand = (cur + " " + s).strip() if cur else s
        if post_cost(cand, platform) <= budget:
            cur = cand
        else:
            _flush()
            budget = reply_budget
            cur = s

    _flush()
    if not posts:
        return (full_text or "").strip(), []
    return posts[0], posts[1:]


def build_x_thread_text(full_text: str) -> tuple[str, list[str]]:
    """X専用のスレッド分割。親<=240 / reply<=260（X重み付き、CJK=2）。
    reply先頭に "1/n " 形式の番号を付ける。文の途中では切らず「…」も使わない。
    セクション順（結論→何が起きた→なぜ重要→市場への影響→見るべき点）は
    入力テキストの順序をそのまま保持することで守られる。
    """
    # まず番号prefixぶんの余白(重み6程度)を確保して分割
    parent, replies = build_thread_text(full_text, "x")
    if not replies:
        return parent, []

    n = len(replies)
    numbered: list[str] = []
    for i, r in enumerate(replies, 1):
        prefix = f"{i}/{n} "
        body = r
        # prefix込みで reply予算(260)を超えるなら意味単位で詰める
        budget = X_REPLY_BUDGET - weighted_len(prefix)
        if post_cost(body, "x") > budget:
            body = smart_trim(body, budget, "x")
        if not body:
            continue
        numbered.append(prefix + body)
    return parent, numbered
