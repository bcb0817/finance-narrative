from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from common.openai_client import review_tweet_with_openai
from common.post_registry import record_post
from common.x_client import post_tweet_with_image

from .models import FxMovement


@dataclass(frozen=True)
class PostResult:
    status: str
    text: str
    tweet_id: str = ""
    reason: str = ""


def build_post(movement: FxMovement, *, style: str = "fx_breaking") -> str:
    pair = f"{movement.pair[:3]}/{movement.pair[3:]}"
    sign = "+" if movement.change_yen >= 0 else ""
    move = (
        f"{pair}は{movement.window}で{movement.start_price:.2f}円から"
        f"{movement.end_price:.2f}円へ、{sign}{movement.change_yen:.2f}円"
        f"（{sign}{movement.change_pct:.2f}%）変動。"
    )
    if style == "fx_misconception":
        lead = "為替の急変＝特定材料とは限りません。"
    elif style == "fx_what_to_watch":
        lead = f"為替市場で注目すべき動き。{movement.direction_ja}方向への変動です。"
    else:
        lead = f"【為替速報】{movement.direction_ja}が進行。"
    cause = movement.cause_summary or "現時点で明確な材料は確認できていません"
    ending = "短時間の値動きが続く可能性があるため、流動性と次の公表情報を確認します。"
    text = f"{lead}\n{move}\n{cause}。{ending}"
    if len(text) > 280:
        text = f"{lead}\n{move}\n{cause}。"
    return text[:280]


def publish(movement: FxMovement, image_path: str, *, style: str = "fx_breaking") -> PostResult:
    text = build_post(movement, style=style)
    if not text or len(text) > 280 or text[-1] not in "。！？!?":
        return PostResult("content_blocked", text, reason="incomplete or invalid post text")
    if any(word in text for word in ("必ず", "確実", "買うべき", "売るべき")):
        return PostResult("content_blocked", text, reason="prohibited financial phrasing")
    image = Path(image_path)
    try:
        with Image.open(image) as handle:
            handle.verify()
    except (OSError, ValueError):
        return PostResult("image_blocked", text, reason="chart image verification failed")
    review = review_tweet_with_openai(text, "USD/JPY FX movement", "market data provider")
    if not review.get("ok_to_post"):
        return PostResult("review_blocked", text, reason=str(review.get("reason", "review rejected")))
    if os.getenv("FX_POST_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        return PostResult("disabled", text, reason="FX_POST_ENABLED=false")
    tweet_id = post_tweet_with_image(text, image_path)
    if not tweet_id:
        return PostResult("not_posted", text, reason="global policy or POST_ENABLED blocked posting")
    record_post(
        tweet_id,
        text=text,
        source="market data provider",
        bot="fx-alert",
        mode=style,
        extra={
            "has_media": True,
            "with_image": True,
            "image_type": "fx_line_chart",
            "movement_id": movement.movement_id,
            "pair": movement.pair,
            "bot_type": "fx_alert",
            "movement_window": movement.window,
            "movement_jpy": movement.change_yen,
            "movement_percent": movement.change_pct,
            "movement_direction": movement.movement_direction,
            "alert_level": movement.alert_level,
            "chart_type": "line",
            "fx_data_source": movement.data_source,
            "cause_confidence": movement.cause_confidence,
            "source_confirmation_status": movement.cause_confidence,
            "chart_path": image_path,
            "post_type": "fx_alert",
        },
    )
    return PostResult("posted", text, tweet_id=tweet_id)
