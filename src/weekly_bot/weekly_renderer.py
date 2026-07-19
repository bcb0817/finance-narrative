"""
weekly_renderer.py
週間スケジュールJSONを表形式のPNG画像に描画する（Pillow / ダークテーマ）。

週間スケジュールJSON:
  {
    "title": "今週の注目イベント",
    "month_label": "2026年6月",
    "days": [
      {"date": "6/22", "weekday": "月", "events": [ <正規化イベント>, ... ]},
      ...
    ]
  }
列: 日時 / 国 / 種別 / 内容
"""

import os
from PIL import Image, ImageDraw, ImageFont

# ===== テーマ（ダーク） =====
BG        = (13, 17, 23)
HEADER_BG = (22, 27, 34)
DAY_BG    = (28, 33, 40)
ROW_A     = (17, 22, 28)
ROW_B     = (21, 26, 33)
LINE      = (48, 54, 61)
TEXT      = (230, 237, 243)
SUBTLE    = (139, 148, 158)
ACCENT    = (45, 212, 191)
RED       = (248, 81, 73)   # 最重要イベントの文字色

# 種別ごとのラベル色
CAT_COLORS = {
    "中銀": (163, 113, 247),  # 紫
    "発言": (88, 166, 255),   # 青
    "統計": (63, 185, 80),    # 緑
    "市場": (210, 153, 34),   # 琥珀
    "企業": (219, 109, 180),  # ピンク
}
# 国コードバッジの枠色
COUNTRY_COLORS = {
    "US": (59, 130, 246), "JP": (239, 68, 68), "EU": (250, 204, 21),
    "CN": (248, 113, 113), "UK": (96, 165, 250), "TW": (52, 211, 153),
}

from fonts import get_font

W        = 1080
PAD      = 48
CANVAS_H = 4000

# 列レイアウト（x開始位置）
COL_TIME    = PAD
COL_COUNTRY = PAD + 170
COL_CAT     = PAD + 300
COL_BODY    = PAD + 440
BODY_W      = W - PAD - COL_BODY


def _f(size, bold=False):
    return get_font(size, bold)


def _wrap(d, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if d.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _pill(d, x, y, text, font, fg, bg, h=40, padx=14, r=9):
    w = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + w + padx * 2, y + h], radius=r, fill=bg)
    d.text((x + padx, y + (h - font.getmetrics()[0]) // 2 + 2), text, font=font, fill=fg)
    return w + padx * 2


def _badge(d, x, y, code, font, color, h=40):
    w = max(56, d.textlength(code, font=font) + 24)
    d.rounded_rectangle([x, y, x + w, y + h], radius=9, outline=color, width=2)
    tw = d.textlength(code, font=font)
    d.text((x + (w - tw) // 2, y + (h - font.getmetrics()[0]) // 2 + 2), code, font=font, fill=color)
    return w


def render_weekly(schedule: dict, out_path: str) -> str:
    img = Image.new("RGB", (W, CANVAS_H), BG)
    d = ImageDraw.Draw(img)

    f_title = _f(48, True)
    f_month = _f(28)
    f_colh  = _f(26, True)
    f_day   = _f(30, True)
    f_time  = _f(28, True)
    f_cc    = _f(24, True)
    f_cat   = _f(24, True)
    f_body  = _f(30)
    f_note  = _f(24)

    y = PAD

    # ===== ヘッダー =====
    d.text((PAD, y), schedule.get("title", "今週の注目イベント"), font=f_title, fill=TEXT)
    ml = schedule.get("month_label", "")
    if ml:
        mw = d.textlength(ml, font=f_month)
        d.text((W - PAD - mw, y + 14), ml, font=f_month, fill=SUBTLE)
    y += 70
    d.line([PAD, y, W - PAD, y], fill=ACCENT, width=3)
    y += 20

    # ===== 列ヘッダー =====
    d.rectangle([PAD, y, W - PAD, y + 46], fill=HEADER_BG)
    d.text((COL_TIME + 6, y + 10), "日時", font=f_colh, fill=SUBTLE)
    d.text((COL_COUNTRY + 6, y + 10), "国", font=f_colh, fill=SUBTLE)
    d.text((COL_CAT + 6, y + 10), "種別", font=f_colh, fill=SUBTLE)
    d.text((COL_BODY + 6, y + 10), "内容", font=f_colh, fill=SUBTLE)
    y += 46

    row_idx = 0
    for day in schedule.get("days", []):
        # 日付バンド
        d.rectangle([PAD, y, W - PAD, y + 48], fill=DAY_BG)
        label = f'{day.get("date","")}（{day.get("weekday","")}）'
        d.text((COL_TIME + 6, y + 11), label, font=f_day, fill=ACCENT)
        y += 48

        events = day.get("events", [])
        if not events:
            d.text((COL_BODY + 6, y + 12), "主要イベントなし", font=f_note, fill=SUBTLE)
            y += 50
            continue

        for ev in events:
            # 本文の行数から行高を決定
            title = ev.get("title", "")
            body_lines = _wrap(d, title, f_body, BODY_W)
            has_note = bool(ev.get("note")) or ev.get("tentative")
            b_asc, b_desc = f_body.getmetrics()
            line_h = b_asc + b_desc + 8
            row_h = max(64, len(body_lines) * line_h + (30 if has_note else 0) + 24)

            # 行背景（ストライプ）
            d.rectangle([PAD, y, W - PAD, y + row_h], fill=ROW_A if row_idx % 2 == 0 else ROW_B)
            row_idx += 1

            cy = y + 14

            # 日時（未定/早朝も文字でそのまま表示）
            time_txt = ev.get("time_jst", "未定")
            time_color = SUBTLE if time_txt in ("未定", "早朝", "時間未定") else TEXT
            d.text((COL_TIME + 6, cy + 4), time_txt, font=f_time, fill=time_color)

            # 国バッジ
            cc = ev.get("country", "US")
            _badge(d, COL_COUNTRY + 6, cy, cc, f_cc, COUNTRY_COLORS.get(cc, SUBTLE))

            # 種別ラベル（色分け）
            cat = ev.get("category", "市場")
            col = CAT_COLORS.get(cat, SUBTLE)
            _pill(d, COL_CAT + 6, cy, cat, f_cat, (8, 12, 14), col)

            # 内容（最重要は赤字）
            body_color = RED if ev.get("importance") == "high" else TEXT
            ty = y + 14
            for ln in body_lines:
                d.text((COL_BODY + 6, ty), ln, font=f_body, fill=body_color)
                ty += line_h
            # 補足・未定タグ
            extras = []
            if ev.get("tentative"):
                extras.append("（時間未定の可能性）")
            if ev.get("note"):
                extras.append(ev["note"])
            if extras:
                d.text((COL_BODY + 6, ty), "　".join(extras), font=f_note, fill=SUBTLE)

            y += row_h
            d.line([PAD, y, W - PAD, y], fill=LINE, width=1)

    # フッター
    y += 16
    foot = "※時刻は日本時間（JST）。出所確認済みイベントのみ掲載。"
    d.text((PAD, y), foot, font=f_note, fill=SUBTLE)
    y += 44

    img = img.crop((0, 0, W, int(y)))
    img.save(out_path)
    return out_path
