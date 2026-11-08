"""Developer tool: convert a folder of SVGs and write side-by-side comparison PNGs.

Each output PNG has three columns separated by grey bars:
  left   - the SVG rendered by Inkscape (reference)
  middle - svgslide's .pptx rendered by LibreOffice
  right  - another converter's .pptx of the same name rendered by LibreOffice
           (only with --other DIR)

Usage:
  python tools/compare_corpus.py --corpus DIR --out DIR [--other DIR] [--height 900]
                                 [--font "DejaVu Sans=Arial"] [NAME ...]

Needs Inkscape, LibreOffice (soffice) and pdftoppm, found on PATH, via the
environment variables SVGSLIDE_INKSCAPE / SVGSLIDE_SOFFICE / SVGSLIDE_PDFTOPPM,
or (macOS) in the standard application bundles.  Prints the conversion time
of each file.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import svgslide  # noqa: E402

def find_tool(name):
    env = os.environ.get('SVGSLIDE_' + name.upper())
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    sys.exit('cannot find %s on PATH; set SVGSLIDE_%s' % (name, name.upper()))


def read_ppm(path):
    data = Path(path).read_bytes()
    parts = []
    i = 0
    while len(parts) < 4:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b'#':
            i = data.index(b'\n', i)
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        parts.append(data[i:j])
        i = j
    i += 1
    w, h = int(parts[1]), int(parts[2])
    return w, h, data[i:i + w * h * 3]


def to_pdf_first_page_ppm(pdf, height, pdftoppm, tmp):
    stem = Path(tmp) / ('p%d' % time.perf_counter_ns())
    res = subprocess.run([pdftoppm, '-f', '1', '-l', '1', '-scale-to-y', str(height), '-scale-to-x', '-1',
                          '-singlefile', str(pdf), str(stem)], capture_output=True)
    if res.returncode:
        print('      pdftoppm failed on %s: %s' % (Path(pdf).name, res.stderr.decode(errors='replace')[:200]))
        return None
    return read_ppm(str(stem) + '.ppm')


def read_png_rgb(path):
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG (Inkscape output) to RGB on white."""
    import struct
    data = Path(path).read_bytes()
    i, idat, w, h, ctype = 8, [], 0, 0, 6
    while i < len(data):
        n, t = struct.unpack('>I4s', data[i:i + 8])
        d = data[i + 8:i + 8 + n]
        if t == b'IHDR':
            w, h, _, ctype = struct.unpack('>IIBB', d[:10])
        elif t == b'IDAT':
            idat.append(d)
        i += 12 + n
    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(b''.join(idat))
    stride = w * bpp
    prev = bytearray(stride)
    out = bytearray()
    for r in range(h):
        ft = raw[r * (stride + 1)]
        line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for k in range(stride):
            a = line[k - bpp] if k >= bpp else 0
            b = prev[k]
            if ft == 1:
                line[k] = (line[k] + a) & 255
            elif ft == 2:
                line[k] = (line[k] + b) & 255
            elif ft == 3:
                line[k] = (line[k] + ((a + b) >> 1)) & 255
            elif ft == 4:
                c = prev[k - bpp] if k >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[k] = (line[k] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if bpp == 4:
            for k in range(0, stride, 4):
                al = line[k + 3]
                out += bytes(((v * al + 255 * (255 - al)) // 255) for v in line[k:k + 3])
        else:
            out += line
    return w, h, bytes(out)


def write_png(path, w, h, rgb):
    raw = b''.join(b'\x00' + rgb[r * w * 3:(r + 1) * w * 3] for r in range(h))

    def chunk(t, d):
        import struct
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    import struct
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    Path(path).write_bytes(png)


def side_by_side(images, height):
    gap = 12
    widths = [im[0] if im else height // 2 for im in images]
    total = sum(widths) + gap * (len(images) - 1)
    rows = []
    for r in range(height):
        parts = []
        for k, im in enumerate(images):
            if k:
                parts.append(b'\x99\x99\x99' * gap)
            if im is None:
                parts.append(b'\xff\x00\xff' * widths[k])
                continue
            w, h, px = im
            parts.append(px[r * w * 3:(r + 1) * w * 3] if r < h else b'\xff\xff\xff' * w)
        rows.append(b''.join(parts))
    return total, height, b''.join(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--corpus', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--other')
    ap.add_argument('--height', type=int, default=900)
    ap.add_argument('--font', action='append', default=[])
    ap.add_argument('names', nargs='*')
    args = ap.parse_args()
    inkscape, soffice, pdftoppm = find_tool('inkscape'), find_tool('soffice'), find_tool('pdftoppm')
    fonts = dict(f.split('=', 1) for f in args.font)
    out = Path(args.out)
    (out / 'pptx').mkdir(parents=True, exist_ok=True)
    svgs = sorted(Path(args.corpus).glob('*.svg'))
    if args.names:
        svgs = [p for p in svgs if p.stem in args.names]
    with tempfile.TemporaryDirectory(dir=str(out)) as tmp:
        profile = Path(tmp) / 'lo_profile'
        lo = [soffice, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--convert-to', 'pdf',
              '--outdir', tmp]
        for svg in svgs:
            pptx = out / 'pptx' / (svg.stem + '.pptx')
            t = time.perf_counter()
            rep = svgslide.convert(svg, pptx, fonts=fonts or None)
            dt = time.perf_counter() - t
            print('%-32s %7.2f s  shapes=%d text=%d pictures=%d groups=%d skipped=%d'
                  % (svg.stem, dt, rep.shapes, rep.text_boxes, rep.pictures, rep.groups, len(rep.skipped)))
            for feat, where in rep.skipped[:5]:
                print('      skipped: %s (%s)' % (feat, where))
            ref_pdf = Path(tmp) / (svg.stem + '.ref.pdf')
            subprocess.run([inkscape, str(svg), '--export-type=pdf', '--export-filename=' + str(ref_pdf)],
                           capture_output=True)
            ref = to_pdf_first_page_ppm(ref_pdf, args.height, pdftoppm, tmp)
            if ref is None:
                ref_png = Path(tmp) / (svg.stem + '.ref.png')
                subprocess.run([inkscape, str(svg), '--export-type=png', '--export-filename=' + str(ref_png),
                                '--export-height=%d' % args.height], capture_output=True)
                ref = read_png_rgb(ref_png) if ref_png.exists() else None
            images = [ref]
            cands = [pptx]
            if args.other:
                cands.append(Path(args.other) / (svg.stem + '.pptx'))
            for cand in cands:
                if not cand.exists():
                    images.append(None)
                    continue
                sub = Path(tmp) / ('c%d' % len(images))
                sub.mkdir(exist_ok=True)
                shutil.copy(cand, sub / cand.name)
                subprocess.run(lo[:-1] + [str(sub), str(sub / cand.name)], check=True, capture_output=True)
                images.append(to_pdf_first_page_ppm(sub / (cand.stem + '.pdf'), args.height, pdftoppm, tmp))
            w, h, px = side_by_side(images, args.height)
            write_png(out / (svg.stem + '.png'), w, h, px)


if __name__ == '__main__':
    main()
