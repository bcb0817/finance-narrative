"""
金融ニュースを「構造を説明する図解」PNGに描画するモジュール。

LLM が news の内容から最適な type を選び、その type 用のJSONを返す。
このモジュールが type を見て対応する描画関数にディスパッチする。

描画レイアウトは内部的に4種類のみ（_flow / _compare / _stat / _timeline）。
表向きの type は約20種類用意し、それぞれを4レイアウトのどれかにマッピングする。
未知の type は flow にフォールバックする。

■ flow系（因果・連鎖・整理 → _flow レイアウト）
  "flow"      : 因果・波及（材料→見方→注目点 など）
  "chain"     : 金利→為替→株式 のような連鎖
  "risk_path" : リスクの伝播経路
  "scenario"  : 今後の分岐シナリオ
  "map"       : テーマの全体像
  "driver"    : 相場ドライバー整理
  "watchlist" : 注目点リスト
  "takeaway"  : 要点整理

■ compare系（対比 → _compare レイアウト）
  "compare"        : 2対象比較
  "bull_bear"      : 強気材料 vs 弱気材料
  "before_after"   : 発表前後・政策前後の変化
  "sector_compare" : セクター比較

■ stat系（数字が主役 → _stat レイアウト）
  "stat"             : 数字が主役
  "earnings"         : 決算速報
  "macro_indicator"  : CPI・雇用統計・GDP・PMI など
  "market_snapshot"  : 金利・為替・指数・原油・金などの市場スナップショット

■ timeline系（時系列・順序 → _timeline レイアウト）
  "timeline"       : 時系列
  "calendar"       : 今日/今週の重要イベント
  "event_sequence" : 発表→市場反応→次の材料
  "policy_path"    : 中銀政策・利下げ/利上げ見通しの時系列
"""

from PIL import Image, ImageDraw, ImageFont

# ===== テーマ（ダークターミナル調） =====
BG        = (13, 17, 23)
CARD      = (22, 27, 34)
CARD_LINE = (48, 54, 61)
ACCENT    = (45, 212, 191)   # ティール
ACCENT_DK = (35, 134, 122)
TEXT      = (230, 237, 243)
SUBTLE    = (139, 148, 158)
ARROW     = (88, 166, 255)   # ブルー
GREEN     = (63, 185, 80)    # 強気・上昇
RED       = (248, 81, 73)    # 弱気・下落
INK       = (8, 12, 14)

FONT_REG  = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BLK  = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"

W       = 1080
PAD     = 64
BOX_PAD = 32
RADIUS  = 20
CANVAS_H = 2400   # 一旦大きく描いて最後にクロップ

_CLOSING = set("。、）」』】〕》〉？！…ーぁぃぅぇぉっゃゅょゎ々：；,.)]}%")


import os as _os

def _f(size, bold=False):
    path = FONT_BLK if bold else FONT_REG
    if not _os.path.exists(path):
        for alt in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ):
            if _os.path.exists(alt):
                path = alt
                break
    return ImageFont.truetype(path, size)


def _wrap(draw, text, font, max_w):
    """日本語対応 + 簡易禁則（閉じ記号を行頭に置かない）"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        elif ch in _CLOSING and cur:
            cur += ch
            lines.append(cur); cur = ""
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines


def _block(d, x, y, text, font, fill, max_w, gap=10):
    lines = _wrap(d, text, font, max_w)
    asc, desc = font.getmetrics()
    lh = asc + desc + gap
    for i, ln in enumerate(lines):
        d.text((x, y + i * lh), ln, font=font, fill=fill)
    return len(lines) * lh


def _pill(d, x, y, text, font, fg, bg, h=44, padx=14, r=10):
    w = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + w + padx * 2, y + h], radius=r, fill=bg)
    d.text((x + padx, y + (h - font.getmetrics()[0]) // 2 + 2), text, font=font, fill=fg)
    return w + padx * 2


def _header(d, data, y):
    inner = W - PAD * 2
    f_tag = _f(30, True)
    _pill(d, PAD, y, f"【{data.get('tag','市場メモ')}】", f_tag, ACCENT, (30, 41, 43), h=56, padx=18, r=12)
    y += 56 + 24
    y += _block(d, PAD, y, data["title"], _f(46, True), TEXT, inner, gap=12)
    return y + 44


def _footer(d, data, y):
    f = _f(28)
    y += 20
    foot = "　".join(data.get("hashtags", []))
    d.text((PAD, y), foot, font=f, fill=SUBTLE)
    handle = data.get("handle", "")
    if handle:
        hw = d.textlength(handle, font=f)
        d.text((W - PAD - hw, y), handle, font=f, fill=ACCENT)
    return y + 50


def _arrow_down(d, y, gap=40):
    cx = W // 2
    d.line([cx, y + 8, cx, y + gap - 14], fill=ARROW, width=4)
    d.polygon([(cx - 12, y + gap - 16), (cx + 12, y + gap - 16), (cx, y + gap - 4)], fill=ARROW)
    return y + gap


# ---------- type: flow ----------
def _flow(d, data, y):
    inner_w = W - PAD * 2
    tw = inner_w - BOX_PAD * 2
    f_lbl, f_body = _f(28, True), _f(34)
    nodes = data["nodes"]
    for i, node in enumerate(nodes):
        lines = _wrap(d, node["text"], f_body, tw)
        b_asc, b_desc = f_body.getmetrics()
        body_h = len(lines) * (b_asc + b_desc + 10)
        nh = BOX_PAD + 44 + 14 + body_h + BOX_PAD
        d.rounded_rectangle([PAD, y, W - PAD, y + nh], radius=RADIUS, fill=CARD, outline=CARD_LINE, width=2)
        _pill(d, PAD + BOX_PAD, y + BOX_PAD, node["label"], f_lbl, INK, ACCENT_DK)
        _block(d, PAD + BOX_PAD, y + BOX_PAD + 44 + 14, node["text"], f_body, TEXT, tw)
        y += nh
        if i < len(nodes) - 1:
            y = _arrow_down(d, y)
    return y


# ---------- type: compare ----------
def _compare(d, data, y):
    gap = 28
    col_w = (W - PAD * 2 - gap) // 2
    tw = col_w - BOX_PAD * 2
    f_h, f_body = _f(32, True), _f(30)
    cols = [("left", GREEN), ("right", RED)]
    # 各列の高さを測って高い方に揃える
    heights = []
    for key, _ in cols:
        c = data[key]
        h = BOX_PAD + 52 + 16
        for p in c["points"]:
            ls = _wrap(d, "・" + p, f_body, tw)
            a, de = f_body.getmetrics()
            h += len(ls) * (a + de + 8) + 12
        heights.append(h + BOX_PAD)
    box_h = max(heights)
    for idx, (key, col) in enumerate(cols):
        c = data[key]
        x0 = PAD + idx * (col_w + gap)
        d.rounded_rectangle([x0, y, x0 + col_w, y + box_h], radius=RADIUS, fill=CARD, outline=col, width=3)
        # ヘッダ帯
        d.rounded_rectangle([x0, y, x0 + col_w, y + 64], radius=RADIUS, fill=col)
        d.rectangle([x0, y + 32, x0 + col_w, y + 64], fill=col)
        d.text((x0 + BOX_PAD, y + 16), c["title"], font=f_h, fill=INK)
        yy = y + 64 + 20
        for p in c["points"]:
            yy += _block(d, x0 + BOX_PAD, yy, "・" + p, f_body, TEXT, tw, gap=8) + 12
    return y + box_h


# ---------- type: stat ----------
def _stat(d, data, y):
    stats = data["stats"]
    n = len(stats)
    gap = 24
    col_w = (W - PAD * 2 - gap * (n - 1)) // n
    box_h = 230
    f_val, f_unit, f_lab = _f(78, True), _f(34, True), _f(28)
    for i, s in enumerate(stats):
        x0 = PAD + i * (col_w + gap)
        col = GREEN if s.get("dir") == "up" else RED if s.get("dir") == "down" else ACCENT
        d.rounded_rectangle([x0, y, x0 + col_w, y + box_h], radius=RADIUS, fill=CARD, outline=CARD_LINE, width=2)
        val = s["value"]
        vw = d.textlength(val, font=f_val)
        uw = d.textlength(s.get("unit", ""), font=f_unit)
        cx = x0 + (col_w - vw - uw) // 2
        d.text((cx, y + 44), val, font=f_val, fill=col)
        if s.get("unit"):
            d.text((cx + vw + 6, y + 90), s["unit"], font=f_unit, fill=col)
        lab = s["label"]
        lw = d.textlength(lab, font=f_lab)
        d.text((x0 + (col_w - lw) // 2, y + 158), lab, font=f_lab, fill=SUBTLE)
    y += box_h + 32
    if data.get("context"):
        tw = W - PAD * 2 - BOX_PAD * 2
        lines = _wrap(d, data["context"], _f(32), tw)
        a, de = _f(32).getmetrics()
        ch = len(lines) * (a + de + 10) + BOX_PAD * 2
        d.rounded_rectangle([PAD, y, W - PAD, y + ch], radius=RADIUS, fill=CARD, outline=CARD_LINE, width=2)
        _block(d, PAD + BOX_PAD, y + BOX_PAD, data["context"], _f(32), TEXT, tw)
        y += ch
    return y


# ---------- type: timeline ----------
def _timeline(d, data, y):
    line_x = PAD + 20
    text_x = line_x + 44
    f_when, f_body = _f(30, True), _f(32)
    tw = W - PAD - text_x - 8
    events = data["events"]
    dots = []
    for ev in events:
        dots.append(y + 14)
        d.text((text_x, y), ev["when"], font=f_when, fill=ACCENT)
        y += f_when.getmetrics()[0] + 14
        y += _block(d, text_x, y, ev["text"], f_body, TEXT, tw) + 30
    # 縦線（テキストの左なので重ならない）→ 点を上に重ねる
    d.line([line_x, dots[0], line_x, dots[-1]], fill=CARD_LINE, width=4)
    for dy in dots:
        d.ellipse([line_x - 12, dy - 12, line_x + 12, dy + 12], fill=ACCENT)
    return y


_RENDERERS = {"flow": _flow, "compare": _compare, "stat": _stat, "timeline": _timeline}


# ===== 表向きの type（約20種類）と説明 =====
DIAGRAM_TYPES = {
    # flow系
    "flow":            "因果・波及（材料→見方→注目点 など）",
    "chain":           "金利→為替→株式のような連鎖",
    "risk_path":       "リスクの伝播経路",
    "scenario":        "今後の分岐シナリオ",
    "map":             "テーマの全体像",
    "driver":          "相場ドライバー整理",
    "watchlist":       "注目点リスト",
    "takeaway":        "要点整理",
    # compare系
    "compare":         "2対象比較",
    "bull_bear":       "強気材料 vs 弱気材料",
    "before_after":    "発表前後・政策前後の変化",
    "sector_compare":  "セクター比較",
    # stat系
    "stat":            "数字が主役",
    "earnings":        "決算速報",
    "macro_indicator": "CPI・雇用統計・GDP・PMI など",
    "market_snapshot": "金利・為替・指数・原油・金などの市場スナップショット",
    # timeline系
    "timeline":        "時系列",
    "calendar":        "今日/今週の重要イベント",
    "event_sequence":  "発表→市場反応→次の材料",
    "policy_path":     "中銀政策・利下げ/利上げ見通しの時系列",
}

# 各 type を内部の描画レイアウト（_RENDERERS のキー）にマッピング
TYPE_TO_RENDERER = {
    # flow系 → "flow"
    "flow": "flow", "chain": "flow", "risk_path": "flow", "scenario": "flow",
    "map": "flow", "driver": "flow", "watchlist": "flow", "takeaway": "flow",
    # compare系 → "compare"
    "compare": "compare", "bull_bear": "compare",
    "before_after": "compare", "sector_compare": "compare",
    # stat系 → "stat"
    "stat": "stat", "earnings": "stat",
    "macro_indicator": "stat", "market_snapshot": "stat",
    # timeline系 → "timeline"
    "timeline": "timeline", "calendar": "timeline",
    "event_sequence": "timeline", "policy_path": "timeline",
}


def render_diagram(data: dict, out_path: str) -> str:
    img = Image.new("RGB", (W, CANVAS_H), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    y = _header(d, data, y)

    # type → renderer_key → 描画関数（未知typeは flow にフォールバック）
    diagram_type = data.get("type", "flow")
    renderer_key = TYPE_TO_RENDERER.get(diagram_type, "flow")
    renderer = _RENDERERS.get(renderer_key, _flow)

    y = renderer(d, data, y)
    y = _footer(d, data, y)
    d.rectangle([0, 0, 8, y], fill=ACCENT)  # 左アクセント
    img = img.crop((0, 0, W, int(y) + PAD - 20))
    img.save(out_path)
    return out_path


# ===== サンプル描画 =====
if __name__ == "__main__":
    samples = {
        "flow": {
            "type": "flow", "tag": "市場メモ",
            "title": "FRBが政策金利を据え置き、年内利下げ観測は後退",
            "nodes": [
                {"label": "材料", "text": "FOMCが政策金利の据え置きを決定。声明でインフレの粘着性に改めて言及した。"},
                {"label": "市場の見方", "text": "早期利下げ期待が剥落し、米長期金利は上昇方向。ドル高・株式バリュエーションには逆風。"},
                {"label": "注目点", "text": "次のCPI・雇用統計でディスインフレ継続を確認できるか。金利感応度の高いグロース株の反応に注目。"},
            ],
            "hashtags": ["#米国株", "#金利"], "handle": "@singa9999",
        },
        "compare": {
            "type": "compare", "tag": "強気と弱気",
            "title": "エヌビディア決算後、市場の見方が二分",
            "left": {"title": "強気シナリオ", "points": [
                "データセンター需要は依然旺盛",
                "ガイダンスが市場予想を上回る",
                "AI設備投資サイクルは継続"]},
            "right": {"title": "弱気シナリオ", "points": [
                "成長率の鈍化が鮮明に",
                "高いバリュエーションに割高感",
                "在庫調整・競合の台頭リスク"]},
            "hashtags": ["#エヌビディア", "#米国株"], "handle": "@singa9999",
        },
        "stat": {
            "type": "stat", "tag": "決算速報",
            "title": "エヌビディア 第3四半期決算が市場予想を上回る",
            "stats": [
                {"value": "+62", "unit": "%", "label": "売上高 前年比", "dir": "up"},
                {"value": "351", "unit": "億$", "label": "四半期売上高", "dir": "up"},
                {"value": "+5.1", "unit": "%", "label": "時間外株価", "dir": "up"},
            ],
            "context": "データセンター部門が牽引。市場予想を上回る着地で、AI関連投資の持続性が改めて意識される展開。",
            "hashtags": ["#決算", "#エヌビディア"], "handle": "@singa9999",
        },
        "timeline": {
            "type": "timeline", "tag": "今週の予定",
            "title": "今週の重要イベント・経済指標カレンダー",
            "events": [
                {"when": "火曜", "text": "米10月CPI発表。コア指標の鈍化が続くかが最大の焦点。"},
                {"when": "水曜", "text": "FOMC議事要旨の公開。利下げ時期を巡る議論の温度感を確認。"},
                {"when": "木曜", "text": "新規失業保険申請件数。労働市場の減速度合いをチェック。"},
                {"when": "金曜", "text": "小売売上高。個人消費の底堅さが景気の鍵を握る。"},
            ],
            "hashtags": ["#経済指標", "#米国株"], "handle": "@singa9999",
        },
        # ===== 追加type（内部は既存4レイアウトに振り分け） =====
        "chain": {  # flow系
            "type": "chain", "tag": "連鎖メモ",
            "title": "米利上げ観測の再燃が為替・株式に波及",
            "nodes": [
                {"label": "金利", "text": "強い経済指標を受け、米長期金利が上昇。利上げ長期化観測が再燃。"},
                {"label": "為替", "text": "日米金利差の拡大を意識し、ドル円は円安方向に振れやすい地合い。"},
                {"label": "株式", "text": "割引率上昇でグロース株に逆風。一方で輸出関連・銀行株には追い風。"},
            ],
            "hashtags": ["#為替", "#米国株"], "handle": "@singa9999",
        },
        "bull_bear": {  # compare系
            "type": "bull_bear", "tag": "強気と弱気",
            "title": "日本株、最高値圏での強気材料と弱気材料",
            "left": {"title": "強気材料", "points": [
                "好調な企業業績と自社株買い",
                "新NISAによる継続的な資金流入",
                "ガバナンス改革への期待"]},
            "right": {"title": "弱気材料", "points": [
                "急ピッチな上昇への過熱感",
                "円高反転による業績下振れ懸念",
                "海外景気の減速リスク"]},
            "hashtags": ["#日本株", "#日経平均"], "handle": "@singa9999",
        },
        "macro_indicator": {  # stat系
            "type": "macro_indicator", "tag": "経済指標",
            "title": "米10月雇用統計、労働市場の減速が鮮明に",
            "stats": [
                {"value": "15.0", "unit": "万人", "label": "非農業部門雇用者数", "dir": "down"},
                {"value": "4.1", "unit": "%", "label": "失業率", "dir": "up"},
                {"value": "+4.0", "unit": "%", "label": "平均時給 前年比", "dir": "flat"},
            ],
            "context": "雇用の伸びが市場予想を下回り、賃金上昇も鈍化。早期利下げ観測を支える内容となった。",
            "hashtags": ["#雇用統計", "#米国株"], "handle": "@singa9999",
        },
        "market_snapshot": {  # stat系
            "type": "market_snapshot", "tag": "市場概況",
            "title": "本日の主要マーケット・スナップショット",
            "stats": [
                {"value": "+1.2", "unit": "%", "label": "S&P500", "dir": "up"},
                {"value": "157.8", "unit": "円", "label": "ドル円", "dir": "up"},
                {"value": "-0.8", "unit": "%", "label": "原油WTI", "dir": "down"},
            ],
            "context": "株高・ドル高が進行。原油は需要懸念で軟調。リスク選好がやや優勢な一日。",
            "hashtags": ["#マーケット", "#為替"], "handle": "@singa9999",
        },
        "calendar": {  # timeline系
            "type": "calendar", "tag": "今週の予定",
            "title": "今週の重要イベント・経済指標カレンダー",
            "events": [
                {"when": "火", "text": "米CPI発表。コアの鈍化が続くかが最大の焦点。"},
                {"when": "水", "text": "FOMC議事要旨。利下げ時期を巡る議論を確認。"},
                {"when": "木", "text": "ECB理事会。利下げ示唆の有無に注目。"},
                {"when": "金", "text": "米小売売上高。個人消費の底堅さを点検。"},
            ],
            "hashtags": ["#経済指標", "#米国株"], "handle": "@singa9999",
        },
        "takeaway": {  # flow系
            "type": "takeaway", "tag": "要点整理",
            "title": "今朝の相場、押さえておきたい3つの要点",
            "nodes": [
                {"label": "ポイント1", "text": "米金利低下を背景にハイテク株が反発。ナスダックが主導。"},
                {"label": "ポイント2", "text": "決算シーズン本格化。ガイダンスの強弱が個別株の明暗を分ける。"},
                {"label": "ポイント3", "text": "週後半の経済指標待ちで、上値追いは限定的との見方も。"},
            ],
            "hashtags": ["#米国株", "#相場メモ"], "handle": "@singa9999",
        },
    }
    for name, data in samples.items():
        render_diagram(data, f"/home/claude/sample_{name}.png")
        print("rendered", name)
