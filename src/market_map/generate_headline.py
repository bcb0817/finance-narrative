"""見出し(headline)と投稿文(caption)の生成。

- 投稿本文(caption)は日本語
- 画像上部の見出し(headline)はデフォルト英語(Kalshi風)。
  画像も日本語にしたい場合は make_headline_jp() を使う(末尾コメント参照)。
"""
from __future__ import annotations

import pandas as pd

# GICSセクター(英語) -> 日本語
SECTOR_JP = {
    "Information Technology": "情報技術",
    "Health Care": "ヘルスケア",
    "Financials": "金融",
    "Consumer Discretionary": "一般消費財",
    "Communication Services": "通信サービス",
    "Industrials": "資本財",
    "Consumer Staples": "生活必需品",
    "Energy": "エネルギー",
    "Utilities": "公益事業",
    "Real Estate": "不動産",
    "Materials": "素材",
}


def _sector_jp(name: str) -> str:
    return SECTOR_JP.get(name, name)


def format_usd(value: float) -> str:
    """英語見出し用: $1.5T / $850B / $120M 形式(絶対値)。"""
    v = abs(value)
    if v >= 1e12:
        return f"${v / 1e12:.1f}T"
    if v >= 1e9:
        return f"${v / 1e9:.0f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def format_usd_jp(value: float) -> str:
    """日本語用: 1.5兆ドル / 8500億ドル 形式(絶対値)。"""
    v = abs(value)
    if v >= 1e12:
        return f"{v / 1e12:.1f}兆ドル"
    if v >= 1e8:
        return f"{v / 1e8:,.0f}億ドル"
    return f"{v:,.0f}ドル"


def _signed_jp(value: float) -> str:
    """符号付き日本語金額: +1200億ドル / -8500億ドル。"""
    sign = "+" if value >= 0 else "-"
    return f"{sign}{format_usd_jp(value)}"


def make_headline(total_change: float, session: str = "open") -> str:
    """画像上部用の英語見出し(Kalshi風)。

    例: JUST IN: $1.5T erased from the S&P 500 at the open
    """
    verb = "erased from" if total_change < 0 else "added to"
    timing = "near the close" if session == "pre_close" else "at the open"
    return f"JUST IN: {format_usd(total_change)} {verb} the S&P 500 {timing}"


def make_reversal_headline(reversal_pct: float) -> str:
    """前日終値をまたぐ大幅な日中反転用の画像見出し。"""
    if reversal_pct < 0:
        return "JUST IN: S&P 500 erases all gains and turns red"
    return "JUST IN: S&P 500 recovers all losses and turns green"


def make_headline_jp(total_change: float) -> str:
    """画像上部を日本語にしたい場合の見出し。

    例: 【速報】寄り付きでS&P500の時価総額が約1.5兆ドル消失
    """
    verb = "消失" if total_change < 0 else "増加"
    return f"【速報】寄り付きでS&P500の時価総額が約{format_usd_jp(total_change)}{verb}"


def make_caption(
    df: pd.DataFrame,
    total_change: float,
    sector_summary: pd.DataFrame,
    n_movers: int = 5,
    session: str = "open",
    reversal_pct: float = 0.0,
    index_current_pct: float = 0.0,
) -> str:
    """日本語の投稿文を生成する。

    含める要素:
        - 全体の時価総額変化
        - 売り/買いの中心セクター
        - 主な下落銘柄
        - ハッシュタグ少なめ

    ※ X は1投稿につき cashtag($SYMBOL)を最大1つまで。ティッカーに $ は付けない。
    """
    direction = "消失" if total_change < 0 else "増加"
    changes = df["percent_change"].dropna()
    advancers = int((changes > 0).sum())
    decliner_count = int((changes < 0).sum())
    breadth_total = advancers + decliner_count
    breadth_ratio = advancers / breadth_total if breadth_total else 0.5
    total_mcap = float(df["market_cap"].sum()) if "market_cap" in df else 0.0
    total_pct = total_change / total_mcap * 100.0 if total_mcap else 0.0
    rotation = (
        breadth_total > 0
        and (breadth_ratio >= 0.7 or breadth_ratio <= 0.3)
        and abs(total_pct) < 1.0
    )

    # 売り/買いの中心セクター(sector_summary は昇順)
    worst_sector = sector_summary.iloc[0]
    best_sector = sector_summary.iloc[-1]

    # 主な下落銘柄(下落率の大きい順)。cashtag制限のため $ は付けない
    decliners = (
        df[df["percent_change"] < 0]
        .sort_values("percent_change")
        .head(n_movers)
    )
    mover_lines = "、".join(
        f"{r.ticker} {r.percent_change * 100:+.1f}%" for r in decliners.itertuples()
    )

    timing = "取引終了直前" if session == "pre_close" else "寄り付き"
    if reversal_pct < 0:
        opening = (
            f"【速報】S&P500は日中の上昇分を全て失い、"
            f"前日比{index_current_pct:+.2f}%へ反転"
            f"（高値から{abs(reversal_pct):.2f}ポイント低下）。"
        )
    elif reversal_pct > 0:
        opening = (
            f"【速報】S&P500は日中の下落分を取り戻し、"
            f"前日比{index_current_pct:+.2f}%へ反転"
            f"（安値から{abs(reversal_pct):.2f}ポイント上昇）。"
        )
    else:
        opening = f"【速報】{timing}でS&P500の時価総額が約{format_usd_jp(total_change)}{direction}。"

    lines = [
        opening,
        "",
        f"売り主導：{_sector_jp(worst_sector['sector'])}（{_signed_jp(worst_sector['market_cap_change'])}）",
        f"買い主導：{_sector_jp(best_sector['sector'])}（{_signed_jp(best_sector['market_cap_change'])}）",
        f"騰落銘柄：上昇 {advancers}（{breadth_ratio:.1%}）／下落 {decliner_count}",
    ]
    if rotation:
        lines += ["", "指数以上に内部差が大きい、セクターローテーション相場。"]
    if mover_lines:
        lines += ["", f"主な下落：{mover_lines}"]
    lines += ["", "#米国株 #SP500"]

    return "\n".join(lines)
