"""
common/fonts.py
ローカル環境向けの日本語フォント解決。
優先順: FONT_PATH 環境変数 → Linux Noto CJK → macOS ヒラギノ → Windows Yu Gothic/Meiryo
→ DejaVu → Pillow デフォルト（クラッシュせず警告のみ）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import ImageFont

logger = logging.getLogger(__name__)

_CANDIDATES_REG = [
    # Linux (Noto CJK)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    # macOS（ヒラギノ。機種/OSで場所差があるため複数）
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/AquaKana.ttc",
    "/Library/Fonts/ヒラギノ角ゴ ProN W3.otf",
    "/Library/Fonts/Osaka.ttf",
    "/System/Library/Fonts/Supplemental/Osaka.ttf",
    # Windows
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    # 汎用フォールバック（日本語は出ないが落ちない）
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_warned = False


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """日本語対応フォントを解決して返す。見つからなければPillow既定で警告。"""
    global _warned
    env_path = os.environ.get("FONT_PATH", "").strip()
    candidates = ([env_path] if env_path else []) + (
        _CANDIDATES_BOLD if bold else _CANDIDATES_REG
    ) + (_CANDIDATES_REG if bold else [])  # bold未発見時はRegularへ落とす
    for p in candidates:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    if not _warned:
        logger.warning(
            "日本語フォントが見つかりません。FONT_PATH を設定してください"
            "（画像内の日本語が正しく描画されない可能性があります）。"
        )
        _warned = True
    return ImageFont.load_default()
