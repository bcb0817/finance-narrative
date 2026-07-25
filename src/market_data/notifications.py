from __future__ import annotations

from common.operations_alerts import send_discord_preview

from .models import MarketMovement
from .posts import external_display_approved


def notify_market_preview(
    movement: MarketMovement, text: str, *, fixture: bool = False,
    blocked_reason: str = "", chart_path: str = "",
) -> dict:
    if fixture:
        detail = (
            f"[TEST/FIXTURE・架空データ] {movement.alert_type} {movement.symbol} "
            f"{movement.percentage_change:+.2f}% RV={movement.relative_volume or 0:.2f}x\n{text}"
        )
    elif not external_display_approved():
        detail = (
            f"Market Data候補を検知しましたが外部表示未承認のため、価格・チャート・本文は送信しません。 "
            f"symbol={movement.symbol} reason={blocked_reason or 'external_display_not_approved'}"
        )
    else:
        detail = f"{movement.alert_type} {movement.symbol}\n{text}"
    return send_discord_preview(
        f"market_preview_{movement.movement_id}", detail,
        file_path=chart_path if fixture or external_display_approved() else "",
    )
