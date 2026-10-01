"""Render only adapter-verified visual facts; never infer rights or causal arrows."""
import hashlib
import re
from common.runtime import output_dir


def visual_reason(packet):
    brief = packet['brief']
    if brief.get('image_kind') not in {'compare', 'timeline', 'conditions', 'relation'} or not brief.get('image_value'):
        return 'no_visual_explanatory_value'
    sources = packet['sources']
    points = [p for s in sources for p in s.get('visual_points', [])]
    if not 2 <= len(points) <= 4:
        return 'requires_2_to_4_verified_points'
    for source in sources:
        if source.get('visual_points') and (source.get('image_public_allowed') is not True
                or not source.get('public_allowed') or source.get('provider', '').lower() == 'twelvedata'):
            return 'image_public_rights_unconfirmed'
        for point in source.get('visual_points', []):
            quote = point.get('quote', '')
            if not quote or len(quote) > 110 or quote not in source['excerpt']:
                return 'image_quote_not_grounded'
            if point.get('period') != source.get('period') or point.get('source') != source.get('source'):
                return 'image_source_period_mismatch'
            if re.search(r'\d', quote) and (not point.get('unit') or point['unit'] not in quote):
                return 'image_numeric_unit_missing'
            if re.search(r'https?://|www\.|[→⇒⟶]', quote):
                return 'image_link_or_causal_arrow'
    if all(p['quote'] in packet['text'] for p in points):
        return 'image_repeats_body'
    return ''


def prepare_visual(packet, reviewer=None):
    reason = visual_reason(packet)
    if reason:
        return {'image_path': None, 'image_hash': None, 'image_reason': reason}
    points = [p for s in packet['sources'] for p in s.get('visual_points', [])]
    # Image text gets the same fact/safety scrutiny as the body. No captions are invented.
    checked = reviewer('\n'.join(p['quote'] for p in points)) if reviewer else {}
    if not all(checked.get(k) is True for k in ('supported', 'safe', 'unchanged')):
        return {'image_path': None, 'image_hash': None, 'image_reason': 'image_fact_review_failed'}
    try:
        path = render(packet, points)
    except (OSError, ValueError):
        return {'image_path': None, 'image_hash': None, 'image_reason': 'image_render_failed'}
    return {'image_path': str(path), 'image_hash': hashlib.sha256(path.read_bytes()).hexdigest(),
            'image_reason': packet['brief']['image_value'], 'image_points': points}


def render(packet, points):
    from PIL import Image, ImageDraw
    from common.fonts import get_font
    image = Image.new('RGB', (1200, 180 + 230 * len(points)), '#f5f6f8')
    draw = ImageDraw.Draw(image)
    font, small = get_font(32), get_font(23)
    labels = {'compare': '比較', 'timeline': '時系列', 'conditions': '条件の違い', 'relation': '関係の整理（因果を示すものではありません）'}
    draw.text((44, 36), labels[packet['brief']['image_kind']], font=font, fill='#182f48')
    for index, point in enumerate(points):
        y = 110 + 230 * index
        draw.rounded_rectangle((32, y, 1168, y + 212), radius=12, fill='white')
        def lines(text, max_width=1080):
            result, current = [], ''
            for char in text:
                if draw.textlength(current + char, font=font) > max_width:
                    result.append(current); current = ''
                current += char
            return result + [current]
        for n, line in enumerate(lines(point['quote'])):
            if n > 3:
                raise ValueError('visual text overflow')
            draw.text((52, y + 16 + n * 37), line, font=font, fill='#122338')
        footer = f"出典：{point['source']} ｜ 対象：{point['period']}"
        if draw.textlength(footer, font=small) > 1080:
            raise ValueError('visual source overflow')
        draw.text((52, y + 178), footer, font=small, fill='#4b5b70')
    path = output_dir('editorial') / (packet['body_hash'][:20] + '.png')
    image.save(path)
    return path
