from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common.runtime import JST, output_dir

from .models import MarketBar, MarketMovement


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in ("C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def create_market_chart(
    bars: list[MarketBar], movement: MarketMovement, *,
    display_name: str = "", delayed: bool = False,
    width: int = 1600, height: int = 900,
) -> tuple[Path, Path]:
    if len(bars) < 2:
        raise ValueError("at least two bars are required")
    folder = output_dir("market_charts") / movement.detected_at.astimezone(JST).strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"market_{movement.symbol.replace('/','')}_{movement.movement_id}"
    image_path, metadata_path = folder / f"{stem}.png", folder / f"{stem}.json"
    image = Image.new("RGB", (width, height), "#08111f")
    draw = ImageDraw.Draw(image)
    margin, top, bottom = 110, 190, 230
    plot_width, plot_height = width - margin * 2, height - top - bottom
    closes = [bar.close for bar in bars]
    low, high = min(closes), max(closes)
    # Keep at least a 2% price span so quiet moves are not visually exaggerated.
    observed_range = high - low
    minimum_range = abs(closes[-1]) * 0.02
    if observed_range < minimum_range:
        center = (high + low) / 2
        low, high = center - minimum_range / 2, center + minimum_range / 2
    raw_range = high - low
    low -= raw_range * 0.15
    high += raw_range * 0.15

    def point(index: int, price: float) -> tuple[float, float]:
        return (
            margin + index * plot_width / max(len(bars) - 1, 1),
            top + (high - price) / max(high - low, 1e-9) * plot_height,
        )

    for index in range(5):
        value = low + (high - low) * index / 4
        y = point(0, value)[1]
        draw.line((margin, y, width - margin, y), fill="#26364d", width=2)
        draw.text((18, y - 14), f"{value:,.2f}", fill="#9fb0c5", font=_font(24))
    color = "#4da3ff" if movement.direction == "down" else "#ff606d"
    points = [point(index, value) for index, value in enumerate(closes)]
    draw.line(points, fill=color, width=8, joint="curve")
    draw.ellipse((points[-1][0]-10, points[-1][1]-10, points[-1][0]+10, points[-1][1]+10), fill="#ffffff")
    start_index = min(
        range(len(bars)),
        key=lambda index: abs(
            (bars[index].timestamp - (
                movement.detected_at.astimezone(bars[index].timestamp.tzinfo)
                - timedelta(minutes=movement.window_minutes)
            )).total_seconds()
        ),
    )
    start_x = points[start_index][0]
    draw.line((start_x, top, start_x, top + plot_height), fill="#f5b942", width=3)
    draw.text((start_x + 8, top + 8), "急変開始", fill="#f5b942", font=_font(22))
    draw.line((points[-1][0], top, points[-1][0], top + plot_height), fill="#ffffff", width=3)
    draw.text((points[-1][0] - 140, top + 42), "急変検知", fill="#ffffff", font=_font(22))
    title = f"{movement.symbol}  {display_name}".strip()
    draw.text((margin, 35), title, fill="#ffffff", font=_font(50))
    sign = "+" if movement.percentage_change >= 0 else ""
    subtitle = (
        f"{movement.window_minutes}分  {movement.start_price:,.2f} → {movement.current_price:,.2f}  "
        f"{sign}{movement.absolute_change:,.2f} ({sign}{movement.percentage_change:.2f}%)"
    )
    draw.text((margin, 105), subtitle, fill=color, font=_font(35))
    if delayed:
        draw.rounded_rectangle((width-390, 35, width-110, 105), 12, fill="#8b5a00")
        draw.text((width-355, 50), "遅延データ", fill="#ffffff", font=_font(32))
    volumes = [float(bar.volume or 0) for bar in bars]
    maximum_volume = max(volumes, default=0)
    volume_top, volume_bottom = height - 190, height - 125
    if maximum_volume:
        bar_width = max(1, int(plot_width / max(len(bars), 1)))
        for index, volume in enumerate(volumes):
            x = margin + index * plot_width / max(len(bars) - 1, 1)
            bar_height = (volume / maximum_volume) * (volume_bottom - volume_top)
            draw.rectangle(
                (x, volume_bottom - bar_height, x + bar_width, volume_bottom),
                fill="#38536f",
            )
        draw.text((margin, volume_top - 26), "Volume", fill="#9fb0c5", font=_font(20))
    rv = "-" if movement.relative_volume is None else f"{movement.relative_volume:.2f}x"
    details = (
        f"High {movement.high:,.2f} / Low {movement.low:,.2f} / Relative volume {rv} / "
        f"{movement.detected_at.astimezone(JST):%Y-%m-%d %H:%M} JST"
    )
    draw.text((margin, height-100), details, fill="#b5c1d1", font=_font(24))
    source_label = (
        "TEST/FIXTURE（架空データ・実在価格ではありません）"
        if movement.data_source == "fixture"
        else "Source: Twelve Data（単一データ提供元）/ 情報提供目的"
    )
    draw.text((margin, height-55), source_label, fill="#8191a7", font=_font(23))
    image.save(image_path, format="PNG", optimize=True)
    content_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    metadata = {
        "movement": movement.to_dict(), "points": len(bars),
        "width": width, "height": height, "delayed": delayed,
        "y_axis_min": low, "y_axis_max": high,
        "source": movement.data_source, "file_path": str(image_path),
        "content_hash": content_hash,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return image_path, metadata_path
