"""market_map パッケージ。

既存Botからは基本的にこれだけを import すればよい:

    from market_map import generate_market_map_post, post_to_x

    post = generate_market_map_post()
    if post["image_path"]:
        post_to_x(post["caption"], post["image_path"])
    else:
        run_normal_post()   # 通常投稿にフォールバック
"""
from __future__ import annotations

import logging

from .build_treemap import build_treemap
from .calculate_market_cap_move import calculate_market_cap_move
from .fetch_market_data import fetch_index_snapshot, fetch_market_data
from .generate_headline import make_caption, make_headline, make_reversal_headline
from .post_to_x import post_to_x

logger = logging.getLogger(__name__)

__all__ = ["generate_market_map_post", "post_to_x"]


def generate_market_map_post(out_path: str = "market_map.png", session: str = "open") -> dict:
    """寄り付きヒートマップ投稿の素材を生成する。

    Returns:
        {
            "headline":   str,
            "caption":    str,
            "image_path": str | None,   # 画像生成失敗時は None
        }

    仕様11: 画像生成に失敗してもBotを落とさない。
    image_path が None の場合、呼び出し側Botは通常投稿にフォールバックすること。
    """
    df = fetch_market_data(session=session)
    df, total_change, sector_summary = calculate_market_cap_move(df)

    # 投稿ゲート用の指標
    total_mcap = float(df["market_cap"].sum()) or 1.0
    total_pct = total_change / total_mcap * 100.0  # 指数近似の変化率(%)
    # セクター偏り: |変化|合計に占める最大セクターの割合（0〜1）
    # calculate_market_cap_move returns a two-column frame after reset_index().
    # Only the numeric movement column participates in the concentration metric.
    abs_by_sector = sector_summary.set_index("sector")["market_cap_change"].abs()
    skew = float(abs_by_sector.max() / abs_by_sector.sum()) if float(abs_by_sector.sum()) else 0.0
    top_sector = str(abs_by_sector.idxmax()) if len(abs_by_sector) else ""

    changes = df["percent_change"].dropna() if "percent_change" in df else None
    advancers = int((changes > 0).sum()) if changes is not None else 0
    decliners = int((changes < 0).sum()) if changes is not None else 0
    breadth_total = advancers + decliners
    breadth_ratio = advancers / breadth_total if breadth_total else 0.5

    max_sector_pct = 0.0
    max_sector_pct_name = ""
    if {"sector", "market_cap"}.issubset(df.columns):
        sector_caps = df.groupby("sector")["market_cap"].sum()
        sector_moves = sector_summary.set_index("sector")["market_cap_change"]
        sector_pcts = (sector_moves / sector_caps.reindex(sector_moves.index) * 100.0).dropna()
        if len(sector_pcts):
            max_sector_pct_name = str(sector_pcts.abs().idxmax())
            max_sector_pct = float(sector_pcts.loc[max_sector_pct_name])

    index_snapshot = fetch_index_snapshot() if session == "pre_close" else {}
    current_pct = float(index_snapshot.get("current_pct", 0.0) or 0.0)
    high_pct = float(index_snapshot.get("day_high_pct", 0.0) or 0.0)
    low_pct = float(index_snapshot.get("day_low_pct", 0.0) or 0.0)
    intraday_reversal_pct = 0.0
    if high_pct > 0.0 and current_pct <= 0.0:
        intraday_reversal_pct = current_pct - high_pct
    elif low_pct < 0.0 and current_pct >= 0.0:
        intraday_reversal_pct = current_pct - low_pct

    if intraday_reversal_pct:
        headline = make_reversal_headline(intraday_reversal_pct)
    else:
        headline = make_headline(total_change, session=session)
    caption = make_caption(
        df,
        total_change,
        sector_summary,
        session=session,
        reversal_pct=intraday_reversal_pct,
        index_current_pct=current_pct,
    )

    image_path: str | None = None
    try:
        image_path = build_treemap(df, headline, out_path=out_path)
    except Exception as e:  # noqa: BLE001
        # 仕様11: 画像生成失敗でも落とさず、テキストのみで返す
        logger.warning("treemap 生成に失敗、画像なしで返却: %s", e)
        image_path = None

    return {"headline": headline, "caption": caption, "image_path": image_path,
            "total_change": total_change, "total_pct": total_pct,
            "sector_skew": skew, "top_sector": top_sector,
            "advancers": advancers, "decliners": decliners,
            "breadth_ratio": breadth_ratio,
            "max_sector_pct": max_sector_pct,
            "max_sector_pct_name": max_sector_pct_name,
            "intraday_reversal_pct": intraday_reversal_pct,
            "index_current_pct": current_pct,
            "session": session}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = generate_market_map_post()
    print(result["headline"])
    print("-" * 50)
    print(result["caption"])
    print("-" * 50)
    print("image:", result["image_path"])
