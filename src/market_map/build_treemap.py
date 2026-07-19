"""Render a market-cap weighted heatmap with company logos."""
from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
import squarify
from PIL import Image, ImageDraw

from .ticker_domains import TICKER_DOMAINS

logger = logging.getLogger(__name__)

BG = (7, 11, 18)
RED = (239, 48, 64)
NEUTRAL = (45, 55, 72)
GREEN = (0, 200, 120)
WHITE = (255, 255, 255)
TILE_BORDER = (9, 14, 23)

CLEARBIT = "https://logo.clearbit.com/{domain}"
GOOGLE_FAVICON = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
LOGO_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "logo_cache"


def _font(size: int, bold: bool = False):
    from common.fonts import get_font
    return get_font(size, bold)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _color_for(pct: float, clip: float):
    """Map returns to vivid red/green while preserving intensity."""
    if clip <= 0:
        return NEUTRAL
    p = max(-clip, min(clip, float(pct)))
    if abs(p) < 0.0001:
        return NEUTRAL
    strength = 0.28 + 0.72 * ((abs(p) / clip) ** 0.5)
    return _lerp(NEUTRAL, GREEN if p > 0 else RED, min(1.0, strength))


def _normalize_domain(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("https://", "").replace("http://", "").split("/")[0].strip()


def _fetch_logo(domain: str, ticker: str = "") -> Image.Image | None:
    """Load a cached logo, falling back from Clearbit to a domain favicon."""
    safe_name = "".join(c for c in (ticker or domain) if c.isalnum() or c in "-_").lower()
    if not safe_name:
        return None
    cache_path = LOGO_CACHE_DIR / f"{safe_name}.png"
    if cache_path.exists():
        try:
            return Image.open(cache_path).convert("RGBA")
        except OSError:
            logger.debug("invalid logo cache: %s", cache_path)

    headers = {"User-Agent": "finance-narrative-market-map/1.0"}
    urls = (
        CLEARBIT.format(domain=domain),
        GOOGLE_FAVICON.format(domain=domain),
    )
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code != 200 or not resp.content:
                continue
            logo = Image.open(BytesIO(resp.content)).convert("RGBA")
            if logo.width < 16 or logo.height < 16:
                continue
            LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            logo.save(cache_path, format="PNG")
            return logo
        except Exception as exc:  # noqa: BLE001
            logger.debug("logo fetch failed for %s via %s: %s", domain, url, exc)
    return None


def _text_size(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def build_treemap(
    df: pd.DataFrame,
    headline: str,
    out_path: str = "market_map.png",
    color_clip: float = 0.04,
    label_top_n: int = 60,
    logo_top_n: int = 40,
    width: int = 1600,
    height: int = 900,
    header_h: int = 80,
) -> str:
    """Create and save a market-cap weighted treemap PNG."""
    df = df.sort_values("market_cap", ascending=False).reset_index(drop=True)
    area_w, area_h = width, height - header_h
    norm = squarify.normalize_sizes(df["market_cap"].tolist(), area_w, area_h)
    rects = squarify.squarify(norm, 0, header_h, area_w, area_h)

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    draw.text((20, 22), headline, font=_font(34, bold=True), fill=WHITE)

    for i, rect in enumerate(rects):
        row = df.iloc[i]
        x0, y0 = rect["x"], rect["y"]
        w, h = rect["dx"], rect["dy"]
        x1, y1 = min(x0 + w, width), min(y0 + h, height)
        draw.rectangle(
            [x0, y0, x1, y1],
            fill=_color_for(row.percent_change, color_clip),
            outline=TILE_BORDER,
            width=2,
        )
        if w < 28 or h < 22:
            continue

        cx, cy = x0 + w / 2, y0 + h / 2
        pct_text = f"{row.percent_change * 100:+.1f}%"
        logo_pasted = False
        if i < logo_top_n and w >= 64 and h >= 64:
            explicit_domain = _normalize_domain(getattr(row, "logo_url", ""))
            domain = explicit_domain or TICKER_DOMAINS.get(str(row.ticker), "")
            if domain:
                logo = _fetch_logo(domain, str(row.ticker))
                if logo is not None:
                    target = max(20, int(min(w, h) * 0.34))
                    logo.thumbnail((target, target))
                    lx = int(cx - logo.width / 2)
                    ly = int(cy - logo.height / 2 - h * 0.12)
                    pad = max(4, target // 10)
                    draw.rounded_rectangle(
                        [lx - pad, ly - pad, lx + logo.width + pad, ly + logo.height + pad],
                        radius=max(5, pad * 2),
                        fill=(255, 255, 255),
                    )
                    img.paste(logo, (lx, ly), logo)
                    logo_pasted = True

        if i < label_top_n:
            fsize = max(10, min(20, int(w / 6)))
            ticker_font = _font(fsize, bold=True)
            pct_font = _font(max(9, fsize - 2))
            ticker = str(row.ticker)
            tw, th = _text_size(draw, ticker, ticker_font)
            pw, ph = _text_size(draw, pct_text, pct_font)
            ty = int(cy + h * 0.10) if logo_pasted else int(cy - (th + ph) / 2)
            if tw <= w - 4 and (th + ph) <= h - 4:
                draw.text((cx - tw / 2, ty), ticker, font=ticker_font, fill=WHITE)
                draw.text((cx - pw / 2, ty + th + 2), pct_text, font=pct_font, fill=WHITE)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    logger.info("treemap image saved: %s", out)
    return str(out)
