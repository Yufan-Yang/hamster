"""Reusable live-cover layout: real footage, explicit attribution and a short editorial hook."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .core import q
from .live_bilibili import original_source, title_name


DESIGN_VERSION = 2
SIZE = (1280, 720)
FONTS = (
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
    '/System/Library/Fonts/PingFang.ttc',
)


def font(size):
    for path in FONTS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise ValueError('生成中文封面需要安装 Noto Sans CJK 字体')


def wrap(text, face, width):
    lines = []
    for paragraph in text.splitlines():
        line = ''
        tokens = re.findall(r'[A-Za-z0-9]+(?:[ .+-][A-Za-z0-9]+)*|\s+|.', paragraph)
        for token in tokens:
            parts = list(token) if face.getlength(token) > width else [token]
            for part in parts:
                if line and face.getlength(line + part) > width:
                    lines.append(line.strip()); line = ''
                line += part.lstrip() if not line else part
        if line.strip():
            lines.append(line.strip())
    return lines or ['直播片段']


def fit_hook(text, width=1160):
    for size in (76, 72, 68, 64, 60, 56, 52):
        face = font(size)
        lines = wrap(text, face, width)
        if len(lines) <= 3:
            return face, lines
    lines = lines[:3]
    while lines[-1] and face.getlength(lines[-1] + '…') > width:
        lines[-1] = lines[-1][:-1]
    lines[-1] += '…'
    return face, lines


def hook_text(clip, source):
    text = str(clip.get('cover_title') or '').strip()
    if text:
        return text
    text = str(clip['title']).strip()
    for name in (title_name(source), source['name']):
        text = re.sub(r'^\s*(?:【' + re.escape(name) + r'】|' + re.escape(name) + r'\s*[:：])\s*', '', text)
    return text


def design(frame, clip, source):
    """Actual livestream frame with native clipping-channel lettering, without a branded panel."""
    source = original_source(clip, source)
    # Portrait streams become an honest close-up. Keep the eyes around the middle;
    # text goes at the edges, rather than replacing half the footage with a panel.
    frame = frame.convert('RGB')
    image = ImageOps.fit(frame, SIZE, centering=(.5, .45))
    if frame.width / frame.height < 1.15:
        # Common portrait-stream treatment: retain a readable head/gesture in the
        # centre and extend the same frame at the edges instead of cutting off the chin.
        image = image.filter(ImageFilter.GaussianBlur(24))
        image.paste(ImageOps.fit(frame, (820, 720), centering=(.5, .45)), (230, 0))
    draw = ImageDraw.Draw(image)
    text = hook_text(clip, source)
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    sticker = len(paragraphs) == 1 and len(paragraphs[0]) <= 16
    if sticker:
        face = font(104)
        while face.getlength(paragraphs[0]) > 1120 and face.size > 64:
            face = font(face.size-4)
        # A white outer die-cut edge, black inner stroke and yellow lettering:
        # the brief reaction-caption treatment the user referenced.
        draw.text((640, 35), paragraphs[0], font=face, anchor='mt', fill='#ffdf55',
                  stroke_width=18, stroke_fill='#ffffff')
        draw.text((640, 35), paragraphs[0], font=face, anchor='mt', fill='#ffdf55',
                  stroke_width=9, stroke_fill='#111111')
    else:
        top = paragraphs[0] if len(paragraphs) > 1 else text
        face, lines = fit_hook(top)
        for i, line in enumerate(lines):
            draw.text((640, 30+i*(face.size+12)), line, font=face, anchor='mt',
                      fill='#ffdf55', stroke_width=8, stroke_fill='#111111')
        if len(paragraphs) > 1:
            face, lines = fit_hook(' '.join(paragraphs[1:]))
            lines = lines[:2]
            y = 600-(len(lines)-1)*(face.size+12)
            for i, line in enumerate(lines):
                draw.text((640, y+i*(face.size+12)), line, font=face, anchor='mt',
                          fill='#ffdf55', stroke_width=8, stroke_fill='#111111')
    return image


def ensure_cover(clip, source):
    clip, source = dict(clip), original_source(clip, dict(source))
    video = Path(clip['file'])
    signature = hashlib.sha256(json.dumps([
        DESIGN_VERSION, clip['title'], clip.get('cover_title'), title_name(source), source['name'],
        clip.get('lane'), video.stat().st_mtime_ns,
    ], ensure_ascii=False).encode()).hexdigest()[:16]
    output = video.with_name(f'{video.stem}-cover-{signature}.jpg')
    if not output.is_file():
        duration = max(1, float(clip.get('end', 4))-float(clip.get('start', 0)))
        result = subprocess.run([
            'ffmpeg', '-v', 'error', '-ss', str(min(duration*.25, 12)), '-i', str(video),
            '-frames:v', '1', '-vf', 'thumbnail=24,scale=960:-2', '-f', 'image2pipe', '-vcodec', 'png', '-',
        ], capture_output=True, timeout=30, check=True)
        with Image.open(BytesIO(result.stdout)) as frame:
            image = design(frame, clip, source)
        handle = tempfile.NamedTemporaryFile(prefix=output.stem+'-', suffix='.jpg', dir=output.parent, delete=False)
        try:
            with handle:
                image.save(handle, format='JPEG', quality=91, optimize=True)
            os.replace(handle.name, output)
        finally:
            Path(handle.name).unlink(missing_ok=True)
    q('UPDATE live_clips SET cover=? WHERE id=? AND version=? AND file=?',
      (str(output), clip['id'], clip['version'], str(video)))
    return output
