"""market_cap 重み付けの treemap ヒートマップ画像を生成する。

- タイルサイズ: market_cap
- カラー:       percent_change(下落=赤 / 上昇=緑)
- タイル内:     ticker + percent_change(大きい銘柄のみ)
- 上部:         見出し(headline)
- ロゴ:         logo_url 列を保持しているので後から追加しやすい(下部コメント参照)

画像出力には plotly + kaleido を使用。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# 下落=赤 / 中立=ダーク / 上昇=緑
COLORSCALE = [
    [0.0, "#b91c1c"],  # 強い下落
    [0.5, "#1f2937"],  # 中立
    [1.0, "#15803d"],  # 強い上昇
]


def build_treemap(
    df: pd.DataFrame,
    headline: str,
    out_path: str = "market_map.png",
    color_clip: float = 0.04,
    label_top_n: int = 60,
    width: int = 1600,
    height: int = 900,
) -> str:
    """treemap PNG を生成して out_path を返す。

    Args:
        df: ticker, company_name, market_cap, percent_change を含む DataFrame
        headline: 画像上部に出す見出し
        color_clip: 色付けの上下限(±4%)。極端値で配色が潰れるのを防ぐ
        label_top_n: ラベル表示する銘柄数(時価総額上位 N 銘柄のみ)
    """
    df = df.copy()

    # ラベルは大きい銘柄(時価総額上位 label_top_n)だけ表示する
    big = set(df.nlargest(label_top_n, "market_cap")["ticker"])

    def _label(row) -> str:
        if row.ticker in big:
            return f"<b>{row.ticker}</b><br>{row.percent_change * 100:+.1f}%"
        return ""

    df["tile_text"] = [_label(r) for r in df.itertuples()]

    # 色は ±color_clip にクリップ(表示テキストは実際の値のまま)
    color_value = df["percent_change"].clip(-color_clip, color_clip)

    fig = go.Figure(
        go.Treemap(
            labels=df["ticker"],
            parents=[""] * len(df),
            values=df["market_cap"],
            text=df["tile_text"],
            textinfo="text",
            textfont=dict(size=14, color="white", family="Arial"),
            marker=dict(
                colors=color_value,
                colorscale=COLORSCALE,
                cmid=0.0,
                cmin=-color_clip,
                cmax=color_clip,
                line=dict(width=1, color="#0b0f17"),
            ),
            tiling=dict(pad=1),
            customdata=np.stack(
                [df["company_name"], df["percent_change"] * 100], axis=-1
            ),
            hovertemplate="%{customdata[0]}<br>%{label}: %{customdata[1]:+.2f}%<extra></extra>",
        )
    )

    fig.update_layout(
        title=dict(
            text=headline,
            x=0.01,
            xanchor="left",
            font=dict(size=30, color="white", family="Arial Black"),
        ),
        margin=dict(t=70, l=10, r=10, b=10),
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
    )

    fig.write_image(out_path, width=width, height=height, scale=2)
    logger.info("treemap 画像を出力: %s", out_path)
    return out_path


# -------------------------------------------------------------------
# ロゴ追加用フック(後から実装しやすくするためのメモ)
# -------------------------------------------------------------------
# df["logo_url"] にロゴURL(またはローカルパス)を入れておけば、
# 出力済み PNG を Pillow で開き、各タイル中心にロゴを貼る後処理を追加できる。
# タイル座標が必要な場合は go.Treemap ではなく squarify ライブラリで
# レイアウトを自前計算する方式に切り替えると座標を直接得られる。
