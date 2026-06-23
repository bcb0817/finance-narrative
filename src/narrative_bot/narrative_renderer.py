"""
narrative_renderer.py
「今日の市場ナラティブ」を1枚のPNGに描画する（Pillow/ダーク）。
top_narrative 1件のみを表示する。3カード表示・最重要テーマ欄は廃止。

入力 top dict:
  title, stance, impact, post_value,
  what, why, market_effect, watch_points[], tickers[]
"""

import os
from PIL import Image, ImageDraw, ImageFont

BG       = (13, 17, 23)
CARD_BG  = (22, 27, 34)
LINE     = (48, 54, 61)
TEXT     = (230, 237, 243)
SUBTLE   = (139, 148, 158)
ACCENT   = (45, 212, 191)
RED      = (248, 81, 73)
GREEN    = (63, 185, 80)
AMBER    = (210, 153, 34)
CHIP_BG  = (33, 38, 45)

STANCE = {
    "強気": (63, 185, 80),
    "弱気": (248, 81, 73),
    "中立": (139, 148, 158),
}

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BLK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"

W = 1080
PAD = 48


def _f(size, bold=False):
    p = FONT_BLK if bold else FONT_REG
    if not os.path.exists(p):
        p = FONT_REG
    return ImageFont.truetype(p, size)


def _wrap(d, text, font, max_w):
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if d.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _chip(d, x, y, text, font):
    w = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + w + 22, y + 34], radius=8, fill=CHIP_BG)
    d.text((x + 11, y + 6), text, font=font, fill=ACCENT)
    return w + 22 + 10


def render_narrative(top: dict, out_path: str) -> str:
    """top_narrative 1件をカード1枚で描画。"""
    img = Image.new("RGB", (W, 2200), BG)
    d = ImageDraw.Draw(img)

    f_h1    = _f(44, True)
    f_meta  = _f(26)
    f_theme = _f(38, True)
    f_label = _f(24, True)
    f_body  = _f(28)
    f_chip  = _f(24)
    f_small = _f(24)

    y = PAD
    d.text((PAD, y), "市場ナラティブ", font=f_h1, fill=TEXT)
    pv = top.get("post_value", "")
    meta = f"投稿価値 {pv}/10"
    mw = d.textlength(meta, font=f_meta)
    d.text((W - PAD - mw, y + 10), meta, font=f_meta, fill=SUBTLE)
    y += 62
    d.line([PAD, y, W - PAD, y], fill=ACCENT, width=3)
    y += 22

    card_top = y
    inner = PAD + 22
    body_w = W - PAD * 2 - 44
    cy = y + 22

    stance = top.get("stance", "中立")
    scol = STANCE.get(stance, SUBTLE)
    bw = d.textlength(stance, font=f_label) + 28
    d.rounded_rectangle([W - PAD - 22 - bw, cy, W - PAD - 22, cy + 38], radius=9, fill=scol)
    d.text((W - PAD - 22 - bw + 14, cy + 6), stance, font=f_label, fill=(8, 12, 14))
    d.text((inner, cy), "テーマ", font=f_small, fill=ACCENT)
    cy += 34
    for ln in _wrap(d, top.get("title", ""), f_theme, body_w - 140):
        d.text((inner, cy), ln, font=f_theme, fill=TEXT)
        cy += 50
    cy += 8

    def section(label, text, color=TEXT):
        nonlocal cy
        d.text((inner, cy), label, font=f_label, fill=ACCENT)
        cy += 36
        for ln in _wrap(d, text, f_body, body_w):
            d.text((inner, cy), ln, font=f_body, fill=color)
            cy += 38
        cy += 12

    section("何が起きているか", top.get("what", ""))
    section("なぜ重要か", top.get("why", ""))
    section("市場への影響", top.get("market_effect", ""))

    wps = (top.get("watch_points", []) or [])[:3]
    if wps:
        d.text((inner, cy), "見るべきポイント", font=f_label, fill=ACCENT)
        cy += 36
        for w in wps:
            d.text((inner + 6, cy), "・", font=f_body, fill=ACCENT)
            for j, ln in enumerate(_wrap(d, w, f_body, body_w - 30)):
                d.text((inner + 34, cy), ln, font=f_body, fill=TEXT)
                cy += 38
        cy += 12

    tickers = top.get("tickers", []) or []
    if tickers:
        d.text((inner, cy), "関連銘柄", font=f_label, fill=ACCENT)
        cy += 36
        cx = inner
        for t in tickers[:6]:
            cx += _chip(d, cx, cy, str(t), f_chip)
            if cx > W - PAD - 140:
                break
        cy += 50

    card_bottom = cy + 10
    d.rounded_rectangle([PAD, card_top, W - PAD, card_bottom], radius=14, outline=LINE, width=1)
    y = card_bottom + 20

    d.text((PAD, y), "※市場全体・主要セクターに影響する材料のみ抽出", font=f_small, fill=SUBTLE)
    y += 42

    img = img.crop((0, 0, W, int(y)))
    img.save(out_path)
    return out_path
