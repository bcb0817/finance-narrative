from __future__ import annotations

from datetime import datetime
from common.runtime import JST


def radar_fixture(now: datetime | None = None) -> dict:
    observed = (now or datetime.now(JST)).astimezone(JST).isoformat()
    specs = [
        ("NVIDIA / AI Semiconductor", ["NVDA", "AMD", "AVGO"], "AI / SEMICONDUCTOR", 92, 88, "AIインフラ投資と次世代GPUの議論が今回の検索結果で目立っています。", "AI設備投資の継続性", "投資回収期間への懸念", "強い需要観測を将来の株価保証と捉えないこと"),
        ("Google / AI Capex", ["GOOGL"], "US STOCKS", 78, 72, "AI向け設備投資と収益化を巡る議論が観測されています。", "設備投資の増加とクラウド需要", "利益率への短期的な圧力", "設備投資額だけで成果を断定できないこと"),
        ("USDJPY / BOJ", ["USDJPY"], "FX", 74, 81, "日銀政策とドル円の反応について議論が強まっている可能性があります。", "政策正常化の速度", "金利差が円安を支えるとの見方", "観測投稿は介入の事実確認ではないこと"),
        ("US Rates / Federal Reserve", ["US10Y"], "MACRO", 64, 48, "米金利とFRBの政策経路が今回の検索結果で議論されています。", "利下げ時期とインフレ鈍化", "高金利の長期化", "単一指標で政策決定は決まらないこと"),
        ("Japan Stocks / Semiconductor", ["6857", "8035"], "JAPAN STOCKS", 51, 31, "日本の半導体関連株と海外需要を巡る観測が見られます。", "AI関連需要の波及", "為替とバリュエーションへの懸念", "米国半導体株と常に同方向とは限らないこと"),
    ]
    topics = []
    for index, (title, tickers, category, velocity, acceleration, summary, dominant, dissent, misconception) in enumerate(specs, 1):
        handle = ["nvidia", "Google", "Bank_of_Japan_e", "federalreserve", "meti_NIPPON"][index - 1]
        topics.append({"topic_id": f"fixture-{index}", "title": title, "canonical_topic": title.lower(), "tickers": tickers,
            "entities": [], "category": category, "summary": summary, "observed_velocity_score": velocity,
            "observed_acceleration_score": acceleration, "reliability": "medium", "official_source_present": index in {1,2,3,4,5},
            "source_confirmation_status": "official_source_observed", "dominant_narrative": dominant, "dissent": dissent,
            "misconception": misconception, "representative_posts": [{"post_id": str(1000+index), "handle": handle,
            "account_type": "official", "url": f"https://x.com/{handle}/status/{1000+index}",
            "short_summary": "この論点について言及", "why_selected": "関連する公式アカウントの観測例"}], "updated_at": observed})
    return {"run_id": "fixture-x-radar", "observed_at": observed, "generated_at": observed, "topics": topics}
