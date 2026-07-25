from __future__ import annotations

from common.operations_alerts import send_discord_alerts

from .models import FxMovement


def notify_preview(movement: FxMovement, text: str) -> dict:
    detail = (
        f"FX候補 {movement.pair} {movement.window} "
        f"{movement.change_yen:+.2f}円 ({movement.change_pct:+.2f}%)\n{text}"
    )
    return send_discord_alerts(
        [{"code": f"fx_preview_{movement.movement_id}", "severity": "info", "detail": detail}]
    )


def notify_result(movement: FxMovement, status: str, *, tweet_id: str = "") -> dict:
    detail = f"FX通知結果: {status} / {movement.pair} / movement_id={movement.movement_id}"
    if tweet_id:
        detail += f" / https://x.com/i/web/status/{tweet_id}"
    return send_discord_alerts(
        [{"code": f"fx_result_{movement.movement_id}_{status}", "severity": "info", "detail": detail}]
    )
