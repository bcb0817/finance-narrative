"""
weekly_events.py
Finnhub の決算カレンダー / 経済指標カレンダーから、その週(JST)の
注目イベント候補を取得し、weekly_normalizer が受け取れる「生イベント形式」で返す。

- 決算: /calendar/earnings  → hour(amc/bmo) を timing に変換、主要ティッカーのみ
- 指標: /calendar/economic  → time(UTC) を time_utc に、国・指標名・重要度を変換
- APIキー未設定・エラー・空でも例外を投げず [] を返す（Bot全体を止めない）

APIキーは環境変数 FINNHUB_API_KEY（GitHub Secrets）から読む。コードに直書きしない。
ネットワークが無い環境では空が返るので、呼び出し側でサンプルにフォールバックする。
"""

import os
import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
JST = timezone(timedelta(hours=9))

# 決算で残す主要ティッカー（米株指数・半導体/AI・大型株）→ 表示名
EARNINGS_WHITELIST: dict[str, str] = {
    "NVDA": "Nvidia", "MU": "Micron Technology", "AAPL": "Apple", "MSFT": "Microsoft",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "GOOGL": "Alphabet", "GOOG": "Alphabet",
    "AVGO": "Broadcom", "AMD": "AMD", "TSM": "TSMC", "NFLX": "Netflix", "QCOM": "Qualcomm",
    "INTC": "Intel", "ARM": "Arm", "SMCI": "Super Micro", "ASML": "ASML", "ORCL": "Oracle",
    "ADBE": "Adobe", "CRM": "Salesforce", "PLTR": "Palantir", "DELL": "Dell", "MRVL": "Marvell",
    "JPM": "JPMorgan", "BAC": "Bank of America", "GS": "Goldman Sachs", "WMT": "Walmart",
    "COST": "Costco", "HD": "Home Depot", "DIS": "Disney", "NKE": "Nike", "BA": "Boeing",
    "XOM": "Exxon Mobil", "LLY": "Eli Lilly", "UNH": "UnitedHealth", "V": "Visa", "MA": "Mastercard",
    "FDX": "FedEx", "ORCL2": "Oracle",
}

# 中央銀行イベント（国に依らず固定名）
_CB_MAP = [
    (("fomc", "federal funds", "interest rate decision", "rate decision"), "FOMC（政策金利）"),
    (("ecb",), "ECB理事会"),
    (("boe",), "BOE（イングランド銀行）"),
    (("boj",), "日銀 金融政策決定会合"),
]

# 米国指標 英→日（country=="US" のときだけ適用）
_US_STAT_MAP = [
    (("core pce",), "米PCEデフレーター（コア）"),
    (("pce",), "米PCE物価指数"),
    (("core cpi",), "米CPI（コア）"),
    (("cpi",), "米CPI（消費者物価指数）"),
    (("nonfarm", "non-farm", "payroll"), "米雇用統計（非農業部門）"),
    (("unemployment rate",), "米失業率"),
    (("initial jobless", "jobless claims"), "米新規失業保険申請件数"),
    (("gdp",), "米GDP"),
    (("ism manufacturing",), "米ISM製造業景況指数"),
    (("ism services", "ism non-manufacturing"), "米ISM非製造業景況指数"),
    (("manufacturing pmi",), "米製造業PMI"),
    (("services pmi",), "米サービス業PMI"),
    (("retail sales",), "米小売売上高"),
    (("durable goods",), "米耐久財受注"),
    (("michigan",), "米ミシガン大消費者信頼感指数"),
    (("personal income",), "米個人所得"),
    (("personal spending",), "米個人消費支出"),
    (("housing starts",), "米住宅着工件数"),
    (("existing home",), "米中古住宅販売件数"),
    (("new home",), "米新築住宅販売件数"),
]


def _http_get_json(path: str, params: dict):
    token = os.getenv("FINNHUB_API_KEY")
    if not token:
        logger.warning("FINNHUB_API_KEY 未設定。Finnhub取得をスキップします。")
        return None
    q = dict(params)
    q["token"] = token
    url = f"{FINNHUB_BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "singa9999-weekly-bot"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"Finnhub HTTPエラー {path}: {e.code} {e.reason}")
    except Exception as e:
        logger.warning(f"Finnhub取得失敗 {path}: {e}")
    return None


def _fetch_earnings(frm: str, to: str) -> list[dict]:
    data = _http_get_json("/calendar/earnings", {"from": frm, "to": to})
    if not data:
        return []
    out = []
    for e in data.get("earningsCalendar", []) or []:
        sym = (e.get("symbol") or "").upper()
        if sym not in EARNINGS_WHITELIST:
            continue
        hour = (e.get("hour") or "").lower()
        timing = {"amc": "after market close", "bmo": "before market open"}.get(hour, "")
        out.append({
            "date": e.get("date", ""),
            "country": "US",
            "category": "企業",
            "title": f"{EARNINGS_WHITELIST[sym]} 決算発表",
            "timing": timing,
            "source_name": "Finnhub",
            "source_url": "https://finnhub.io/calendar/earnings",
            "verified": True,
        })
    logger.info(f"決算カレンダー: 主要 {len(out)}件（whitelist適用）")
    return out


def _econ_title(event_en: str, country: str) -> str:
    el = (event_en or "").lower()
    # 中銀系は国に依らず固定名
    for keys, name in _CB_MAP:
        if any(k in el for k in keys):
            return name
    # 米国指標の和名は US のときだけ
    if country == "US":
        for keys, ja in _US_STAT_MAP:
            if any(k in el for k in keys):
                return ja
    return event_en or "経済指標"


def _econ_category(event_en: str) -> str:
    el = (event_en or "").lower()
    if any(k in el for k in ("fomc", "rate decision", "interest rate", "federal funds", "ecb", "boe", "boj")):
        return "中銀"
    if any(k in el for k in ("speech", "speaks", "testimony", "powell")):
        return "発言"
    return "統計"


def _fetch_economic(frm: str, to: str) -> list[dict]:
    data = _http_get_json("/calendar/economic", {"from": frm, "to": to})
    if not data:
        return []
    out = []
    for e in data.get("economicCalendar", []) or []:
        country = (e.get("country") or "").upper()
        event_en = e.get("event", "") or ""
        t = (e.get("time", "") or "").strip()  # "YYYY-MM-DD HH:MM:SS" (UTC)
        date = t.split(" ")[0] if t else ""
        hhmmss = t.split(" ")[1] if " " in t else ""
        item = {
            "date": date,
            "country": country or "US",
            "category": _econ_category(event_en),
            "title": _econ_title(event_en, country or "US"),
            "importance": e.get("impact", ""),
            "source_name": "Finnhub",
            "source_url": "https://finnhub.io/calendar/economic",
            "verified": True,
        }
        # 00:00:00 や時刻なしは「時刻不明」扱い（無理に時刻を作らない）
        if hhmmss and hhmmss != "00:00:00":
            item["time_utc"] = t.replace(" ", "T")
        out.append(item)
    logger.info(f"経済指標カレンダー: {len(out)}件")
    return out


# =====================================================================
# 公式日程に基づくマクロ指標カレンダー（2026年）
# 出所: OMB/Executive Office "Schedule of Release Dates for Principal
#       Federal Economic Indicators for 2026"（BLS/BEA/Census）
#       + Federal Reserve FOMC calendar
# 時刻は各指標の標準発表時刻(ET)。BLS/BEA/Census主要指標=8:30 ET、
# 新築住宅販売=10:00 ET、FOMC声明=14:00 ET。time_et→JSTは normalizer が変換。
# ※ AIの推測ではなく公式日程の転記。年に1〜数回、公式更新時に見直すこと。
# =====================================================================
_MACRO_YEAR = 2026
# (title, category, time_et, [Jan..Dec の日。0=その月なし], source_name, source_url)
_MACRO_DEFS = [
    ("米雇用統計（非農業部門）", "統計", "08:30", [9, 6, 6, 3, 8, 5, 2, 7, 4, 2, 6, 4],
     "BLS（公式日程）", "https://www.bls.gov/schedule/"),
    ("米CPI（消費者物価指数）", "統計", "08:30", [13, 11, 11, 10, 12, 10, 14, 12, 11, 14, 10, 10],
     "BLS（公式日程）", "https://www.bls.gov/schedule/"),
    ("米PCEデフレーター（個人所得・消費）", "統計", "08:30", [29, 26, 27, 30, 28, 25, 30, 26, 30, 29, 25, 23],
     "BEA（公式日程）", "https://www.bea.gov/news/schedule"),
    ("米GDP", "統計", "08:30", [29, 26, 27, 30, 28, 25, 30, 26, 30, 29, 25, 23],
     "BEA（公式日程）", "https://www.bea.gov/news/schedule"),
    ("米小売売上高", "統計", "08:30", [15, 17, 16, 16, 14, 17, 16, 14, 16, 15, 17, 16],
     "Census（公式日程）", "https://www.census.gov/economic-indicators/"),
    ("米耐久財受注", "統計", "08:30", [28, 26, 25, 24, 28, 25, 27, 26, 25, 27, 25, 23],
     "Census（公式日程）", "https://www.census.gov/economic-indicators/"),
    ("米住宅着工件数", "統計", "08:30", [21, 18, 17, 17, 19, 16, 17, 18, 17, 20, 18, 17],
     "Census（公式日程）", "https://www.census.gov/economic-indicators/"),
    ("米新築住宅販売件数", "統計", "10:00", [27, 25, 24, 23, 27, 24, 24, 25, 24, 27, 25, 23],
     "Census（公式日程）", "https://www.census.gov/economic-indicators/"),
]
# FOMC 政策金利発表（会合2日目, 14:00 ET）
_FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


def _macro_events() -> list[dict]:
    """公式日程テーブルから2026年のマクロ指標を生イベント形式で生成（APIキー不要）。"""
    out: list[dict] = []
    for title, cat, time_et, days, sname, surl in _MACRO_DEFS:
        for i, d in enumerate(days):
            if not d:
                continue
            out.append({
                "date": f"{_MACRO_YEAR}-{i + 1:02d}-{d:02d}",
                "country": "US", "category": cat, "title": title,
                "time_et": time_et, "source_name": sname, "source_url": surl,
                "verified": True,
            })
    for ds in _FOMC_DATES:
        out.append({
            "date": ds, "country": "US", "category": "中銀",
            "title": "FOMC（政策金利発表）", "time_et": "14:00",
            "source_name": "Federal Reserve（公式日程）",
            "source_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "verified": True,
        })
    return out


def _coming_monday_jst() -> "datetime.date":
    today = datetime.now(JST).date()
    delta = (0 - today.weekday()) % 7   # 月曜=0。今日が月曜なら今日、それ以外は次の月曜
    return today + timedelta(days=delta)


def fetch_weekly_events(week_start=None) -> list[dict]:
    """
    指定週(JST, 月〜日)の注目イベント候補を「生イベント形式」で返す。
    week_start: date or "YYYY-MM-DD"。未指定なら次の月曜。
    """
    if week_start is None:
        week_start = _coming_monday_jst()
    elif isinstance(week_start, str):
        week_start = datetime.strptime(week_start, "%Y-%m-%d").date()
    week_end = week_start + timedelta(days=6)

    # 米国日付は JST より前後するため、取得窓は前後1日広げる
    frm = (week_start - timedelta(days=1)).strftime("%Y-%m-%d")
    to = week_end.strftime("%Y-%m-%d")

    raw: list[dict] = []
    raw += _macro_events()              # 公式マクロ日程（APIキー不要・常時利用可）
    raw += _fetch_earnings(frm, to)     # Finnhub 決算（無料枠でOK）
    # 経済指標API(/calendar/economic)は無料枠で403のため既定オフ。
    # 有料枠で併用したい場合だけ環境変数 FINNHUB_ECON=1 を設定する。
    if os.getenv("FINNHUB_ECON") == "1":
        raw += _fetch_economic(frm, to)
    logger.info(f"イベント取得合計: {len(raw)}件（マクロ+決算, 取得窓 {frm}〜{to}）")
    if not raw:
        return []

    # 正規化して JST 週内(week_start〜week_end)に入るものだけ残す（生dictで返す）
    from weekly_normalizer import normalize_event
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")
    kept: list[dict] = []
    for r in raw:
        n = normalize_event(r)
        if not n["title"]:
            continue
        if ws <= n["display_date_jst"] <= we:
            kept.append(r)
    logger.info(f"JST週内({ws}〜{we})に絞り込み: {len(kept)}件")
    return kept


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    events = fetch_weekly_events()
    print(json.dumps(events, ensure_ascii=False, indent=2))
