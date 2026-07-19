"""
common/calendar_utils.py
米国市場（NYSE/NASDAQ）の営業日判定。narrative / market-map / ローカルscheduler で共用。

休場日は公式カレンダーの転記（AIの推測ではない）。年に1回、翌年分を追記すること。
半日立会い（早期クローズ）は通常営業扱いとし、ここには含めない。
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None

# 出所: NYSE/ICE 公式カレンダー
US_MARKET_HOLIDAYS = {
    # 2026年（10日）
    "2026-01-01",  # 元日
    "2026-01-19",  # キング牧師記念日
    "2026-02-16",  # ワシントン誕生日（大統領の日）
    "2026-04-03",  # グッドフライデー
    "2026-05-25",  # メモリアルデー
    "2026-06-19",  # ジューンティーンス
    "2026-07-03",  # 独立記念日の振替（7/4が土曜のため）
    "2026-09-07",  # レイバーデー
    "2026-11-26",  # サンクスギビング
    "2026-12-25",  # クリスマス
}


def now_et() -> datetime:
    if _ET is not None:
        return datetime.now(_ET)
    # フォールバック（夏時間 -4h 固定近似）
    return datetime.now(timezone.utc) - timedelta(hours=4)


def is_us_market_business_day(d: date | None = None) -> bool:
    """土日・休場日でなければ True。"""
    d = d or now_et().date()
    if d.weekday() >= 5:  # 5=土, 6=日
        return False
    return d.isoformat() not in US_MARKET_HOLIDAYS


def us_market_holiday_reason(d: date | None = None) -> str:
    """休場なら理由文字列、営業日なら空文字。"""
    d = d or now_et().date()
    if d.weekday() >= 5:
        return f"米国市場の週末（ET {d.isoformat()}）"
    if d.isoformat() in US_MARKET_HOLIDAYS:
        return f"米国市場の休場日（ET {d.isoformat()}）"
    return ""
