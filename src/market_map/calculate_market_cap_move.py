"""時価総額変化の計算とセクター集計。"""
from __future__ import annotations

import pandas as pd


def calculate_market_cap_move(df: pd.DataFrame):
    """percent_change と market_cap_change を計算し、合計とセクター集計を返す。

    式(仕様どおり):
        percent_change   = current_price / prev_close - 1
        market_cap_change = market_cap * (current_price / prev_close - 1)
                          = market_cap * percent_change

    ※ ここでの market_cap は yfinance の現在値ベース。仕様の式に厳密に従っている。

    Returns:
        (df, total_change, sector_summary)
        - df: percent_change, market_cap_change 列を追加した DataFrame
        - total_change: 全銘柄合計の時価総額増減(float, USD)
        - sector_summary: セクター別 market_cap_change 合計(昇順=売り先導が先頭)
    """
    df = df.copy()

    df["percent_change"] = df["current_price"] / df["prev_close"] - 1.0
    df["market_cap_change"] = df["market_cap"] * df["percent_change"]

    total_change = float(df["market_cap_change"].sum())

    sector_summary = (
        df.groupby("sector")["market_cap_change"]
        .sum()
        .sort_values()  # 昇順: 先頭が最も売られたセクター、末尾が最も買われたセクター
        .reset_index()
    )

    return df, total_change, sector_summary
