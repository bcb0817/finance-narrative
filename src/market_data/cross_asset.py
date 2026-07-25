from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .models import CrossAssetSignal


def classify_cross_asset(changes: dict[str, float], *, detected_at: datetime | None = None) -> CrossAssetSignal:
    now = detected_at or datetime.now(timezone.utc)
    qqq = changes.get("QQQ", 0.0)
    spy = changes.get("SPY", 0.0)
    smh = changes.get("SMH", changes.get("SOXX", 0.0))
    tlt = changes.get("TLT", 0.0)
    gld = changes.get("GLD", 0.0)
    usdjpy = changes.get("USD/JPY", 0.0)
    btc = changes.get("BTC/USD", 0.0)
    uso = changes.get("USO", 0.0)
    pattern = "unknown"
    interpretation = "複数資産の方向が揃わず、明確な市場局面は確認できません。"
    confidence = "unknown"
    if spy <= -1 and qqq <= -1 and gld >= 0.5:
        pattern, confidence = "risk_off", "likely"
        interpretation = "株式安と金上昇が同時に観測されていますが、因果関係は未確認です。"
    elif spy >= 1 and qqq >= 1 and btc >= 1:
        pattern, confidence = "risk_on", "likely"
        interpretation = "株式と暗号資産が同方向に上昇していますが、共通原因は未確認です。"
    elif tlt <= -1 and qqq <= -1:
        pattern, confidence = "yield_shock", "possible"
        interpretation = "長期債ETFとNASDAQ ETFが同時に下落し、金利要因が意識された可能性があります。"
    elif usdjpy >= 0.7 and gld <= -0.5:
        pattern, confidence = "dollar_strength", "possible"
        interpretation = "ドル高方向の動きが複数資産に表れていますが、原因の確認は取れていません。"
    elif usdjpy <= -0.7 and gld >= 0.5:
        pattern, confidence = "dollar_weakness", "possible"
        interpretation = "ドル安方向と金上昇が同時に観測されていますが、共通原因は未確認です。"
    elif uso >= 2 and tlt <= -0.7:
        pattern, confidence = "inflation_shock", "possible"
        interpretation = "エネルギーETF上昇と長期債ETF下落が同時に観測されています。インフレ要因かは未確認です。"
    elif abs(uso) >= 3:
        pattern, confidence = "commodity_shock", "possible"
        interpretation = "エネルギーETFに大きな変動がありますが、背景材料は未確認です。"
    elif smh <= qqq - 1.0:
        pattern, confidence = "semiconductor_specific", "likely"
        interpretation = "NASDAQ全体より半導体ETFの下落が大きく、半導体固有の弱さが観測されています。"
    elif abs(btc) >= 4 and abs(qqq) < 1:
        pattern, confidence = "crypto_specific", "possible"
        interpretation = "暗号資産だけに大きな値動きがあり、株式市場との共通性は確認できません。"
    elif abs(qqq - btc) >= 3:
        pattern, confidence = "divergence", "possible"
        interpretation = "NASDAQ ETFと暗号資産の値動きに大きな乖離があります。"
    raw = f"{pattern}:{now.replace(second=0,microsecond=0).isoformat()}:{sorted(changes.items())}"
    return CrossAssetSignal(
        signal_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        detected_at=now, primary_symbol=max(changes, key=lambda key: abs(changes[key]), default=""),
        related_symbols=sorted(changes),
        pattern_type=pattern, movements=changes,
        likely_interpretation=interpretation,
        alternative_interpretations=["個別材料", "流動性要因", "時差のある価格反映"],
        confidence=confidence,
    )
