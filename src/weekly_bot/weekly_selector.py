"""
weekly_selector.py
取得済みイベントを「米国株式市場向け」に market_impact_score で選別する。

スコア設計（0〜100。米国株・米金利・ドル円・半導体/大型ハイテクへの影響度）:
  最優先(high) : FOMC/FRB, CPI, PCE, 雇用統計, GDP, ISM/PMI, 小売売上,
                 失業保険, 耐久財, 個人所得/消費, ミシガン大, 大型/半導体・AI決算
  中優先(mid)  : 住宅指標, 米国債入札, 重要Fed高官発言, ECB/BOE, 日銀(ドル円波及)
  除外(low)    : 都区部CPI等の日本ローカル統計, 市場コメント, 出所はあるが米株影響が薄いもの

選別・スコアはすべてコードで判定し、AIにイベントを創作させない。
"""

import re
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# 市場コメント（イベントではない。常に除外）
_MARKET_COMMENTARY = [
    "連休明け", "週明け", "海外材料", "材料消化", "消化", "様子見",
    "材料探し", "小動き", "続報待ち", "手掛かり難",
]
# 公式に確認できる市場日程（JPでも残す）
_MARKET_DAY_KEEP = ["休場", "祝日", "権利付", "権利落ち", "大納会", "大発会", "メジャーsq", "sq"]
# 日本関連で残してよいキーワード
_JP_KEEP = ["日銀", "日本銀行", "政策決定", "植田", "総裁", "全国", "財務省", "為替介入", "国債入札"] + _MARKET_DAY_KEEP

# 企業決算：半導体/AI・大型ハイテク
_SEMI_AI = ["nvidia", "エヌビディア", "micron", "マイクロン", "broadcom", "ブロードコム",
            "tsmc", "amd", "arm", "asml", "qualcomm", "クアルコム", "半導体", " ai", "ai決算", "ai関連"]
_MEGA = ["apple", "アップル", "microsoft", "マイクロソフト", "amazon", "アマゾン",
         "meta", "tesla", "テスラ", "google", "alphabet", "netflix", "ネットフリックス"]

MIN_SCORE = 45  # これ未満は画像に載せない


def _has(text: str, keys: list[str]) -> bool:
    return any(k in text for k in keys)


def market_impact_score(event: dict) -> tuple[int, str]:
    """米国株式市場への影響度をスコア化。戻り値: (score, reason)。"""
    title = (event.get("title", "") or "").lower()
    cat = event.get("category", "")
    country = event.get("country", "")

    # ===== 最優先：米国マクロ / FRB =====
    if "fomc" in title or (cat == "中銀" and country == "US"):
        return 100, "FOMC/FRB金融政策 → 米株・金利・ドル円に直結"
    if _has(title, ["雇用統計", "非農業", "nonfarm", "payroll"]):
        return 95, "雇用統計 → 金利見通しの最重要材料"
    if "cpi" in title and country != "JP":
        return 95, "米CPI → インフレ・利下げ観測に直結"
    if "pce" in title:
        return 92, "PCE → FRBが最重視するインフレ指標"
    if _has(title, ["小売売上", "retail sales"]):
        return 85, "小売売上高 → 個人消費の体温計"
    if "gdp" in title and country != "JP":
        return 85, "米GDP → 景気の総合指標"
    if _has(title, ["ism", "pmi"]):
        return 82, "ISM/PMI → 景況感の先行指標"
    if "耐久財" in title:
        return 72, "耐久財受注 → 設備投資の手掛かり"
    if _has(title, ["個人所得", "個人消費"]):
        return 72, "個人所得・消費 → 内需の確認"
    if "ミシガン" in title:
        return 70, "ミシガン大指数 → 期待インフレに注目"
    if _has(title, ["失業保険", "jobless"]):
        return 70, "新規失業保険 → 労働市場の週次速報"

    # ===== 企業決算 =====
    if cat == "企業":
        if _has(title, _SEMI_AI):
            return 88, "半導体/AI主要決算 → 指数・テーマ全体に波及"
        if _has(title, _MEGA):
            return 85, "大型ハイテク決算 → 米株指数に影響大"
        return 50, "一般企業決算 → セクター限定の影響"

    # ===== 中優先 =====
    if _has(title, ["住宅着工", "中古住宅", "新築住宅", "住宅販売", "housing", "home sales"]):
        return 55, "住宅指標 → 金利感応セクター"
    if _has(title, ["国債入札", "treasury auction", "入札"]):
        return 55, "米国債入札 → 金利・需給の確認"
    if cat == "発言" and _has(title, ["議長", "理事", "パウエル", "fed", "frb"]):
        return 65, "重要Fed高官発言 → 政策スタンスの手掛かり"
    if "ecb" in title:
        return 65, "ECB → 欧州金利・ユーロ経由で波及"
    if "boe" in title:
        return 60, "BOE → 主要中銀の金融政策"
    if country == "JP" and _has(title, ["日銀", "日本銀行", "植田", "政策決定"]):
        return 65, "日銀/植田 → ドル円・米金利に波及"

    # ===== 日本ローカル等：低 =====
    if country == "JP":
        if "全国" in title:
            return 55, "日本CPI全国 → 円・日銀観測に関連"
        if _has(title, _MARKET_DAY_KEEP):
            return 50, "市場休場/権利日 → 需給に影響"
        return 15, "日本ローカル統計 → 米株影響は薄い"

    return 40, "米株への影響は限定的"


def importance_from_score(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 50:
        return "mid"
    return "low"


def should_include_weekly_event(event: dict) -> tuple[bool, str]:
    """掲載すべきか。戻り値: (掲載するか, 除外理由)。"""
    title = event.get("title", "") or ""
    country = event.get("country", "")

    if _has(title, _MARKET_COMMENTARY):
        return False, "no verified market holiday source"

    has_source = bool(event.get("source_name") or event.get("source_url"))
    if not event.get("verified", False) or not has_source:
        return False, "no verified source"

    if "株主総会" in title:
        return False, "US equity relevance is low"

    if country == "JP" and not _has(title, _JP_KEEP):
        return False, "US equity relevance is low"

    score, _ = market_impact_score(event)
    if score < MIN_SCORE:
        return False, f"low US-equity market impact (score={score})"

    return True, "ok"


def _time_key(event: dict) -> str:
    t = event.get("time_jst", "") or ""
    if t == "早朝":
        return "05:00"
    if "前後" in t:
        t2 = t.replace("前後", "")
        return t2 if re.match(r"^\d{1,2}:\d{2}$", t2) else "20:00"
    if t in ("未定", "時間未定"):
        return "99:99"
    return t if re.match(r"^\d{1,2}:\d{2}$", t) else "98:98"


def select_weekly_events(
    events: list[dict],
    max_total: int = 10,
    max_per_day: int = 3,
) -> list[dict]:
    """
    market_impact_score でスコア化し、score降順で日別/全体の上限まで採用。
    high(>=80)を優先的に確保し、スコアが高い米国株材料を落とさない。
    """
    included: list[dict] = []
    for ev in events:
        ok, reason = should_include_weekly_event(ev)
        if not ok:
            logger.info(f"Excluded weekly event: {ev.get('title','')} / excluded reason: {reason}")
            continue
        score, why = market_impact_score(ev)
        ev = dict(ev)
        ev["impact_score"] = score
        ev["score_reason"] = why
        ev["importance"] = importance_from_score(score)
        included.append(ev)

    # スコア降順（同点は時刻昇順）でグリーディ採用。日別上限と全体上限を守る。
    per_day: dict[str, int] = defaultdict(int)
    selected: list[dict] = []
    for ev in sorted(included, key=lambda e: (-e["impact_score"], _time_key(e))):
        if len(selected) >= max_total:
            break
        d = ev["display_date_jst"]
        if per_day[d] >= max_per_day:
            logger.info(
                f"Excluded weekly event: {ev['title']} / "
                f"excluded reason: per-day cap reached (score={ev['impact_score']})"
            )
            continue
        selected.append(ev)
        per_day[d] += 1

    # 採用ログ（要件8）
    for ev in sorted(selected, key=lambda e: -e["impact_score"]):
        logger.info(
            f"Selected weekly event: {ev['title']} / impact_score={ev['impact_score']} / "
            f"reason: {ev['score_reason']}"
        )

    # 描画用に時系列へ
    selected.sort(key=lambda e: (e["display_date_jst"], _time_key(e)))
    logger.info(
        f"週次イベント選別: 取得{len(events)} → 掲載{len(selected)}件"
        f"（score降順 / 1日最大{max_per_day} / 全体最大{max_total}）"
    )
    return selected
