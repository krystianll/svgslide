"""Embedded raster images: data: URIs, intrinsic sizes and upright PNG storage."""

import base64
import binascii
import struct
import urllib.parse
import zlib

PNG_SIG = b'\x89PNG\r\n\x1a\n'
# Larger images (~2.5 s per 16 MB) are mirrored by the picture's flip flag instead.
MAX_FLIP_BYTES = 16_000_000
EXT = {'image/png': 'png', 'image/jpeg': 'jpeg', 'image/jpg': 'jpeg', 'image/gif': 'gif'}


def decode_data_uri(href):
    """Return (mime, bytes) for a data: URI, or None."""
    if not href or not href.startswith('data:'):
        return None
    head, sep, data = href[5:].partition(',')
    if not sep:
        return None
    parts = head.split(';')
    mime = (parts[0] or 'text/plain').strip().lower()
    try:
        if 'base64' in [p.strip().lower() for p in parts[1:]]:
            raw = base64.b64decode(''.join(data.split()), validate=False)
        else:
            raw = urllib.parse.unquote_to_bytes(data)
    except (binascii.Error, ValueError):
        return None
    # trust the bytes more than the declared type
    if raw.startswith(PNG_SIG):
        mime = 'image/png'
    elif raw.startswith(b'\xff\xd8'):
        mime = 'image/jpeg'
    elif raw.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    return mime, raw


def intrinsic_size(mime, raw):
    """Pixel size (w, h) of a PNG, JPEG or GIF, or None."""
    try:
        if mime == 'image/png' and raw[12:16] == b'IHDR':
            return struct.unpack('>II', raw[16:24])
        if mime == 'image/gif':
            return struct.unpack('<HH', raw[6:10])
        if mime == 'image/jpeg':
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack('>H', raw[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack('>HH', raw[i + 5:i + 9])
                    return (w, h)
                i += 2 + seg
    except struct.error:
        return None
    return None


def _chunks(raw):
    i = 8
    while i + 8 <= len(raw):
        n, typ = struct.unpack('>I4s', raw[i:i + 8])
        yield typ, raw[i + 8:i + 8 + n]
        i += 12 + n


def _chunk(typ, data):
    return struct.pack('>I', len(data)) + typ + data + struct.pack('>I', zlib.crc32(typ + data) & 0xFFFFFFFF)


def flip_png(raw, flip_h, flip_v):
    """Return the PNG mirrored as asked, or None when the format is unsupported
    (interlaced, or horizontal flip of sub-byte pixels)."""
    try:
        chunks = list(_chunks(raw))
        ihdr = chunks[0][1]
        w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', ihdr)
    except (IndexError, struct.error):
        return None
    if interlace or (flip_h and depth < 8):
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        return None
    bits = depth * channels
    bpp = max(1, bits // 8)
    stride = (w * bits + 7) // 8
    if stride * h > MAX_FLIP_BYTES:
        return None   # too slow in pure Python; the caller flips with xfrm instead
    try:
        data = zlib.decompress(b''.join(d for t, d in chunks if t == b'IDAT'))
    except zlib.error:
        return None
    if len(data) < h * (stride + 1):
        return None
    rows = []
    prev = bytearray(stride)
    for r in range(h):
        off = r * (stride + 1)
        ft = data[off]
        line = bytearray(data[off + 1:off + 1 + stride])
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line))
        prev = line
    if flip_v:
        rows.reverse()
    if flip_h:
        rows = [b''.join(row[i:i + bpp] for i in range(stride - bpp, -1, -bpp)) for row in rows]
    idat = zlib.compress(b''.join(b'\x00' + row for row in rows), 9)
    out = [PNG_SIG]
    wrote_idat = False
    for typ, d in chunks:
        if typ == b'IDAT':
            if not wrote_idat:
                out.append(_chunk(b'IDAT', idat))
                wrote_idat = True
            continue
        out.append(_chunk(typ, d))
    return b''.join(out)


def encode_png(w, h, ctype, rows):
    """PNG bytes from 8-bit rows (colour type 0 grey, 2 RGB or 6 RGBA)."""
    ihdr = struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0)
    idat = zlib.compress(b''.join(b'\x00' + r for r in rows), 6)
    return PNG_SIG + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')
