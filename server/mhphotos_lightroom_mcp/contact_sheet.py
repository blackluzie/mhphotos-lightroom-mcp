"""Bounded in-memory review sheets from already authorized JPEG previews."""
import base64
import io

STATUSES = frozenset({'available', 'preview_timeout', 'preview_unavailable', 'preview_size_limit'})


def render_contact_sheet(statuses, images, offset):
    if (type(offset) is not int or not 0 <= offset <= 1000000
        or not isinstance(statuses, list) or len(statuses) > 10
        or not isinstance(images, list) or len(images) > 10):
        raise ValueError('invalid_sheet_input')
    for index, status in enumerate(statuses, 1):
        if (not isinstance(status, dict) or set(status) != {'index', 'status'}
            or type(status['index']) is not int or status['index'] != index
            or not isinstance(status['status'], str) or status['status'] not in STATUSES):
            raise ValueError('invalid_sheet_input')
    if len(images) != sum(s['status'] == 'available' for s in statuses):
        raise ValueError('invalid_sheet_input')
    if not images:
        return None

    from PIL import Image, ImageDraw, ImageFont, ImageOps

    columns = min(5, len(statuses))
    rows = (len(statuses) + columns - 1) // columns
    width, height, label_height = 256, 180, 24
    canvas = Image.new('RGB', (columns * width, rows * (height + label_height)), (30, 32, 36))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=14)
    available = iter(images)
    for index, status in enumerate(statuses):
        left = (index % columns) * width
        top = (index // columns) * (height + label_height)
        draw.rectangle((left, top, left + width - 1, top + height + label_height - 1), outline=(90, 92, 96))
        draw.text((left + 8, top + 4), 'Foto ' + str(offset + index + 1), font=font, fill='white')
        if status['status'] != 'available':
            message = 'Vorschau ausstehend' if status['status'] == 'preview_timeout' else 'Vorschau fehlt'
            draw.text((left + 12, top + label_height + 70), message, font=font, fill=(210, 210, 210))
            continue
        encoded = next(available)
        if not isinstance(encoded, str) or len(encoded) > 700000:
            raise ValueError('invalid_sheet_preview')
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 512000:
            raise ValueError('invalid_sheet_preview')
        try:
            with Image.open(io.BytesIO(raw), formats=['JPEG']) as source:
                if max(source.size) > 1024:
                    raise ValueError('invalid_sheet_preview')
                thumb = ImageOps.exif_transpose(source).convert('RGB')
                thumb.thumbnail((width - 8, height - 8))
                canvas.paste(thumb, (left + (width - thumb.width) // 2,
                                    top + label_height + (height - thumb.height) // 2))
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError('invalid_sheet_preview') from exc
    output = io.BytesIO()
    canvas.save(output, format='JPEG', quality=85, exif=b'', icc_profile=None)
    if output.tell() > 512000:
        raise ValueError('sheet_too_large')
    return base64.b64encode(output.getvalue()).decode('ascii')
