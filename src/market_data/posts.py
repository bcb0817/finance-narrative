from __future__ import annotations

import os
from dataclasses import dataclass

from common.openai_client import review_tweet_with_openai
from common.post_registry import record_post
from common.x_client import post_tweet_with_image

from .models import CrossAssetSignal, MarketMovement
from .symbols import symbol_config


TRUE_VALUES = {"1", "true", "yes"}


@dataclass(frozen=True)
class MarketPostResult:
    status: str
    text: str
    tweet_id: str = ""
    reason: str = ""


def external_display_approved() -> bool:
    return os.getenv("TWELVEDATA_EXTERNAL_DISPLAY_APPROVED", "false").lower() in TRUE_VALUES


def market_post_enabled() -> bool:
    return os.getenv("MARKET_DATA_POST_ENABLED", "false").lower() in TRUE_VALUES


def symbol_external_display_allowed(symbol: str) -> bool:
    try:
        return bool(symbol_config(symbol).get("external_display_allowed", False))
    except (KeyError, ValueError):
        return False


def post_type_enabled(movement: MarketMovement) -> bool:
    if movement.alert_type == "volume_alert":
        flag = "VOLUME_ALERT_POST_ENABLED"
    elif movement.asset_type == "equity":
        flag = "MEGACAP_POST_ENABLED"
    elif movement.asset_type == "etf":
        flag = "ETF_POST_ENABLED"
    elif movement.asset_type == "crypto":
        flag = "CRYPTO_SIGNAL_POST_ENABLED"
    else:
        return False
    return os.getenv(flag, "false").lower() in TRUE_VALUES


def build_market_post(movement: MarketMovement, *, post_type: str | None = None) -> str:
    kind = post_type or movement.alert_type
    sign = "+" if movement.percentage_change >= 0 else ""
    fact = (
        f"{movement.symbol}は{movement.window_minutes}分で"
        f"{movement.start_price:,.2f}から{movement.current_price:,.2f}へ、"
        f"{sign}{movement.percentage_change:.2f}%変動。"
    )
    if kind == "volume_alert":
        rv = "-" if movement.relative_volume is None else f"{movement.relative_volume:.1f}倍"
        lead = f"【出来高変化】{movement.symbol}の出来高は直近平均の{rv}。"
        context = "株価変化と市場参加者の関心を示す観測値であり、売買主体は確認できません。"
    elif kind == "sector_divergence":
        lead = f"【セクター変動】{movement.symbol}で通常より大きな値動き。"
        context = "市場全体との比較が必要で、現時点では原因を断定できません。"
    elif kind == "what_to_watch":
        lead = f"{movement.symbol}で注目すべき値動き。"
        context = "次は関連ETF、出来高、公式発表を確認します。"
    else:
        lead = f"【市場データ】{movement.symbol}が急変。"
        context = "現時点で明確な材料は確認できていません。"
    delayed = "遅延データです。" if movement.data_quality == "delayed" else ""
    if movement.data_source == "fixture":
        source = "Source: TEST/FIXTURE（架空データ・実在価格ではありません）"
        lead = f"【TEST/FIXTURE・架空データ】\n{lead}"
    else:
        source = "Source: Twelve Data（単一データ提供元）"
    return f"{lead}\n{fact}\n{delayed}{context}\n{source}"[:280]


def build_cross_asset_post(signal: CrossAssetSignal) -> str:
    changes = "、".join(f"{key} {value:+.2f}%" for key, value in signal.movements.items())
    return (
        f"【クロスアセット】{signal.pattern_type}\n{changes}\n"
        f"{signal.likely_interpretation}\n相関は因果関係を意味しません。"
    )[:280]


def publish_market(movement: MarketMovement, image_path: str) -> MarketPostResult:
    text = build_market_post(movement)
    if not external_display_approved():
        return MarketPostResult("license_blocked", text, reason="TWELVEDATA_EXTERNAL_DISPLAY_APPROVED=false")
    if not symbol_external_display_allowed(movement.symbol):
        return MarketPostResult("symbol_license_blocked", text, reason="external_display_allowed=false")
    if not market_post_enabled():
        return MarketPostResult("disabled", text, reason="MARKET_DATA_POST_ENABLED=false")
    if not post_type_enabled(movement):
        return MarketPostResult("type_disabled", text, reason="market-data post type disabled")
    if os.getenv("POST_ENABLED", "false").lower() not in TRUE_VALUES:
        return MarketPostResult("global_disabled", text, reason="POST_ENABLED=false")
    review = review_tweet_with_openai(text, f"{movement.symbol} market movement", "Twelve Data")
    if not review.get("ok_to_post"):
        return MarketPostResult("review_blocked", text, reason=str(review.get("reason", "review rejected")))
    tweet_id = post_tweet_with_image(text, image_path)
    if not tweet_id:
        return MarketPostResult("not_posted", text, reason="global posting policy blocked")
    record_post(
        tweet_id, text=text, source="Twelve Data", bot="market-data",
        mode=movement.alert_type,
        extra={
            "bot_type": "market_data", "symbol": movement.symbol,
            "asset_type": movement.asset_type, "movement_id": movement.movement_id,
            "signal_id": None, "movement_window": movement.window_minutes,
            "movement_direction": movement.direction,
            "movement_percent": movement.percentage_change,
            "relative_volume": movement.relative_volume,
            "alert_type": movement.alert_type, "chart_type": "line",
            "market_data_source": movement.data_source,
            "data_delay_seconds": None,
            "source_confirmation_status": "unknown",
            "radar_influenced": False, "external_display_approved": True,
            "with_image": True, "has_media": True,
        },
    )
    return MarketPostResult("posted", text, tweet_id=tweet_id)
