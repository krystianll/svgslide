"""Read PDF pages with pypdfium2 and describe each one as an SVG tree.

The converter then turns that tree into slide shapes like any other SVG.
Each page object becomes one SVG element in drawing order:

- path objects: ``<path>`` with the object's matrix as ``transform``, fill
  and stroke colours with their alpha, line width, dash pattern, caps, joins
  and fill rule;
- text objects: one ``<text>`` per run of characters that the PDF sets one
  after the other (a jump of more than half an em, or a change of line,
  starts a new run), at the position the PDF gives the run's first character,
  with the font family, weight and style read from the font's name;
- image objects: ``<image>`` with the picture as a PNG (or the PDF's own
  JPEG data) in a ``data:`` URI; masks become the PNG's alpha;
- form XObjects: ``<g>`` holding their objects;
- clip paths: ``clip-path`` on a ``<g>`` around the object (the converter
  keeps rectangles exactly and approximates other outlines).

Shadings, text used as a clip path, soft masks and blend modes are not
drawn, pattern paints are drawn in their average colour; all are reported
with the page.
"""

import base64
import ctypes
import math
import re
import xml.etree.ElementTree as ET

from . import _geom as g
from . import _images as img
from ._convert import _ps_font
from ._text import char_width

SVG = '{http://www.w3.org/2000/svg}'
PIP_HINT = 'pip install svgslide[pdf]'
_SUBSET = re.compile(r'^[A-Z]{6}\+')
_COLOR = '#%02x%02x%02x'
# a text run ends where the next character starts more than this many ems
# away from where the previous character's advance ends
GAP_EM = 0.5


def _pdfium():
    try:
        import pypdfium2
        import pypdfium2.raw as raw
    except ImportError as e:
        raise ImportError('reading PDF files needs pypdfium2; install it with: %s' % PIP_HINT) from e
    return pypdfium2, raw


def page_numbers(pages, count):
    """The 1-based page numbers asked for, checked against the page count."""
    if pages is None:
        return list(range(1, count + 1))
    items = [pages] if isinstance(pages, int) else list(pages)
    if not items:
        raise ValueError('pages: no page given')
    for p in items:
        if isinstance(p, bool) or not isinstance(p, int):
            raise ValueError('pages: page numbers must be integers, got %r' % (p,))
        if not 1 <= p <= count:
            raise ValueError('pages: page %d is out of range (the PDF has %d page%s)'
                             % (p, count, '' if count == 1 else 's'))
    return items


def _fmt(v):
    s = '%.4f' % v
    s = s.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def _mat(m):
    return 'matrix(%s)' % ' '.join(_fmt(v) for v in m)


def _rgb(c):
    return _COLOR % (c[0], c[1], c[2])


class _Page:
    """Builds the SVG tree of one page."""

    def __init__(self, raw, doc, page, number, size):
        self.R = raw
        self.doc = doc
        self.page = page
        self.number = number
        self.skipped = []
        self.n_obj = self.current = 0
        self.pending = self.last_text = None
        self.clip_ids = []
        self.clips = {}
        R = raw
        box = R.FS_RECTF()
        if not R.FPDF_GetPageBoundingBox(page, box):
            box.left, box.bottom = 0.0, 0.0
            box.right, box.top = R.FPDF_GetPageWidthF(page), R.FPDF_GetPageHeightF(page)
        x0, x1 = min(box.left, box.right), max(box.left, box.right)
        y0, y1 = min(box.bottom, box.top), max(box.bottom, box.top)
        w, h = x1 - x0, y1 - y0
        # PDF user space (y up) -> SVG (y down), then the page's /Rotate
        m = (1.0, 0.0, 0.0, -1.0, -x0, y1)
        rot = R.FPDFPage_GetRotation(page) % 4
        if rot == 1:
            m = g.mul((0.0, 1.0, -1.0, 0.0, h, 0.0), m)
            w, h = h, w
        elif rot == 2:
            m = g.mul((-1.0, 0.0, 0.0, -1.0, w, h), m)
        elif rot == 3:
            m = g.mul((0.0, -1.0, 1.0, 0.0, 0.0, w), m)
            w, h = h, w
        self.page_m = m
        self.w, self.h = w, h
        # the slide has the first page's size; this page fits into it
        sw, sh = size or (w, h)
        self.root = ET.Element(SVG + 'svg', {
            'width': _fmt(sw) + 'pt', 'height': _fmt(sh) + 'pt',
            'viewBox': '0 0 %s %s' % (_fmt(w), _fmt(h))})
        self.defs = ET.SubElement(self.root, SVG + 'defs')

    def skip(self, feature, what=None):
        self.skipped.append((feature, 'page %d %s' % (self.number, what or 'object %d' % self.current)))

    # --- reading -------------------------------------------------------

    def read(self):
        R = self.R
        self._text_chars()
        out = []
        for k in range(R.FPDFPage_CountObjects(self.page)):
            out.extend(self.obj(R.FPDFPage_GetObject(self.page, k), g.IDENTITY))
        self.root.extend(out)
        if len(self.defs) == 0:
            self.root.remove(self.defs)
        n_annots = R.FPDFPage_GetAnnotCount(self.page)
        if n_annots:
            self.skip('annotations (comments, links, form fields)', '(%d annotations)' % n_annots)
        return self.root

    def _text_chars(self):
        """Characters of the page's text, grouped by their text object."""
        R = self.R
        self.chars = {}
        self.fonts = {}
        tp = R.FPDFText_LoadPage(self.page)
        if not tp:
            return
        try:
            x, y = ctypes.c_double(), ctypes.c_double()
            box = R.FS_RECTF()
            for i in range(R.FPDFText_CountChars(tp)):
                obj = R.FPDFText_GetTextObject(tp, i)
                key = ctypes.cast(obj, ctypes.c_void_p).value
                if not key or R.FPDFText_IsGenerated(tp, i) == 1:
                    continue
                u = R.FPDFText_GetUnicode(tp, i)
                if not R.FPDFText_GetCharOrigin(tp, i, x, y):
                    continue
                adv = None
                if R.FPDFText_GetLooseCharBox(tp, i, box):
                    adv = (abs(box.right - box.left), abs(box.top - box.bottom))
                if key not in self.chars:
                    self.chars[key] = []
                    n = R.FPDFText_GetFontInfo(tp, i, None, 0, None)
                    buf = ctypes.create_string_buffer(max(n, 1))
                    flags = ctypes.c_int(0)
                    R.FPDFText_GetFontInfo(tp, i, buf, max(n, 1), flags)
                    self.fonts[key] = (buf.value.decode('utf-8', 'replace'), max(flags.value, 0),
                                       R.FPDFText_GetFontWeight(tp, i))
                self.chars[key].append((u, x.value, y.value, adv))
        finally:
            R.FPDFText_ClosePage(tp)

    def _matrix(self, o):
        m = self.R.FS_MATRIX()
        if not self.R.FPDFPageObj_GetMatrix(o, m):
            return g.IDENTITY
        return (m.a, m.b, m.c, m.d, m.e, m.f)

    def obj(self, o, parent):
        """SVG elements for one page object; parent maps the object's space to the page."""
        R = self.R
        self.n_obj += 1
        self.last_text = None
        number = self.current = self.n_obj      # counted in drawing order, form contents included
        t = R.FPDFPageObj_GetType(o)
        full = g.mul(parent, self._matrix(o))
        self.patterned = False
        if t == R.FPDF_PAGEOBJ_PATH:
            els = self.path(o, full)
        elif t == R.FPDF_PAGEOBJ_TEXT:
            els = self.text(o, full)
        elif t == R.FPDF_PAGEOBJ_IMAGE:
            els = self.image(o, full)
        elif t == R.FPDF_PAGEOBJ_FORM:
            grp = ET.Element(SVG + 'g')
            for k in range(R.FPDFFormObj_CountObjects(o)):
                grp.extend(self.obj(R.FPDFFormObj_GetObject(o, k), full))
            els = [grp] if len(grp) else []
            self.current = number
        elif t == R.FPDF_PAGEOBJ_SHADING:
            self.skip('shading (smooth colour gradient)')
            els = []
        else:
            self.skip('unknown PDF object')
            els = []
        if not els:
            return []
        # pdfium counts a fill or stroke alpha below 1 as transparency, even
        # for a paint the object does not use; anything else is a soft mask
        # or a blend mode
        if (t != R.FPDF_PAGEOBJ_FORM and not self.patterned and R.FPDFPageObj_HasTransparency(o)
                and all(c is None or c[3] == 255 for c in (self._color(o, 'fill'), self._color(o, 'stroke')))):
            self.skip('soft mask or blend mode (drawn without it)')
        out = self.clip(o, parent, els)
        if t != R.FPDF_PAGEOBJ_TEXT or self.last_text is None:
            self.pending = None
            return out
        key, start, end, em, el = self.last_text
        key = key + (tuple(self.clip_ids),)
        p = self.pending
        gap = math.hypot(start[0] - p[2][0], start[1] - p[2][1]) if p is not None else None
        if p is not None and p[0] == key and gap <= GAP_EM * em:
            # the PDF continues the previous text with a new text object (a
            # ligature, a kerned or re-encoded part): one text box; a gap
            # wider than a thin space is a word space
            sep = ' ' if gap > 0.15 * em and not p[1].text.endswith(' ') and not el.text.startswith(' ') else ''
            p[1].text += sep + el.text
            self.pending = (key, p[1], end)
            return []
        self.pending = (key, el, end)
        return out

    def _color(self, o, which):
        r, gr, b, a = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        get = self.R.FPDFPageObj_GetFillColor if which == 'fill' else self.R.FPDFPageObj_GetStrokeColor
        if not get(o, r, gr, b, a):
            return None
        return (r.value, gr.value, b.value, a.value)

    def _pattern(self, o, which):
        """Average colour of a pattern paint's tile, or None when the paint is
        not a pattern."""
        R = self.R
        get = R.FPDFPageObj_GetRenderedFillPattern if which == 'fill' else R.FPDFPageObj_GetRenderedStrokePattern
        bm = get(self.doc, o)
        if not bm:
            return None
        try:
            w, h = R.FPDFBitmap_GetWidth(bm), R.FPDFBitmap_GetHeight(bm)
            stride, fmt = R.FPDFBitmap_GetStride(bm), R.FPDFBitmap_GetFormat(bm)
            raw = ctypes.string_at(R.FPDFBitmap_GetBuffer(bm), stride * h) if w > 0 and h > 0 else b''
        finally:
            R.FPDFBitmap_Destroy(bm)
        bpp = {R.FPDFBitmap_Gray: 1, R.FPDFBitmap_BGR: 3, R.FPDFBitmap_BGRx: 4, R.FPDFBitmap_BGRA: 4}.get(fmt)
        if not raw or bpp is None:
            return (128, 128, 128, 255)
        tot = [0, 0, 0]
        wsum = 0
        for row in range(h):
            line = raw[row * stride:row * stride + w * bpp]
            if bpp == 1:
                v = sum(line)
                tot = [tot[0] + v, tot[1] + v, tot[2] + v]
                wsum += 255 * w
                continue
            al = line[3::4] if fmt == R.FPDFBitmap_BGRA else None
            for k, ch in enumerate((2, 1, 0)):
                comp = line[ch::bpp]
                tot[k] += sum(c * x for c, x in zip(comp, al)) if al is not None else 255 * sum(comp)
            wsum += sum(al) if al is not None else 255 * w
        if not wsum:
            return (128, 128, 128, 0)
        return tuple(int(round(t / wsum)) for t in tot) + (int(round(wsum / (w * h))),)

    def _paint(self, attrs, which, o):
        c = self._pattern(o, which)
        if c is not None:
            self.skip('pattern %s (drawn in its average colour)' % which)
            self.patterned = True
        else:
            c = self._color(o, which)
        if c is None:
            self.skip('%s colour unreadable (drawn grey)' % which)
            c = (128, 128, 128, 255)
        attrs[which] = _rgb(c)
        if c[3] < 255:
            attrs[which + '-opacity'] = _fmt(c[3] / 255.0)

    # --- clip ----------------------------------------------------------

    def _segments(self, count, get):
        """SVG path data from pdfium path segments."""
        R = self.R
        x, y = ctypes.c_float(), ctypes.c_float()
        d, pending = [], []
        for i in range(count):
            seg = get(i)
            R.FPDFPathSegment_GetPoint(seg, x, y)
            kind = R.FPDFPathSegment_GetType(seg)
            pt = _fmt(x.value) + ' ' + _fmt(y.value)
            if kind == R.FPDF_SEGMENT_BEZIERTO:
                pending.append(pt)
                if len(pending) == 3:
                    d.append('C' + ' '.join(pending))
                    pending = []
            else:
                pending = []
                d.append(('M' if kind == R.FPDF_SEGMENT_MOVETO else 'L') + pt)
            if R.FPDFPathSegment_GetClose(seg):
                d.append('Z')
        return ' '.join(d)

    def clip(self, o, parent, els):
        """Wrap the elements in one group per clip path of the object."""
        R = self.R
        self.clip_ids = []
        cp = R.FPDFPageObj_GetClipPath(o)
        if not cp:
            return els
        n = R.FPDFClipPath_CountPaths(cp)
        if n <= 0:
            return els
        bounds = None
        l, b, r, t = ctypes.c_float(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
        if R.FPDFPageObj_GetBounds(o, l, b, r, t):
            bounds = (l.value, b.value, r.value, t.value)
        for i in range(n):
            count = R.FPDFClipPath_CountPathSegments(cp, i)
            if count < 0:
                self.skip('text used as a clip path (not clipped)')
                continue
            if count == 0:
                continue
            d = self._segments(count, lambda k, i=i: R.FPDFClipPath_GetPathSegment(cp, i, k))
            subs = g.transform_path(g.parse_path(d), parent)
            if bounds is not None and _rect_contains(subs, bounds):
                continue
            d_page = _path_data(g.transform_path(subs, self.page_m))
            cid = self.clips.get(d_page)
            if cid is None:
                cid = 'clip%d' % (len(self.clips) + 1)
                self.clips[d_page] = cid
                cel = ET.SubElement(self.defs, SVG + 'clipPath', {'id': cid})
                ET.SubElement(cel, SVG + 'path', {'d': d_page})
            self.clip_ids.append(cid)
            grp = ET.Element(SVG + 'g', {'clip-path': 'url(#%s)' % cid})
            grp.extend(els)
            els = [grp]
        return els

    # --- objects -------------------------------------------------------

    def path(self, o, full):
        R = self.R
        n = R.FPDFPath_CountSegments(o)
        if n <= 0:
            return []
        d = self._segments(n, lambda k: R.FPDFPath_GetPathSegment(o, k))
        mode, stroke = ctypes.c_int(), ctypes.c_int()
        if not R.FPDFPath_GetDrawMode(o, mode, stroke):
            return []
        fill = mode.value
        if not fill and not stroke.value:
            return []
        attrs = {'d': d, 'transform': _mat(g.mul(self.page_m, full))}
        if fill:
            self._paint(attrs, 'fill', o)
            if fill == R.FPDF_FILLMODE_ALTERNATE:
                attrs['fill-rule'] = 'evenodd'
        else:
            attrs['fill'] = 'none'
        if stroke.value:
            self._paint(attrs, 'stroke', o)
            w = ctypes.c_float()
            width = w.value if R.FPDFPageObj_GetStrokeWidth(o, w) else 1.0
            if width <= 0:
                # PDF: the thinnest line the device can draw; about 0.25 pt
                width = 0.25 / max(g.mean_scale(full), 1e-9)
            attrs['stroke-width'] = _fmt(width)
            attrs['stroke-linecap'] = ('butt', 'round', 'square')[max(0, min(2, R.FPDFPageObj_GetLineCap(o)))]
            attrs['stroke-linejoin'] = ('miter', 'round', 'bevel')[max(0, min(2, R.FPDFPageObj_GetLineJoin(o)))]
            attrs['stroke-miterlimit'] = '10'
            nd = R.FPDFPageObj_GetDashCount(o)
            if nd > 0:
                arr = (ctypes.c_float * nd)()
                if R.FPDFPageObj_GetDashArray(o, arr, nd) and any(v > 0 for v in arr):
                    attrs['stroke-dasharray'] = ' '.join(_fmt(v) for v in arr)
                    ph = ctypes.c_float()
                    if R.FPDFPageObj_GetDashPhase(o, ph) and ph.value:
                        attrs['stroke-dashoffset'] = _fmt(ph.value)
        return [ET.Element(SVG + 'path', attrs)]

    def text(self, o, full):
        R = self.R
        mode = R.FPDFTextObj_GetTextRenderMode(o)
        if mode in (3, 7):      # invisible, or only a clip path
            if mode == 7:
                self.skip('text used as a clip path (not clipped)')
            return []
        key = ctypes.cast(o, ctypes.c_void_p).value
        chars = self.chars.get(key)
        if not chars:
            return []
        inv = g.invert(full)
        if inv is None:
            return []
        size = ctypes.c_float()
        fs = size.value if R.FPDFTextObj_GetFontSize(o, size) else 1.0
        if fs <= 0:
            return []
        attrs = {'transform': _mat(g.mul(self.page_m, g.mul(full, (1.0, 0.0, 0.0, -1.0, 0.0, 0.0)))),
                 'font-size': _fmt(fs), '{http://www.w3.org/XML/1998/namespace}space': 'preserve'}
        attrs.update(self._font(key))
        if mode in (0, 2, 4, 6):
            self._paint(attrs, 'fill', o)
        else:
            attrs['fill'] = 'none'
        if mode in (1, 2, 5, 6):
            self._paint(attrs, 'stroke', o)
            w = ctypes.c_float()
            attrs['stroke-width'] = _fmt(w.value if R.FPDFPageObj_GetStrokeWidth(o, w) and w.value > 0
                                         else 0.25 / max(g.mean_scale(full), 1e-9))
        # positions in the text's own space: x along the baseline, y up
        sx = max(abs(full[0]), abs(full[1]), 1e-9)
        sy = max(abs(full[2]), abs(full[3]), 1e-9)
        axis = abs(full[1]) < 1e-6 * sx and abs(full[2]) < 1e-6 * sy
        turned = abs(full[0]) < 1e-6 * sx and abs(full[3]) < 1e-6 * sy
        runs = []
        bad = 0
        prev_end = prev_start = None
        for u, px, py, adv in chars:
            if u in (0, 0xFFFD, 0xFFFE, 0xFFFF) or (u < 32 and u not in (9,)) or 0xD800 <= u <= 0xDFFF:
                if u not in (10, 13):
                    bad += 1
                continue
            ch = chr(u) if u != 9 else ' '
            lx, ly = g.apply(inv, px, py)
            # advance in text space: exact from the character box when the
            # text runs along an axis of the page, else estimated
            if adv is not None and (axis or turned):
                a = (adv[0] / sx) if axis else (adv[1] / sy)
            else:
                a = char_width(ch) * fs
            if (prev_start is not None and runs and abs(lx - prev_start[0]) < 1e-3 * fs
                    and abs(ly - prev_start[1]) < 1e-3 * fs):
                # a ligature's second character (fi -> f, i) sits on the first one
                runs[-1][2] += ch
                continue
            if (prev_end is None or abs(ly - prev_end[1]) > 0.05 * fs or
                    abs(lx - prev_end[0]) > GAP_EM * fs):
                runs.append([lx, ly, ch])
            else:
                runs[-1][2] += ch
            prev_end = (lx + a, ly)
            prev_start = (lx, ly)
        if bad:
            self.skip('characters without Unicode codes (%d dropped)' % bad)
        out = []
        for lx, ly, s in runs:
            if not s.strip():
                continue
            el = ET.Element(SVG + 'text', dict(attrs, x=_fmt(lx), y=_fmt(-ly)))
            el.text = s
            out.append(el)
        if len(out) == 1 and len(runs) == 1:
            style = tuple(sorted((k, v) for k, v in attrs.items() if k != 'transform'))
            self.last_text = (style + (tuple(round(v, 4) for v in full[:4]),), g.apply(full, runs[0][0], runs[0][1]),
                              g.apply(full, *prev_end), fs * g.mean_scale(full), out[0])
        if len(out) > 1:
            grp = ET.Element(SVG + 'g')
            grp.extend(out)
            return [grp]
        return out

    def _font(self, key):
        """font-family, -weight and -style from the font's name and flags."""
        name, flags, weight = self.fonts.get(key, ('', 0, -1))
        name = _SUBSET.sub('', name).strip()
        family, bold, italic = _ps_font(name.replace(',', '-')) if name else ('sans-serif', False, False)
        out = {'font-family': family or 'sans-serif'}
        if bold or weight >= 600 or flags & 0x40000:
            out['font-weight'] = 'bold'
        if italic or flags & 0x40:
            out['font-style'] = 'italic'
        return out

    def image(self, o, full):
        data = self._masked(o)
        mime = 'image/png'
        if data is None:
            data = self._jpeg(o)
            mime = 'image/jpeg'
        if data is None:
            data = self._png(o)
            mime = 'image/png'
        if data is None:
            self.skip('image in an unreadable format')
            return []
        # the image fills the unit square of its matrix; its first row is at the top
        m = g.mul(self.page_m, g.mul(full, (1.0, 0.0, 0.0, -1.0, 0.0, 1.0)))
        href = 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))
        return [ET.Element(SVG + 'image', {'x': '0', 'y': '0', 'width': '1', 'height': '1',
                                           'preserveAspectRatio': 'none', 'transform': _mat(m),
                                           '{http://www.w3.org/1999/xlink}href': href})]

    def _masked(self, o):
        """RGBA PNG of an image with a soft mask, stencil mask or colour-key
        mask, or None for an opaque image.  pdfium applies masks only when it
        renders, so the image is rendered at its own pixel size."""
        R = self.R
        w, h = ctypes.c_uint(), ctypes.c_uint()
        if not R.FPDFImageObj_GetImagePixelSize(o, w, h) or not w.value or not h.value:
            return None
        if w.value * h.value > 50_000_000:
            return None
        old = R.FS_MATRIX()
        R.FPDFPageObj_GetMatrix(o, old)
        R.FPDFPageObj_SetMatrix(o, R.FS_MATRIX(float(w.value), 0.0, 0.0, float(h.value), 0.0, 0.0))
        try:
            bm = R.FPDFImageObj_GetRenderedBitmap(self.doc, self.page, o)
        finally:
            R.FPDFPageObj_SetMatrix(o, old)
        if not bm:
            return None
        try:
            bw, bh = R.FPDFBitmap_GetWidth(bm), R.FPDFBitmap_GetHeight(bm)
            stride, fmt = R.FPDFBitmap_GetStride(bm), R.FPDFBitmap_GetFormat(bm)
            if fmt != R.FPDFBitmap_BGRA or bw <= 0 or bh <= 0:
                return None
            raw = ctypes.string_at(R.FPDFBitmap_GetBuffer(bm), stride * bh)
        finally:
            R.FPDFBitmap_Destroy(bm)
        rows = [raw[r * stride:r * stride + bw * 4] for r in range(bh)]
        if all(row[3::4].count(255) == bw for row in rows):
            return None
        out = []
        for row in rows:
            rgba = bytearray(len(row))
            rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = row[2::4], row[1::4], row[0::4], row[3::4]
            out.append(bytes(rgba))
        return img.encode_png(bw, bh, 6, out)

    def _jpeg(self, o):
        """The PDF's own JPEG data when it can be used unchanged."""
        R = self.R
        if R.FPDFImageObj_GetImageFilterCount(o) != 1:
            return None
        n = R.FPDFImageObj_GetImageFilter(o, 0, None, 0)
        buf = ctypes.create_string_buffer(max(n, 1))
        R.FPDFImageObj_GetImageFilter(o, 0, buf, n)
        if buf.value != b'DCTDecode':
            return None
        meta = R.FPDF_IMAGEOBJ_METADATA()
        if not R.FPDFImageObj_GetImageMetadata(o, self.page, meta):
            return None
        if meta.colorspace not in (R.FPDF_COLORSPACE_DEVICEGRAY, R.FPDF_COLORSPACE_DEVICERGB):
            return None
        n = R.FPDFImageObj_GetImageDataRaw(o, None, 0)
        if n <= 0:
            return None
        buf = ctypes.create_string_buffer(n)
        R.FPDFImageObj_GetImageDataRaw(o, buf, n)
        data = buf.raw[:n]
        return data if data.startswith(b'\xff\xd8') else None

    def _png(self, o):
        R = self.R
        bm = R.FPDFImageObj_GetBitmap(o)
        if not bm:
            return None
        try:
            w, h = R.FPDFBitmap_GetWidth(bm), R.FPDFBitmap_GetHeight(bm)
            stride, fmt = R.FPDFBitmap_GetStride(bm), R.FPDFBitmap_GetFormat(bm)
            if w <= 0 or h <= 0:
                return None
            ptr = R.FPDFBitmap_GetBuffer(bm)
            raw = ctypes.string_at(ptr, stride * h)
        finally:
            R.FPDFBitmap_Destroy(bm)
        bpp = {R.FPDFBitmap_Gray: 1, R.FPDFBitmap_BGR: 3, R.FPDFBitmap_BGRx: 4, R.FPDFBitmap_BGRA: 4}.get(fmt)
        if bpp is None:
            return None
        rows = []
        for r in range(h):
            row = raw[r * stride:r * stride + w * bpp]
            if bpp == 1:
                rows.append(row)
                continue
            rgb = bytearray(w * (4 if fmt == R.FPDFBitmap_BGRA else 3))
            k = len(rgb) // w
            rgb[0::k], rgb[1::k], rgb[2::k] = row[2::bpp], row[1::bpp], row[0::bpp]
            if k == 4:
                rgb[3::4] = row[3::4]
            rows.append(bytes(rgb))
        ctype = 0 if bpp == 1 else 6 if fmt == R.FPDFBitmap_BGRA else 2
        return img.encode_png(w, h, ctype, rows)


def _path_data(subs):
    out = []
    for start, segs, closed in subs:
        out.append('M%s %s' % (_fmt(start[0]), _fmt(start[1])))
        for s in segs:
            out.append(s[0] + ' '.join(_fmt(v) for v in s[1:]))
        if closed:
            out.append('Z')
    return ' '.join(out)


def _rect_contains(subs, bounds):
    """True when the path is one axis-aligned rectangle holding bounds (l, b, r, t)."""
    if len(subs) != 1:
        return False
    start, segs, _ = subs[0]
    if any(s[0] != 'L' for s in segs):
        return False
    pts = [start] + [(s[1], s[2]) for s in segs]
    xs = sorted(set(round(p[0], 3) for p in pts))
    ys = sorted(set(round(p[1], 3) for p in pts))
    if len(xs) != 2 or len(ys) != 2:
        return False
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        if abs(a[0] - b[0]) > 1e-3 and abs(a[1] - b[1]) > 1e-3:
            return False
    tol = 0.01
    return (xs[0] - tol <= bounds[0] and bounds[2] <= xs[1] + tol and
            ys[0] - tol <= bounds[1] and bounds[3] <= ys[1] + tol)


def read(data, pages):
    """[(svg root, page number, skipped)] for the pages asked for."""
    pdfium, raw = _pdfium()
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ValueError('cannot read the PDF: %s' % e) from e
    try:
        numbers = page_numbers(pages, len(doc))
        out = []
        size = None
        for n in numbers:
            page = doc[n - 1]
            try:
                p = _Page(raw, doc.raw, page.raw, n, size)
                root = p.read()
                if size is None:
                    size = (p.w, p.h)
                out.append((root, n, p.skipped))
            finally:
                page.close()
        return out
    finally:
        doc.close()
