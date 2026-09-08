from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common.runtime import output_dir, JST

from .models import FxBar, FxMovement


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in ("C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def create_chart(bars: list[FxBar], movement: FxMovement, *, width: int = 1600, height: int = 900) -> tuple[Path, Path]:
    if len(bars) < 2:
        raise ValueError("at least two bars are required")
    cutoff = movement.detected_at - timedelta(minutes={"5m": 5, "15m": 15, "1h": 60, "4h": 240, "24h": 1440}[movement.window])
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    prior = [bar for bar in ordered if bar.timestamp <= cutoff]
    bars = prior[-1:] + [bar for bar in ordered if cutoff < bar.timestamp <= movement.detected_at]
    if len(bars) < 2:
        raise ValueError("insufficient bars within movement window")
    folder = output_dir("fx_charts") / movement.detected_at.astimezone().strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"fx_{movement.pair}_{movement.movement_id}"
    image_path = folder / f"{stem}.png"
    metadata_path = folder / f"{stem}.json"
    image = Image.new("RGB", (width, height), "#08111f")
    draw = ImageDraw.Draw(image)
    margin_x, top, bottom = 110, 280, 195
    plot_width, plot_height = width - margin_x * 2, height - top - bottom
    closes = [item.close for item in bars]
    low, high = min(closes), max(closes)
    pad = max((high - low) * 0.12, 0.05)
    low, high = low - pad, high + pad

    def point(index: int, price: float) -> tuple[float, float]:
        elapsed = (bars[index].timestamp - bars[0].timestamp).total_seconds()
        duration = (bars[-1].timestamp - bars[0].timestamp).total_seconds()
        x = margin_x + elapsed * plot_width / max(duration, 1)
        y = top + (high - price) / max(high - low, 0.00001) * plot_height
        return x, y

    for index in range(5):
        price = low + (high - low) * index / 4
        y = point(0, price)[1]
        draw.line((margin_x, y, width - margin_x, y), fill="#243247", width=2)
        draw.text((20, y - 15), f"{price:.2f}", fill="#9fb0c5", font=_font(25))
    color = "#ff606d" if movement.direction == "up" else "#4da3ff"
    points = [point(index, value) for index, value in enumerate(closes)]
    draw.line(points, fill=color, width=8, joint="curve")
    draw.ellipse((*[value - 10 for value in points[-1]], *[value + 10 for value in points[-1]]), fill="#ffffff")
    start_point = point(0, movement.start_price)
    draw.line((margin_x, start_point[1], width-margin_x, start_point[1]), fill="#ffd166", width=3)
    draw.text((margin_x+10, start_point[1]-32), f"基準 {movement.start_price:.3f}", fill="#ffd166", font=_font(23))
    label = "ドル円 " if movement.pair == "USDJPY" else ""
    draw.text((margin_x, 25), f"{label}{movement.pair[:3]}/{movement.pair[3:]}", fill="#ffffff", font=_font(48))
    draw.text((margin_x, 85), f"{movement.end_price:.3f}", fill="#ffffff", font=_font(140))
    draw.text((900, 145), movement.direction_ja + "が進行", fill=color, font=_font(50))
    sign = "+" if movement.change_yen >= 0 else ""
    draw.text((margin_x, height-185), f"{sign}{movement.change_pct:.2f}%", fill=color, font=_font(72))
    draw.text((640, height-155), f"{movement.window} / {sign}{movement.change_yen:.2f}円", fill=color, font=_font(38))
    high_low = f"{bars[0].timestamp.astimezone(JST):%m/%d %H:%M} → {movement.detected_at.astimezone(JST):%m/%d %H:%M} JST / 高 {max(closes):.2f} 安 {min(closes):.2f}"
    draw.text((margin_x, height - 95), high_low, fill="#b5c1d1", font=_font(25))
    draw.text((margin_x, height - 55), f"Source: {movement.data_source} / 情報提供目的", fill="#8191a7", font=_font(25))
    image.save(image_path, format="PNG", optimize=True)
    content_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    metadata = {
        "movement": movement.to_dict(),
        "bars": [item.to_dict() for item in bars],
        "generated_at": datetime.now().astimezone().isoformat(),
        "width": width,
        "height": height,
        "movement_id": movement.movement_id,
        "pair": movement.pair,
        "chart_period": movement.window,
        "interval": bars[-1].interval,
        "points": len(bars),
        "current_price": movement.end_price,
        "change": movement.change_yen,
        "change_percent": movement.change_pct,
        "high": max(item.high for item in bars),
        "low": min(item.low for item in bars),
        "source": movement.data_source or bars[-1].provider,
        "file_path": str(image_path),
        "content_hash": content_hash,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    movement.chart_path = str(image_path)
    return image_path, metadata_path
