"""
weekly_normalizer.py
取得した生イベントを共通形式に正規化する。すべて日本時間（JST）基準に変換する。

共通イベント形式（dict）:
  source_date      : 元データ上の日付 "YYYY-MM-DD"
  display_date_jst : 画像に表示するJST基準の日付 "YYYY-MM-DD"
  date             : 後方互換（= display_date_jst）。日別グルーピングはこれを使う
  weekday          : display_date_jst から導出した曜日 "月"〜"日"
  time_jst         : "21:30" / "早朝" / "20:00前後" / "時間未定" など
  timing           : 元の発表タイミング文字列（ログ用に保持）
  country          : 国コード（US/JP/EU/CN/UK/TW ...）
  category         : 中銀 / 発言 / 統計 / 市場 / 企業
  title            : イベント名
  importance       : "high" / "mid" / "low"
  tentative        : bool
  note             : 補足

時刻変換の優先順位:
  1) time_utc / time_et が取得できれば Asia/Tokyo に変換（日付跨ぎも反映）
  2) 米国企業決算の after market close 系 → JST翌日 "早朝"
                       before market open 系 → 同日 "20:00前後"
  3) いずれも無ければ source_date のまま、time_jst は与えられた値か "時間未定"
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CATEGORIES = ["中銀", "発言", "統計", "市場", "企業"]

_CATEGORY_ALIASES = {
    "central_bank": "中銀", "centralbank": "中銀", "fomc": "中銀", "boj": "中銀",
    "ecb": "中銀", "rate": "中銀", "政策": "中銀", "中央銀行": "中銀",
    "speech": "発言", "speeches": "発言", "talk": "発言", "発言・講演": "発言",
    "stat": "統計", "statistics": "統計", "indicator": "統計", "経済指標": "統計", "指標": "統計",
    "market": "市場", "auction": "市場", "holiday": "市場", "市場イベント": "市場",
    "earnings": "企業", "company": "企業", "決算": "企業", "企業決算": "企業",
}

COUNTRIES = {
    "US": "US", "JP": "JP", "EU": "EU", "CN": "CN", "UK": "UK", "TW": "TW",
    "DE": "DE", "GB": "UK", "KR": "KR", "IN": "IN",
}

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

JST = timezone(timedelta(hours=9))

# 引け後（AMC）／寄り前（BMO）判定キーワード
_AMC_KEYS = [
    "after market close", "after close", "after-hours", "afterhours",
    "post-market", "postmarket", "amc", "market close後", "米国引け後", "引け後",
]
_BMO_KEYS = [
    "before market open", "before open", "pre-market", "premarket",
    "bmo", "market open前", "寄り前", "始値前",
]


def _et_tz():
    """米国東部時間。zoneinfoがあればDST込み、無ければEDT(-4)で近似。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


def _weekday_ja(date_str: str, fallback: str = "") -> str:
    try:
        return _WEEKDAY_JA[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except Exception:
        return fallback


def _shift_date(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _parse_dt(t: str, source_date: str) -> datetime:
    """ISO日時 or HH:MM(＋source_date) を naive datetime に。"""
    t = t.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            tt = datetime.strptime(t, fmt)
            base = datetime.strptime(source_date, "%Y-%m-%d")
            return base.replace(hour=tt.hour, minute=tt.minute)
        except ValueError:
            pass
    raise ValueError(f"時刻パース不可: {t}")


def _to_jst(dt_naive: datetime, src: str) -> datetime:
    if src == "utc":
        aware = dt_naive.replace(tzinfo=timezone.utc)
    else:  # et
        aware = dt_naive.replace(tzinfo=_et_tz())
    return aware.astimezone(JST)


def _match(text: str, keys: list[str]) -> bool:
    return any(k in text for k in keys)


def _resolve_jst(raw: dict, source_date: str, category: str, country: str) -> tuple[str, str, str]:
    """戻り値: (display_date_jst, time_jst, timing)"""
    timing = str(raw.get("timing", "") or "").strip()
    tl = timing.lower()

    # 1) 明示時刻（UTC/ET）があれば最優先で変換
    time_utc = raw.get("time_utc")
    time_et = raw.get("time_et")
    if time_utc or time_et:
        try:
            if time_utc:
                j = _to_jst(_parse_dt(str(time_utc), source_date), "utc")
            else:
                j = _to_jst(_parse_dt(str(time_et), source_date), "et")
            return j.strftime("%Y-%m-%d"), j.strftime("%H:%M"), timing
        except Exception as e:
            logger.warning(f"時刻変換失敗（フォールバック）: {raw.get('title','')} / {e}")

    # 2) 米国企業決算の引け後／寄り前
    is_us_earnings = (category == "企業" and country == "US")
    if is_us_earnings or _match(tl, _AMC_KEYS) or _match(tl, _BMO_KEYS):
        if _match(tl, _AMC_KEYS):
            return _shift_date(source_date, 1), "早朝", timing          # JST翌日早朝
        if _match(tl, _BMO_KEYS):
            return source_date, "20:00前後", timing                     # 同日夜
        # 米国企業決算だがタイミング不明
        if is_us_earnings:
            return source_date, "時間未定", timing

    # 3) フォールバック：与えられた time_jst か 時間未定
    tj = str(raw.get("time_jst", "") or "").strip()
    if not tj:
        tj = "時間未定" if raw.get("tentative") else "未定"
    return source_date, tj, timing


def _norm_category(raw: str) -> str:
    if not raw:
        return "市場"
    r = str(raw).strip()
    return r if r in CATEGORIES else _CATEGORY_ALIASES.get(r.lower(), "市場")


def _norm_country(raw: str) -> str:
    if not raw:
        return "US"
    r = str(raw).strip().upper()
    return COUNTRIES.get(r, r[:2])


def _norm_importance(raw) -> str:
    if isinstance(raw, (int, float)):
        return "high" if raw >= 3 else "mid" if raw == 2 else "low"
    r = str(raw or "").strip().lower()
    if r in ("high", "高", "★★★", "3"):
        return "high"
    if r in ("low", "低", "★", "1"):
        return "low"
    return "mid"


def normalize_event(raw: dict) -> dict:
    source_date = str(raw.get("date", "")).strip()
    category = _norm_category(raw.get("category", ""))
    country = _norm_country(raw.get("country", ""))

    display_date, time_jst, timing = _resolve_jst(raw, source_date, category, country)

    source_name = str(raw.get("source_name", "") or "").strip()
    source_url = str(raw.get("source_url", "") or "").strip()
    # 出所が無ければ verified=False（後段で除外される）
    verified = bool(raw.get("verified", True)) and bool(source_name or source_url)

    return {
        "source_date": source_date,
        "display_date_jst": display_date,
        "date": display_date,                 # 後方互換（グルーピングはこちら）
        "weekday": _weekday_ja(display_date),
        "time_jst": time_jst,
        "timing": timing,
        "country": country,
        "category": category,
        "title": str(raw.get("title", "")).strip(),
        "importance": _norm_importance(raw.get("importance")),
        "tentative": bool(raw.get("tentative", False)),
        "note": str(raw.get("note", "") or "").strip(),
        "source_name": source_name,
        "source_url": source_url,
        "verified": verified,
    }


def normalize_events(raw_events: list[dict]) -> list[dict]:
    out = [normalize_event(e) for e in raw_events]
    return [e for e in out if e["title"]]
