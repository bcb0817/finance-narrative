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
from .fetch_market_data import fetch_market_data
from .generate_headline import make_caption, make_headline
from .post_to_x import post_to_x

logger = logging.getLogger(__name__)

__all__ = ["generate_market_map_post", "post_to_x"]


def generate_market_map_post(out_path: str = "market_map.png") -> dict:
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
    df = fetch_market_data()
    df, total_change, sector_summary = calculate_market_cap_move(df)

    headline = make_headline(total_change)
    caption = make_caption(df, total_change, sector_summary)

    image_path: str | None = None
    try:
        image_path = build_treemap(df, headline, out_path=out_path)
    except Exception as e:  # noqa: BLE001
        # 仕様11: 画像生成失敗でも落とさず、テキストのみで返す
        logger.warning("treemap 生成に失敗、画像なしで返却: %s", e)
        image_path = None

    return {"headline": headline, "caption": caption, "image_path": image_path}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = generate_market_map_post()
    print(result["headline"])
    print("-" * 50)
    print(result["caption"])
    print("-" * 50)
    print("image:", result["image_path"])
