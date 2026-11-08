"""Walk the SVG tree and build the DrawingML shape tree."""

import bisect
import hashlib
import math
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

from . import _drawingml as dml
from . import _geom as g
from . import _images as img
from . import _style as css
from . import _text as txt

SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_HREF = '{http://www.w3.org/1999/xlink}href'
MARKER_LIMIT = 2000      # default: above this many copies per series, one shape per series
MAX_SLIDE = 51206400     # 56 inch
MIN_SLIDE = 914400       # 1 inch
MAX_USE_DEPTH = 24

_URL = re.compile(r'url\(\s*["\']?#([^)"\']+)["\']?\s*\)\s*(.*)$')
_SHAPES = ('path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon')
_NOT_RENDERED = frozenset('''defs symbol marker clipPath mask pattern linearGradient
radialGradient style title desc metadata script filter font font-face cursor view
solidcolor meshgradient hatch'''.split())
_ANIMATION = frozenset('animate animateMotion animateTransform animateColor set'.split())
# glyph outlines: cairo (R svg(), pdftocairo) names them glyph-F-N (older: glyphF-N);
# Matplotlib names them <font PostScript name>-<glyph number in hex>
_CAIRO_GLYPH = re.compile(r'glyph-?\d+-\d+$')
_MPL_GLYPH = re.compile(r'(.+)-[0-9a-f]+$')


class Clip:
    """Clip region in slide EMU: a rectangle, optionally with the exact outline."""
    __slots__ = ('rect', 'shape')

    def __init__(self, rect, shape=None):
        self.rect = rect
        self.shape = shape


def _intersect(a, b):
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r


def _inside(bb, r):
    return bb[0] >= r[0] and bb[1] >= r[1] and bb[2] <= r[2] and bb[3] <= r[3]


def _disjoint(bb, r):
    return bb[2] < r[0] or bb[0] > r[2] or bb[3] < r[1] or bb[1] > r[3]


def _parse_viewbox(v):
    nums = g.parse_numbers(v)
    if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
        return nums
    return None


def viewbox_transform(vb, w, h, par):
    """Map viewBox (x, y, vw, vh) onto a w x h viewport (preserveAspectRatio)."""
    x, y, vw, vh = vb
    sx, sy = w / vw, h / vh
    par = (par or 'xMidYMid meet').split()
    if par and par[0] == 'defer':
        par = par[1:]
    align = par[0] if par else 'xMidYMid'
    if align == 'none':
        return (sx, 0.0, 0.0, sy, -x * sx, -y * sy)
    s = max(sx, sy) if (len(par) > 1 and par[1] == 'slice') else min(sx, sy)
    tx, ty = -x * s, -y * s
    if 'xMid' in align:
        tx += (w - vw * s) / 2
    elif 'xMax' in align:
        tx += w - vw * s
    if 'YMid' in align:
        ty += (h - vh * s) / 2
    elif 'YMax' in align:
        ty += h - vh * s
    return (s, 0.0, 0.0, s, tx, ty)


class Converter:
    def __init__(self, root, fonts=None, series_limit=MARKER_LIMIT, grid=None):
        self.root = root
        self.grid = grid         # path coordinate grid in EMU (precision=), or None
        # repeated <use> copies and markers of one series above this many
        # become one shape while reading (None: never)
        self.series_limit = series_limit
        self.absorbed = 0        # shapes that went into those combined shapes
        self.fonts = dict(fonts or {})
        self.found_fonts = {}    # font names of the text, as written, in first-seen order
        self.parents = {c: p for p in root.iter() for c in p}
        # element -> text of the XML comment right before it (Matplotlib
        # writes each outlined text's string so)
        self.comment_before = {}
        for p in root.iter():
            prev = None
            for c in p:
                if isinstance(c.tag, str) and prev is not None and prev.tag is ET.Comment:
                    self.comment_before[c] = prev.text or ''
                prev = c
        self.ids = {}
        rules = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            i = el.get('id')
            if i is not None and i not in self.ids:
                self.ids[i] = el
            if self.local(el) == 'style':
                t = el.get('type', 'text/css')
                if 'css' in t:
                    rules.extend(css.parse_stylesheet(''.join(el.itertext()), len(rules)))
        self.sheet = css.StyleSheet(rules, self.parents)
        self._spec = {}
        self._doc_style = {}
        self._order = None
        self._clip_cache = {}
        self._local_cache = {}
        self.skipped = {}
        self.media = []          # [(filename, bytes)]
        self._media_ids = {}     # digest -> rId
        self.viewport = [(100.0, 100.0)]
        self.use_stack = []

    # --- helpers -------------------------------------------------------

    @staticmethod
    def local(el):
        tag = el.tag
        if not isinstance(tag, str):
            return ''
        if tag[0] == '{':
            ns, _, name = tag[1:].partition('}')
            return name if ns == SVG_NS else ''
        return tag

    def spec(self, el):
        s = self._spec.get(el)
        if s is None:
            s = css.specified(el, self.sheet)
            self._spec[el] = s
        return s

    def doc_style(self, el):
        """Computed style of an element from its place in the document."""
        st = self._doc_style.get(el)
        if st is None:
            p = self.parents.get(el)
            base = self.doc_style(p) if p is not None else css.ROOT_STYLE
            st = css.cascade(base, self.spec(el))
            self._doc_style[el] = st
        return st

    def describe(self, el):
        i = el.get('id')
        if i:
            return '#' + i
        if self._order is None:
            self._order = {e: n for n, e in enumerate(self.root.iter(), 1)}
        return '<%s> element %d' % (self.local(el) or css.local_name(el.tag), self._order.get(el, 0))

    def skip(self, feature, el):
        key = (feature, self.describe(el) if el is not None else '')
        self.skipped[key] = None

    def href(self, el):
        v = el.get(XLINK_HREF) or el.get('href')
        if v and v.startswith('#'):
            return v[1:]
        return v

    def vp_len(self, v, axis, st=None, default=0.0):
        w, h = self.viewport[-1]
        ref = w if axis == 'x' else h if axis == 'y' else math.hypot(w, h) / math.sqrt(2)
        fs = css.number(st.get('font-size', '16'), 16.0) if st else 16.0
        return css.length(v, ref=ref, font_size=fs, default=default)

    # --- document ------------------------------------------------------

    def run(self):
        root = self.root
        st = css.cascade(css.ROOT_STYLE, self.spec(root))
        vb = _parse_viewbox(root.get('viewBox'))
        wv, hv = root.get('width'), root.get('height')
        w = None if (wv is None or css.is_percent(wv)) else css.length(wv)
        h = None if (hv is None or css.is_percent(hv)) else css.length(hv)
        if vb:
            if w is None and h is None:
                w, h = vb[2], vb[3]
            elif w is None:
                w = h * vb[2] / vb[3]
            elif h is None:
                h = w * vb[3] / vb[2]
        w = w if w and w > 0 else 96.0
        h = h if h and h > 0 else 96.0
        k = dml.EMU_PER_PX
        if w * k > MAX_SLIDE or h * k > MAX_SLIDE:
            k = min(MAX_SLIDE / w, MAX_SLIDE / h)
        self.slide_w = max(MIN_SLIDE, int(round(w * k)))
        self.slide_h = max(MIN_SLIDE, int(round(h * k)))
        m = g.scale(k)
        if vb:
            m = g.mul(m, viewbox_transform(vb, w, h, root.get('preserveAspectRatio')))
            self.viewport = [(vb[2], vb[3])]
        else:
            self.viewport = [(w, h)]
        clip = Clip((0.0, 0.0, w * k, h * k))
        alpha = css.number(st.get('opacity', '1'), 1.0)
        nodes = self.children(root, m, st, clip, alpha)
        return dml.collapse(nodes)

    # --- traversal -------------------------------------------------------

    def children(self, parent, m, st, clip, alpha):
        kids = [c for c in parent if isinstance(c.tag, str)]
        out = []
        i = 0
        n = len(kids)
        run_end = 0      # end of a <use> run already found too short to combine
        while i < n:
            c = kids[i]
            if i >= run_end and self.series_limit is not None and self.local(c) == 'use':
                ref = self.href(c)
                j = i + 1
                while j < n and self.local(kids[j]) == 'use' and self.href(kids[j]) == ref:
                    j += 1
                if self.series_limit is not None and j - i > self.series_limit and not any(
                        k in self.spec(u) for u in kids[i:j] for k in ('clip-path', 'filter', 'mask')):
                    target = self.ids.get(ref)
                    if target is not None and self.local(target) in _SHAPES:
                        out.extend(self.use_series(kids[i:j], target, m, st, clip, alpha))
                        i = j
                        continue
                run_end = j
            out.extend(self.render(c, m, st, clip, alpha))
            i += 1
        return out

    def render(self, el, m, pst, clip, alpha):
        tag = self.local(el)
        if not tag:
            if css.local_name(el.tag) == 'flowRoot':
                self.skip('flowed text (Inkscape flowRoot)', el)
            return []
        if tag in _NOT_RENDERED:
            return []
        if tag in _ANIMATION:
            self.skip('animation', el)
            return []
        if tag == 'flowRoot':
            self.skip('flowed text (Inkscape flowRoot)', el)
            return []
        if tag == 'foreignObject':
            self.skip('foreignObject', el)
            return []
        sp = self.spec(el)
        st = css.cascade(pst, sp)
        if st.get('display') == 'none':
            return []
        a = alpha * max(0.0, min(1.0, css.number(st.get('opacity', '1'), 1.0)))
        if a <= 0:
            return []
        if st.get('filter', 'none') != 'none':
            self.skip('filter', el)
        if st.get('mask', 'none') != 'none':
            self.skip('mask', el)
        tr = el.get('transform')
        if tr:
            m = g.mul(m, g.parse_transform(tr))
        cp = st.get('clip-path', 'none')
        if cp != 'none':
            clip = self.apply_clip(cp, m, el, clip)
            if clip is None:
                return []
        if tag == 'g':
            glyphs = self.glyph_uses(el)
            if glyphs is not None:
                out = None
                if el in self.comment_before:
                    out = self.mpl_text(el, glyphs, m, st, clip, a)
                if out is None:
                    out = self.glyph_shape(el, glyphs, m, st, clip, a)
                if out is not None:
                    return out
        if tag in ('g', 'a'):
            return [dml.Group(el.get('id'), self.children(el, m, st, clip, a))]
        if tag == 'switch':
            for c in el:
                if isinstance(c.tag, str) and self.local(c) and self._switch_ok(c):
                    return [dml.Group(el.get('id'), self.render(c, m, st, clip, a))]
            return []
        if tag == 'svg':
            return self.nested_svg(el, m, st, clip, a, None, None)
        if tag in _SHAPES or tag in ('text', 'image', 'use'):
            for c in el:
                if self.local(c) in _ANIMATION:
                    self.skip('animation', c)
        if tag == 'use':
            return self.use(el, m, st, clip, a)
        if tag in _SHAPES:
            return self.shape(el, st, m, clip, a)
        if tag == 'text':
            return self.text(el, st, m, clip, a)
        if tag == 'image':
            return self.image(el, st, m, clip, a)
        return []

    @staticmethod
    def _switch_ok(el):
        if el.get('requiredExtensions'):
            return False
        lang = el.get('systemLanguage')
        if lang and not any(p.strip().lower().startswith('en') for p in lang.split(',')):
            return False
        return True

    def nested_svg(self, el, m, st, clip, a, w_over, h_over):
        x = self.vp_len(el.get('x'), 'x', st)
        y = self.vp_len(el.get('y'), 'y', st)
        w = w_over if w_over is not None else self.vp_len(el.get('width', '100%'), 'x', st)
        h = h_over if h_over is not None else self.vp_len(el.get('height', '100%'), 'y', st)
        if w <= 0 or h <= 0:
            return []
        m2 = g.mul(m, g.translate(x, y))
        if st.get('overflow', 'hidden') not in ('visible', 'auto'):
            clip = self.clip_rect_local(m2, 0, 0, w, h, clip, el)
            if clip is None:
                return []
        vb = _parse_viewbox(el.get('viewBox'))
        if vb:
            m2 = g.mul(m2, viewbox_transform(vb, w, h, el.get('preserveAspectRatio')))
            self.viewport.append((vb[2], vb[3]))
        else:
            self.viewport.append((w, h))
        try:
            return [dml.Group(el.get('id'), self.children(el, m2, st, clip, a))]
        finally:
            self.viewport.pop()

    def use(self, el, m, st, clip, a):
        ref = self.href(el)
        target = self.ids.get(ref) if ref else None
        if target is None:
            if ref and not ref.startswith('#'):
                self.skip('external <use> reference', el)
            return []
        if target in self.use_stack or len(self.use_stack) > MAX_USE_DEPTH:
            return []
        m = g.mul(m, g.translate(self.vp_len(el.get('x'), 'x', st), self.vp_len(el.get('y'), 'y', st)))
        self.use_stack.append(target)
        try:
            tag = self.local(target)
            if tag == 'symbol':
                tst = css.cascade(st, self.spec(target))
                if tst.get('display') == 'none':
                    return []
                vb = _parse_viewbox(target.get('viewBox'))
                m2 = m
                if vb:
                    w = self.vp_len(el.get('width', '100%'), 'x', st)
                    h = self.vp_len(el.get('height', '100%'), 'y', st)
                    if w <= 0 or h <= 0:
                        return []
                    m2 = g.mul(m, viewbox_transform(vb, w, h, target.get('preserveAspectRatio')))
                tr = target.get('transform')
                if tr:
                    m2 = g.mul(m2, g.parse_transform(tr))
                return [dml.Group(el.get('id'), self.children(target, m2, tst, clip, a))]
            if tag == 'svg':
                tst = css.cascade(st, self.spec(target))
                w = self.vp_len(el.get('width'), 'x', st) if el.get('width') else None
                h = self.vp_len(el.get('height'), 'y', st) if el.get('height') else None
                return self.nested_svg(target, m, tst, clip, a, w, h)
            return [dml.Group(el.get('id'), self.render(target, m, st, clip, a))]
        finally:
            self.use_stack.pop()

    # --- clipping --------------------------------------------------------

    def clip_rect_local(self, m, x, y, w, h, clip, el):
        dec = g.decompose(m)
        r = g.rect_corners_bbox(m, x, y, w, h)
        if dec is None or abs(math.sin(dec[0] * 2)) > 1e-9:
            self.skip('clip region approximated by its bounding box', el)
        if clip is not None:
            r = _intersect(clip.rect, r)
            shape = clip.shape
        else:
            shape = None
        if r[0] >= r[2] or r[1] >= r[3]:
            return None
        return Clip(r, shape)

    def apply_clip(self, value, m, el, clip):
        mt = _URL.match(value)
        if not mt:
            return clip
        cp = self.ids.get(mt.group(1))
        if cp is None or self.local(cp) != 'clipPath':
            return clip
        if cp.get('clipPathUnits') == 'objectBoundingBox':
            self.skip('clip-path in objectBoundingBox units', el)
            return clip
        mc = m
        if cp.get('transform'):
            mc = g.mul(m, g.parse_transform(cp.get('transform')))
        key = (cp, mc)
        res = self._clip_cache.get(key)
        if res is None:
            res = self._clip_region(cp, mc)
            self._clip_cache[key] = res
        rect, shape, exact = res
        if rect is None:
            return None
        if not exact:
            self.skip('non-rectangular clip-path (approximated)', cp)
        if clip is not None:
            rect = _intersect(clip.rect, rect)
            if shape is None:
                shape = clip.shape
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            return None
        return Clip(rect, shape)

    def _clip_region(self, cp, m):
        bb = None
        subs_all = []
        exact = True
        kids = [c for c in cp if isinstance(c.tag, str) and self.local(c)]
        for c in kids:
            tag = self.local(c)
            if tag == 'use':
                ref = self.ids.get(self.href(c) or '')
                mc = g.mul(m, g.parse_transform(c.get('transform')))
                mc = g.mul(mc, g.translate(self.vp_len(c.get('x'), 'x'), self.vp_len(c.get('y'), 'y')))
                c = ref
                if c is None:
                    continue
                tag = self.local(c)
            else:
                mc = m
            if tag == 'text':
                exact = False
                continue
            if tag not in _SHAPES:
                continue
            if self.doc_style(c).get('display') == 'none':
                continue
            if c.get('transform'):
                mc = g.mul(mc, g.parse_transform(c.get('transform')))
            subs, prim = self.local_path(c, self.doc_style(c))
            if not subs:
                continue
            ssubs = g.transform_path(subs, mc)
            b = g.path_bbox(ssubs)
            if b is None:
                continue
            bb = g.bbox_union(bb, b)
            subs_all.extend(ssubs)
            if len(kids) > 1 or prim is None or prim[0] != 'rect' or prim[5] or prim[6]:
                exact = exact and self._is_axis_rect(ssubs)
            else:
                dec = g.decompose(mc)
                if dec is None or abs(math.sin(dec[0] * 2)) > 1e-9:
                    exact = False
        if bb is None:
            return (None, None, True)
        if exact:
            return (bb, None, True)
        return (bb, subs_all, False)

    @staticmethod
    def _is_axis_rect(subs):
        subs = [sp for sp in subs if sp[1]]   # cairo appends an empty "M x y"
        if len(subs) != 1:
            return False
        (sx, sy), segs, _ = subs[0]
        pts = [(sx, sy)] + [(s[1], s[2]) for s in segs if s[0] == 'L']
        if len(pts) != len(segs) + 1:
            return False
        if len(pts) > 4 and abs(pts[-1][0] - sx) < 1e-6 and abs(pts[-1][1] - sy) < 1e-6:
            pts = pts[:-1]
        if len(pts) != 4:
            return False
        xs = sorted(set(round(p[0], 3) for p in pts))
        ys = sorted(set(round(p[1], 3) for p in pts))
        return len(xs) == 2 and len(ys) == 2

    # --- geometry of basic shapes ------------------------------------------

    def local_path(self, el, st):
        """(subpaths, primitive) in the element's user space.
        primitive: ('rect', x, y, w, h, rx, ry) | ('ellipse', cx, cy, rx, ry) | None."""
        tag = self.local(el)
        if tag == 'path':
            d = el.get('d') or ''
            subs = self._local_cache.get(d)
            if subs is None:
                subs = g.parse_path(d)
                if len(d) < 4000:
                    self._local_cache[d] = subs
            return subs, None
        L = self.vp_len
        if tag == 'rect':
            x, y = L(el.get('x'), 'x', st), L(el.get('y'), 'y', st)
            w, h = L(el.get('width'), 'x', st), L(el.get('height'), 'y', st)
            if w <= 0 or h <= 0:
                return [], None
            rxv, ryv = el.get('rx'), el.get('ry')
            if rxv is None and 'rx' in st:
                rxv = st['rx']
            rx = L(rxv, 'x', st) if rxv not in (None, 'auto') else None
            ry = L(ryv, 'y', st) if ryv not in (None, 'auto') else None
            if rx is None:
                rx = ry or 0.0
            if ry is None:
                ry = rx
            rx, ry = min(max(rx, 0.0), w / 2), min(max(ry, 0.0), h / 2)
            if rx and ry:
                k = 0.5522847498
                subs = [[(x + rx, y), [
                    ('L', x + w - rx, y),
                    ('C', x + w - rx + k * rx, y, x + w, y + ry - k * ry, x + w, y + ry),
                    ('L', x + w, y + h - ry),
                    ('C', x + w, y + h - ry + k * ry, x + w - rx + k * rx, y + h, x + w - rx, y + h),
                    ('L', x + rx, y + h),
                    ('C', x + rx - k * rx, y + h, x, y + h - ry + k * ry, x, y + h - ry),
                    ('L', x, y + ry),
                    ('C', x, y + ry - k * ry, x + rx - k * rx, y, x + rx, y)], True]]
            else:
                rx = ry = 0.0
                subs = [[(x, y), [('L', x + w, y), ('L', x + w, y + h), ('L', x, y + h)], True]]
            return subs, ('rect', x, y, w, h, rx, ry)
        if tag in ('circle', 'ellipse'):
            cx, cy = L(el.get('cx'), 'x', st), L(el.get('cy'), 'y', st)
            if tag == 'circle':
                rx = ry = L(el.get('r'), 'xy', st)
            else:
                rx, ry = L(el.get('rx'), 'x', st), L(el.get('ry'), 'y', st)
            if rx <= 0 or ry <= 0:
                return [], None
            k = 0.5522847498
            subs = [[(cx + rx, cy), [
                ('C', cx + rx, cy + k * ry, cx + k * rx, cy + ry, cx, cy + ry),
                ('C', cx - k * rx, cy + ry, cx - rx, cy + k * ry, cx - rx, cy),
                ('C', cx - rx, cy - k * ry, cx - k * rx, cy - ry, cx, cy - ry),
                ('C', cx + k * rx, cy - ry, cx + rx, cy - k * ry, cx + rx, cy)], True]]
            return subs, ('ellipse', cx, cy, rx, ry)
        if tag == 'line':
            x1, y1 = L(el.get('x1'), 'x', st), L(el.get('y1'), 'y', st)
            x2, y2 = L(el.get('x2'), 'x', st), L(el.get('y2'), 'y', st)
            return [[(x1, y1), [('L', x2, y2)], False]], None
        if tag in ('polyline', 'polygon'):
            v = g.parse_numbers(el.get('points'))
            pts = list(zip(v[0::2], v[1::2]))
            if len(pts) < 2:
                return [], None
            return [[pts[0], [('L', px, py) for px, py in pts[1:]], tag == 'polygon']], None
        return [], None

    # --- paint -------------------------------------------------------------

    def paint(self, st, which, el, alpha):
        """None | ('solid', hex, a) | ('grad', element, a)."""
        v = st.get(which)
        if v is None or v == 'none':
            return None
        op = alpha * max(0.0, min(1.0, css.number(st.get(which + '-opacity', '1'), 1.0)))
        mt = _URL.match(v)
        if mt:
            ref = self.ids.get(mt.group(1))
            tag = self.local(ref) if ref is not None else ''
            if tag in ('linearGradient', 'radialGradient'):
                return ('grad', ref, op)
            if tag == 'pattern':
                self.skip('pattern fill', el)
            elif tag:
                self.skip('unsupported paint server <%s>' % tag, el)
            fb = mt.group(2).strip()
            if not fb:
                return None
            v = fb
        c = css.parse_color(v, st.get('color', 'black'))
        if c is None:
            return None
        return ('solid', c[0], c[1] * op)

    def gradient_info(self, gel):
        """Merged attributes and stops of a gradient through its href chain."""
        attrs = {}
        stops = None
        seen = set()
        cur = gel
        kind = self.local(gel)
        while cur is not None and cur not in seen:
            seen.add(cur)
            for k, v in cur.attrib.items():
                k = css.local_name(k) if k.startswith('{') else k
                attrs.setdefault(k, v)
            if stops is None:
                ss = [s for s in cur if self.local(s) == 'stop']
                if ss:
                    stops = ss
            ref = self.href(cur)
            cur = self.ids.get(ref) if ref else None
        out = []
        last = 0.0
        for s in stops or []:
            sst = self.doc_style(s)
            off = max(0.0, min(1.0, css.number(s.get('offset', '0'), 0.0)))
            off = max(off, last)
            last = off
            col = css.parse_color(sst.get('stop-color', 'black'), sst.get('color', 'black'))
            op = max(0.0, min(1.0, css.number(sst.get('stop-opacity', '1'), 1.0)))
            if col is None:
                col, op = ('000000', 0.0)
            else:
                op *= col[1]
                col = col[0]
            out.append((off, col, op))
        return kind, attrs, out

    def fill_xml(self, p, bbox, local_bbox, m, el):
        """DrawingML fill for paint p on a shape whose slide box is bbox."""
        if p is None:
            return '<a:noFill/>'
        if p[0] == 'solid':
            return dml.solid(p[1], p[2])
        kind, attrs, stops = self.gradient_info(p[1])
        op = p[2]
        if not stops:
            return '<a:noFill/>'
        stops = [(o, c, a * op) for o, c, a in stops]
        if len(stops) == 1:
            return dml.solid(stops[0][1], stops[0][2])
        if attrs.get('spreadMethod', 'pad') != 'pad':
            self.skip('gradient spreadMethod %s (drawn as pad)' % attrs.get('spreadMethod'), el)
        t = self._gradient_space(attrs, local_bbox, m)
        if t is None:
            return dml.solid(stops[-1][1], stops[-1][2])
        val = self._gradient_val(attrs)
        x0, y0, x1, y1 = bbox
        bw, bh = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
        if kind == 'linearGradient':
            gx1, gy1 = val('x1', 'x', '0%'), val('y1', 'y', '0%')
            gx2, gy2 = val('x2', 'x', '100%'), val('y2', 'y', '0%')
            vx, vy = gx2 - gx1, gy2 - gy1
            l2 = vx * vx + vy * vy
            ti = g.invert(t)
            if l2 <= 0 or ti is None:
                return dml.solid(stops[-1][1], stops[-1][2])

            def f(px, py):
                ux, uy = g.apply(ti, px, py)
                return ((ux - gx1) * vx + (uy - gy1) * vy) / l2
            vals = [f(x0, y0), f(x1, y0), f(x0, y1), f(x1, y1)]
            fmin, fmax = min(vals), max(vals)
            if fmax - fmin < 1e-9:
                c = _stop_at(stops, fmin)
                return dml.solid(c[0], c[1])
            gxd = ti[0] * vx + ti[1] * vy
            gyd = ti[2] * vx + ti[3] * vy
            ang = math.atan2(gyd, gxd)
            mapped = [((o - fmin) / (fmax - fmin), c, a) for o, c, a in stops]
            return dml.grad_fill(_clip_stops(mapped), lin_angle=ang)
        cx, cy = val('cx', 'x', '50%'), val('cy', 'y', '50%')
        r = val('r', 'xy', '50%')
        if r <= 0:
            return dml.solid(stops[-1][1], stops[-1][2])
        if 'fx' in attrs or 'fy' in attrs:
            fx, fy = val('fx', 'x', attrs.get('cx', '50%')), val('fy', 'y', attrs.get('cy', '50%'))
            if math.hypot(fx - cx, fy - cy) > 1e-3 * r:
                self.skip('radial gradient focal point (centred)', el)
        ccx, ccy = g.apply(t, cx, cy)
        rx = r * math.hypot(t[0], t[2])   # horizontal half-extent of the gradient ellipse
        ry = r * math.hypot(t[1], t[3])   # vertical half-extent
        fu = min(1.0, max(0.0, (ccx - x0) / bw))
        fv = min(1.0, max(0.0, (ccy - y0) / bh))
        # DrawingML's circle gradient scales with the shape box; when the SVG
        # gradient's aspect differs from the box, follow the axis along which
        # the colour changes most inside the shape.
        rn = min(rx / bw, ry / bh)
        rmax = max(math.hypot(cu - fu, cv - fv) for cu in (0.0, 1.0) for cv in (0.0, 1.0))
        if rmax <= 0:
            return dml.solid(stops[0][1], stops[0][2])
        mapped = [(o * rn / rmax, c, a) for o, c, a in stops]
        return dml.grad_fill(_clip_stops(mapped), focus=(fu, fv))

    def line_xml(self, p, sw_emu, sw_user, st, bbox, local_bbox, m, el):
        if p is None or sw_emu <= 0:
            return dml.NO_LINE
        fill = self.fill_xml(p, bbox, local_bbox, m, el)
        dash = None
        da = st.get('stroke-dasharray', 'none')
        if da and da != 'none' and sw_user > 0:
            vals = [self.vp_len(v, 'xy', st) for v in re.split(r'[\s,]+', da.strip()) if v]
            if vals and all(v >= 0 for v in vals) and sum(vals) > 0:
                if len(vals) % 2:
                    vals = vals * 2
                dash = [(int(round(vals[i] / sw_user * 100000)), int(round(vals[i + 1] / sw_user * 100000)))
                        for i in range(0, len(vals), 2)]
                off = st.get('stroke-dashoffset')
                if off and self.vp_len(off, 'xy', st) != 0:
                    self.skip('stroke-dashoffset', el)
        return dml.line(fill, sw_emu, st.get('stroke-linecap', 'butt'), st.get('stroke-linejoin', 'miter'),
                        css.number(st.get('stroke-miterlimit', '4'), 4.0), dash)

    # --- shapes ------------------------------------------------------------

    def stroke_width(self, st):
        return max(0.0, self.vp_len(st.get('stroke-width', '1'), 'xy', st, 1.0))

    def shape(self, el, st, m, clip, a):
        if st.get('visibility', 'visible') in ('hidden', 'collapse'):
            return []
        tag = self.local(el)
        subs, prim = self.local_path(el, st)
        if not subs:
            return []
        fill = None if tag == 'line' else self.paint(st, 'fill', el, a)
        stroke = self.paint(st, 'stroke', el, a)
        sw = self.stroke_width(st)
        if sw <= 0:
            stroke = None
        out = []
        if fill is not None or stroke is not None:
            out.extend(self.emit_shape(el, st, m, subs, prim, fill, stroke, sw, clip, el.get('id')))
        if tag in ('path', 'line', 'polyline', 'polygon'):
            out.extend(self.markers(el, st, m, subs, sw, clip, a))
        return out

    def emit_shape(self, el, st, m, subs, prim, fill, stroke, sw, clip, label):
        sw_emu = sw * g.mean_scale(m)
        margin = sw_emu / 2 if stroke is not None else 0.0
        local_bbox = g.path_bbox(subs)
        dec = g.decompose(m) if prim is not None else None
        # 1. preset geometry (rectangles, rounded rectangles, ellipses)
        if dec is not None:
            theta, sx, sy, _ = dec
            if prim[0] == 'rect':
                _, x, y, w, h, rx, ry = prim
                cx, cy = x + w / 2, y + h / 2
            else:
                _, cx, cy, rx, ry = prim
                w, h = 2 * rx, 2 * ry
            W, H = w * sx, h * sy
            C = g.apply(m, cx, cy)
            box = (C[0] - W / 2, C[1] - H / 2, C[0] + W / 2, C[1] + H / 2)
            rotated = abs(math.sin(theta)) > 1e-9 or math.cos(theta) < 0
            vis = g.rect_corners_bbox(m, cx - w / 2, cy - h / 2, w, h)
            grad = (fill is not None and fill[0] == 'grad') or (stroke is not None and stroke[0] == 'grad')
            clipped = clip is not None and not _inside(_grow(vis, margin), clip.rect)
            geom = None
            if prim[0] == 'ellipse':
                geom = dml.prst_geom('ellipse')
            elif not rx:
                geom = dml.prst_geom('rect')
            elif abs(rx * sx - ry * sy) <= 1e-6 * max(W, H):
                geom = dml.prst_geom('roundRect', int(round(min(50000, rx * sx / max(min(W, H), 1e-9) * 100000))))
            radial = self._radial(fill) or self._radial(stroke)
            if geom and not (grad and rotated) and not clipped and not radial:
                fx = self.fill_xml(fill, box, local_bbox, m, el)
                lx = self.line_xml(stroke, sw_emu, sw, st, box, local_bbox, m, el)
                body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' +
                        dml.xfrm(box[0], box[1], W, H, theta if rotated else 0.0) +
                        geom + fx + lx + '</p:spPr></p:sp>')
                return [_look(dml.Leaf(vis, 'shape', label, '<p:sp><p:nvSpPr>', body), fx, lx,
                              ('prst', prim[0] if prim[0] == 'ellipse' else 'rect' if not rx else 'roundRect',
                               box, theta if rotated else 0.0,
                               min(0.5, rx * sx / max(min(W, H), 1e-9)) if rx and prim[0] != 'ellipse' else 0.0))]
            if geom and clipped and not rotated and fill is not None and stroke is None \
                    and prim[0] == 'rect' and not rx and clip.shape is None:
                r = _intersect(box, clip.rect)
                if r[0] >= r[2] or r[1] >= r[3]:
                    return []
                fx = self.fill_xml(fill, r, local_bbox, m, el)
                body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' +
                        dml.xfrm(r[0], r[1], r[2] - r[0], r[3] - r[1]) +
                        geom + fx + dml.NO_LINE + '</p:spPr></p:sp>')
                return [_look(dml.Leaf(r, 'shape', label, '<p:sp><p:nvSpPr>', body), fx, dml.NO_LINE,
                              ('prst', 'rect', r, 0.0, 0.0))]
        # 2. free-form path
        ssubs = g.transform_path(subs, m)
        bb = g.path_bbox(ssubs)
        if bb is None:
            return []
        paths = None   # list of (subs, filled, stroked)
        if clip is not None and not _inside(_grow(bb, margin), clip.rect):
            if _disjoint(_grow(bb, margin), clip.rect):
                return []
            if (clip.shape is not None and prim is not None and prim[0] == 'rect' and stroke is None
                    and _inside(_intersect(_bbox_of(clip.shape), clip.rect), bb)):
                # a rectangle covering the whole clip outline: the result is the outline
                paths = [(clip.shape, True, False)]
            else:
                polys = g.flatten(ssubs)
                paths = []
                if fill is not None:
                    fpolys = []
                    for pts, _closed in polys:
                        c = g.clip_polygon(pts, clip.rect)
                        if len(c) >= 3:
                            fpolys.append((c, True))
                    if fpolys:
                        paths.append((g.polys_to_path(fpolys), True, False))
                if stroke is not None:
                    lines = []
                    # PowerPoint draws caps unclipped: a square or round cap
                    # at an end cut by the clip would cross its edge
                    pull = st.get('stroke-linecap', 'butt') in ('square', 'round')
                    for pts, closed in polys:
                        p = pts + [pts[0]] if closed else pts
                        for part in g.clip_polyline(p, clip.rect):
                            if pull:
                                part = g.pull_back_ends(part, sw_emu / 2, clip.rect)
                                if part is None:
                                    continue
                            lines.append((part, False))
                    if lines:
                        paths.append((g.polys_to_path(lines), False, True))
            if not paths:
                return []
            bb = None
            for s, _, _ in paths:
                bb = g.bbox_union(bb, g.path_bbox(s))
        else:
            paths = self._fill_rule_paths(ssubs, st, fill is not None, stroke is not None)
        x0, y0 = bb[0], bb[1]
        W, H = max(bb[2] - x0, 1.0), max(bb[3] - y0, 1.0)
        box = (x0, y0, x0 + W, y0 + H)
        geo = dml.cust_geom([(g.transform_path(s, (1, 0, 0, 1, -x0, -y0)), W, H, f, k)
                             for s, f, k in paths], self.grid)
        fx = self.fill_xml(fill, box, local_bbox, m, el)
        lx = self.line_xml(stroke, sw_emu, sw, st, box, local_bbox, m, el)
        body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' + dml.xfrm(x0, y0, W, H) +
                geo + fx + lx + '</p:spPr></p:sp>')
        return [_look(dml.Leaf(box, 'shape', label, '<p:sp><p:nvSpPr>', body), fx, lx, ('paths', paths))]

    def _radial(self, p):
        return p is not None and p[0] == 'grad' and self.local(p[1]) == 'radialGradient'

    def _gradient_space(self, attrs, local_bbox, m):
        gt = g.parse_transform(attrs.get('gradientTransform'))
        if attrs.get('gradientUnits') == 'userSpaceOnUse':
            return g.mul(m, gt)
        if local_bbox is None or local_bbox[2] - local_bbox[0] <= 0 or local_bbox[3] - local_bbox[1] <= 0:
            return None
        lx, ly = local_bbox[0], local_bbox[1]
        return g.mul(m, g.mul((local_bbox[2] - lx, 0.0, 0.0, local_bbox[3] - ly, lx, ly), gt))

    def _gradient_val(self, attrs):
        if attrs.get('gradientUnits') == 'userSpaceOnUse':
            return lambda name, axis, dflt: self.vp_len(attrs.get(name, dflt), axis)
        return lambda name, axis, dflt: css.number(attrs.get(name, dflt), 0.0)

    def _fill_rule_paths(self, ssubs, st, filled, stroked):
        """DrawingML fills the subpaths of one path even-odd.  For nonzero
        fills, move subpaths that even-odd would wrongly hollow out into
        their own path (paths of one shape are filled independently)."""
        if (not filled or len(ssubs) < 2 or len(ssubs) > 400
                or st.get('fill-rule', 'nonzero') == 'evenodd'):
            return [(ssubs, filled, stroked)]
        polys = [pts for pts, _ in g.flatten(ssubs, steps=4)]
        boxes = [(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                  max(p[1] for p in pts)) for pts in polys]
        dirs = [1 if g.signed_area(pts) >= 0 else -1 for pts in polys]
        extra = []
        for i, pts in enumerate(polys):
            px, py = pts[0]
            wind = 0
            depth = 0
            for j, q in enumerate(polys):
                if j == i or not (boxes[j][0] <= px <= boxes[j][2] and boxes[j][1] <= py <= boxes[j][3]):
                    continue
                if g.point_in_polygon(px, py, q):
                    depth += 1
                    wind += dirs[j]
            nonzero_filled = (wind + dirs[i]) != 0
            evenodd_filled = (depth % 2) == 0
            if nonzero_filled != evenodd_filled and depth > 0:
                extra.append(i)
        if not extra:
            return [(ssubs, filled, stroked)]
        main = [s for i, s in enumerate(ssubs) if i not in extra]
        out = [(main, filled, stroked)] if main else []
        out.extend(([ssubs[i]], filled, stroked) for i in extra)
        return out

    # --- markers -----------------------------------------------------------

    def markers(self, el, st, m, subs, sw, clip, a):
        refs = []
        for which in ('marker-start', 'marker-mid', 'marker-end'):
            v = st.get(which, 'none')
            mt = _URL.match(v) if v and v != 'none' else None
            mk = self.ids.get(mt.group(1)) if mt else None
            refs.append(mk if mk is not None and self.local(mk) == 'marker' else None)
        if all(r is None for r in refs):
            return []
        verts = _vertices(subs)
        if not verts:
            return []
        places = []
        if refs[0] is not None:
            x, y, ain, aout = verts[0]
            places.append((refs[0], x, y, aout if aout is not None else ain, True))
        if refs[1] is not None:
            for x, y, ain, aout in verts[1:-1]:
                places.append((refs[1], x, y, _bisect(ain, aout), False))
        if refs[2] is not None:
            x, y, ain, aout = verts[-1]
            places.append((refs[2], x, y, ain if ain is not None else aout, False))
        by_marker = {}
        for mk, x, y, ang, is_start in places:
            orient = mk.get('orient', '0')
            if orient == 'auto':
                deg = math.degrees(ang or 0.0)
            elif orient == 'auto-start-reverse':
                deg = math.degrees(ang or 0.0) + (180.0 if is_start else 0.0)
            else:
                deg = css.number(re.sub('deg$', '', orient), 0.0)
            mm = g.mul(m, g.mul(g.translate(x, y), g.rotate(deg)))
            if mk.get('markerUnits', 'strokeWidth') == 'strokeWidth':
                mm = g.mul(mm, g.scale(sw))
            mw = css.length(mk.get('markerWidth', '3'), default=3.0)
            mh = css.length(mk.get('markerHeight', '3'), default=3.0)
            vb = _parse_viewbox(mk.get('viewBox'))
            vbt = viewbox_transform(vb, mw, mh, mk.get('preserveAspectRatio')) if vb else g.IDENTITY
            rx, ry = g.apply(vbt, css.length(mk.get('refX', '0')), css.length(mk.get('refY', '0')))
            mm = g.mul(mm, g.mul(g.translate(-rx, -ry), vbt))
            by_marker.setdefault(mk, []).append(mm)
        out = []
        for mk, mats in by_marker.items():
            mst = self.doc_style(mk)
            kids = [c for c in mk if isinstance(c.tag, str) and self.local(c)]
            if (self.series_limit is not None and len(mats) > self.series_limit and len(kids) == 1
                    and self.local(kids[0]) in _SHAPES):
                kst = css.cascade(mst, self.spec(kids[0]))
                km = [g.mul(mm, g.parse_transform(kids[0].get('transform'))) for mm in mats]
                out.extend(self.merged(kids[0], kst, km, clip, a, mk.get('id')))
                continue
            for mm in mats:
                out.append(dml.Group(None, self.children(mk, mm, mst, clip, a)))
        return out

    # --- repeated copies (<use> series, many markers) ----------------------

    def use_series(self, uses, target, m, st, clip, a):
        groups = {}
        order = []
        for u in uses:
            ust = css.cascade(st, self.spec(u))
            if ust.get('display') == 'none':
                continue
            ua = a * max(0.0, min(1.0, css.number(ust.get('opacity', '1'), 1.0)))
            um = m
            tr = u.get('transform')
            if tr:
                um = g.mul(um, g.parse_transform(tr))
            um = g.mul(um, g.translate(self.vp_len(u.get('x'), 'x', ust), self.vp_len(u.get('y'), 'y', ust)))
            tst = css.cascade(ust, self.spec(target))
            tr = target.get('transform')
            if tr:
                um = g.mul(um, g.parse_transform(tr))
            key = (tuple(sorted(tst.items())), ua)
            if key not in groups:
                groups[key] = (tst, ua, [], u)
                order.append(key)
            groups[key][2].append(um)
        out = []
        for key in order:
            tst, ua, mats, _first = groups[key]
            if len(mats) > self.series_limit:
                out.extend(self.merged(target, tst, mats, clip, ua, None))
            else:
                for um in mats:
                    out.extend(self.shape(target, tst, um, clip, ua))
        return out

    def merged(self, el, st, mats, clip, a, label):
        """One shape holding every copy of el (one DrawingML path per copy)."""
        if st.get('visibility', 'visible') in ('hidden', 'collapse') or st.get('display') == 'none':
            return []
        subs, _ = self.local_path(el, st)
        if not subs:
            return []
        fill = None if self.local(el) == 'line' else self.paint(st, 'fill', el, a)
        stroke = self.paint(st, 'stroke', el, a)
        sw = self.stroke_width(st)
        if sw <= 0:
            stroke = None
        if fill is None and stroke is None:
            return []
        sw_emu = sw * g.mean_scale(mats[0])
        margin = sw_emu / 2 if stroke is not None else 0.0
        copies = []
        bb = None
        for mm in mats:
            ssubs = g.transform_path(subs, mm)
            b = g.path_bbox(ssubs)
            if b is None:
                continue
            if clip is not None:
                gb = _grow(b, margin)
                if _disjoint(gb, clip.rect):
                    continue
                if not _inside(gb, clip.rect):
                    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                    r = clip.rect
                    if not (r[0] <= cx <= r[2] and r[1] <= cy <= r[3]):
                        continue
            copies.append(ssubs)
            bb = g.bbox_union(bb, b)
        if not copies:
            return []
        self.absorbed += len(copies) - 1
        x0, y0 = bb[0], bb[1]
        W, H = max(bb[2] - x0, 1.0), max(bb[3] - y0, 1.0)
        box = (x0, y0, x0 + W, y0 + H)
        shift = (1, 0, 0, 1, -x0, -y0)
        geo = dml.cust_geom([(g.transform_path(s, shift), W, H, fill is not None, stroke is not None)
                             for s in copies], self.grid)
        local_bbox = g.path_bbox(subs)
        fx = self.fill_xml(fill, box, local_bbox, mats[0], el)
        lx = self.line_xml(stroke, sw_emu, sw, st, box, local_bbox, mats[0], el)
        body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' + dml.xfrm(x0, y0, W, H) +
                geo + fx + lx + '</p:spPr></p:sp>')
        return [dml.Leaf(box, 'shape', label, '<p:sp><p:nvSpPr>', body)]

    # --- glyph labels (text drawn as outlines) --------------------------------

    def glyph_uses(self, el):
        """(uses, paths) of a group that draws one label from glyph outlines,
        or None.  A label group has only <use> children that refer to glyph
        outlines: cairo's glyph-F-N definitions, or, in a Matplotlib text
        group (an XML comment before it), Matplotlib's glyph paths; a
        Matplotlib mathtext group may also hold plain paths (fraction bars)."""
        mpl = el in self.comment_before and bool(el.get('transform'))
        uses, paths = [], []
        for c in el:
            if not isinstance(c.tag, str):
                continue
            tag = self.local(c)
            if not tag or tag in _NOT_RENDERED:
                continue
            if tag == 'use':
                t = self.ids.get(self.href(c) or '')
                if t is None:
                    return None
                tid, ttag = t.get('id', ''), self.local(t)
                if not ((_CAIRO_GLYPH.match(tid) and ttag in ('g', 'symbol', 'path')) or
                        (mpl and ttag == 'path' and _MPL_GLYPH.match(tid))):
                    return None
                uses.append(c)
            elif tag == 'path' and mpl:
                paths.append(c)
            else:
                return None
        return (uses, paths) if uses else None

    def _glyph_parts(self, t, m, st, items, depth=0):
        """Collect (shape, style, matrix) of a glyph definition; False when it
        holds anything but plain shapes."""
        tst = css.cascade(st, self.spec(t))
        if tst.get('display') == 'none':
            return True
        if css.number(tst.get('opacity', '1'), 1.0) != 1.0 or depth > MAX_USE_DEPTH:
            return False
        if t.get('transform'):
            m = g.mul(m, g.parse_transform(t.get('transform')))
        tag = self.local(t)
        if tag in _SHAPES:
            items.append((t, tst, m))
            return True
        if tag not in ('g', 'symbol') or (tag == 'symbol' and t.get('viewBox')):
            return False
        for c in t:
            if isinstance(c.tag, str) and self.local(c) and self.local(c) not in _NOT_RENDERED:
                if not self._glyph_parts(c, m, tst, items, depth + 1):
                    return False
        return True

    def glyph_shape(self, el, glyphs, m, st, clip, a):
        """One shape holding every glyph outline of a label group (one
        DrawingML path per glyph), or None when the glyphs do not share one
        fill or are stroked."""
        uses, paths = glyphs
        items = []
        for u in uses:
            ust = css.cascade(st, self.spec(u))
            if ust.get('display') == 'none':
                continue
            if css.number(ust.get('opacity', '1'), 1.0) != 1.0:
                return None
            um = g.mul(m, g.parse_transform(u.get('transform')))
            um = g.mul(um, g.translate(self.vp_len(u.get('x'), 'x', ust), self.vp_len(u.get('y'), 'y', ust)))
            if not self._glyph_parts(self.ids[self.href(u)], um, ust, items):
                return None
        for p in paths:
            if not self._glyph_parts(p, m, st, items):
                return None
        fill = None
        shapes = []
        for el2, sst, mm in items:
            if sst.get('visibility', 'visible') in ('hidden', 'collapse'):
                continue
            f = self.paint(sst, 'fill', el2, a)
            if f is None or (fill is not None and f != fill):
                return None
            if self.paint(sst, 'stroke', el2, a) is not None and self.stroke_width(sst) > 0:
                return None
            fill = f
            shapes.append((el2, sst, mm))
        if not shapes:
            return []
        paths_out = []
        bb = None
        local_bb = None
        for el2, sst, mm in shapes:
            subs, _ = self.local_path(el2, sst)
            ssubs = g.transform_path(subs, mm)
            b = g.path_bbox(ssubs)
            if b is None:
                continue
            if clip is not None and not _inside(b, clip.rect):
                if _disjoint(b, clip.rect):
                    continue
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                r = clip.rect
                if not (r[0] <= cx <= r[2] and r[1] <= cy <= r[3]):
                    continue
            paths_out.extend(self._fill_rule_paths(ssubs, sst, True, False))
            bb = g.bbox_union(bb, b)
            local_bb = g.bbox_union(local_bb, g.path_bbox(subs))
        if not paths_out:
            return []
        x0, y0 = bb[0], bb[1]
        W, H = max(bb[2] - x0, 1.0), max(bb[3] - y0, 1.0)
        box = (x0, y0, x0 + W, y0 + H)
        shift = (1, 0, 0, 1, -x0, -y0)
        geo = dml.cust_geom([(g.transform_path(s, shift), W, H, f, k) for s, f, k in paths_out], self.grid)
        fx = self.fill_xml(fill, box, local_bb, m, el)
        body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' + dml.xfrm(x0, y0, W, H) +
                geo + fx + dml.NO_LINE + '</p:spPr></p:sp>')
        return [dml.Leaf(box, 'shape', el.get('id'), '<p:sp><p:nvSpPr>', body)]

    def mpl_text(self, el, glyphs, m, st, clip, a):
        """A real text box for a Matplotlib outlined text (svg.fonttype
        'path'), or None when the group does not match the string.

        Matplotlib (backend_svg.RendererSVG._draw_text_as_path) writes the
        string as a comment, then <g transform="translate(x y) rotate(-angle)
        scale(f -f)"> with f = font size in points / 100 (TextToPath.FONT_SCALE),
        holding one <use> per glyph laid out from the baseline's left end."""
        uses, paths = glyphs
        s = self.comment_before[el]
        s = s[1:] if s.startswith(' ') else s
        s = s[:-1] if s.endswith(' ') else s
        s = re.sub(r'- (?=-)', '-', s).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        dollars = s.count('$') - s.count('\\$')
        if paths or (dollars > 0 and dollars % 2 == 0):
            return None                      # mathtext: drawn as outlines
        # Matplotlib writes a (blank) glyph for each space too; allow either
        if len(uses) not in (len(s), sum(1 for c in s if not c.isspace())):
            return None
        if g.det(g.parse_transform(el.get('transform'))) >= 0:
            return None
        font = None
        for u in uses:
            sp = self.spec(u)
            if u.get('x') or u.get('y') or any(k in sp for k in (
                    'fill', 'fill-opacity', 'opacity', 'stroke', 'display', 'visibility',
                    'clip-path', 'mask', 'filter')):
                return None
            ut = g.parse_transform(u.get('transform'))
            if (abs(ut[1]) > 1e-9 or abs(ut[2]) > 1e-9 or abs(ut[0] - 1) > 1e-6
                    or abs(ut[3] - 1) > 1e-6 or abs(ut[5]) > 1e-6):
                return None
            if font is None:
                font = _MPL_GLYPH.match(self.ids[self.href(u)].get('id')).group(1)
        family, bold, italic = _ps_font(font)
        rst = {k: v for k, v in st.items() if k not in (
            'letter-spacing', 'baseline-shift', 'dominant-baseline', 'alignment-baseline', 'text-decoration')}
        rst.update({'font-family': family, 'font-size': '100', 'font-weight': 'bold' if bold else 'normal',
                    'font-style': 'italic' if italic else 'normal', 'text-anchor': 'start'})
        # glyph space: y up, 100 units = the font size; flip y for the text box
        mm = g.mul(m, g.scale(1.0, -1.0))
        leaf = self._text_box({'x': 0.0, 'y': 0.0, 'st': rst, 'runs': [(s, rst, 0.0)],
                               'w': txt.text_width(s, 100.0, bold), 'anchor': 'start'},
                              mm, clip, a, el.get('id'))
        return [leaf] if leaf is not None else []

    # --- images ------------------------------------------------------------

    def image(self, el, st, m, clip, a):
        if st.get('visibility', 'visible') in ('hidden', 'collapse'):
            return []
        href = el.get(XLINK_HREF) or el.get('href') or ''
        dec = img.decode_data_uri(href.strip())
        if dec is None:
            self.skip('external or unreadable image', el)
            return []
        mime, raw = dec
        if mime not in img.EXT:
            self.skip('image type %s' % mime, el)
            return []
        size = img.intrinsic_size(mime, raw)
        x, y = self.vp_len(el.get('x'), 'x', st), self.vp_len(el.get('y'), 'y', st)
        wv, hv = el.get('width'), el.get('height')
        iw, ih = size if size else (0, 0)
        w = self.vp_len(wv, 'x', st) if wv not in (None, 'auto') else float(iw)
        h = self.vp_len(hv, 'y', st) if hv not in (None, 'auto') else float(ih)
        if (wv in (None, 'auto')) != (hv in (None, 'auto')) and iw and ih:
            if wv in (None, 'auto'):
                w = h * iw / ih
            else:
                h = w * ih / iw
        if w <= 0 or h <= 0:
            return []
        # preserveAspectRatio: displayed rectangle and crop fractions (l, t, r, b)
        crop = [0.0, 0.0, 0.0, 0.0]
        par = (el.get('preserveAspectRatio') or 'xMidYMid meet').split()
        if iw and ih and par[0] != 'none':
            s_meet = min(w / iw, h / ih)
            s_slice = max(w / iw, h / ih)
            slice_ = len(par) > 1 and par[1] == 'slice'
            s = s_slice if slice_ else s_meet
            dw, dh = iw * s, ih * s
            fx = 0.0 if 'xMin' in par[0] else 1.0 if 'xMax' in par[0] else 0.5
            fy = 0.0 if 'YMin' in par[0] else 1.0 if 'YMax' in par[0] else 0.5
            if slice_:
                ex, ey = (dw - w) / dw, (dh - h) / dh
                crop = [ex * fx, ey * fy, ex * (1 - fx), ey * (1 - fy)]
            else:
                x += (w - dw) * fx
                y += (h - dh) * fy
                w, h = dw, dh
        dec = g.decompose(m)
        if dec is None:
            self.skip('skewed image (drawn unskewed)', el)
            sx_ = math.hypot(m[0], m[1])
            sy_ = math.hypot(m[2], m[3])
            dec = (math.atan2(m[1], m[0]), sx_, sy_, g.det(m) < 0)
        theta, sx, sy, flip = dec
        flip_v_attr = False
        if flip:
            flipped = img.flip_png(raw, False, True) if mime == 'image/png' else None
            if flipped is not None:
                raw = flipped
            else:
                flip_v_attr = True
            crop[1], crop[3] = crop[3], crop[1]
        W, H = w * sx, h * sy
        C = g.apply(m, x + w / 2, y + h / 2)
        box = [C[0] - W / 2, C[1] - H / 2, C[0] + W / 2, C[1] + H / 2]
        rotated = abs(math.sin(theta)) > 1e-9 or math.cos(theta) < 0
        vis = g.rect_corners_bbox(m, x, y, w, h)
        if clip is not None and not _inside(vis, clip.rect):
            if _disjoint(vis, clip.rect):
                return []
            if rotated:
                self.skip('clipped rotated image (not clipped)', el)
            else:
                r = _intersect(tuple(box), clip.rect)
                if r[0] >= r[2] or r[1] >= r[3]:
                    return []
                sxw = (1 - crop[0] - crop[2]) / W
                syh = (1 - crop[1] - crop[3]) / H
                top_cut = (r[1] - box[1]) * syh
                bottom_cut = (box[3] - r[3]) * syh
                if flip_v_attr:
                    top_cut, bottom_cut = bottom_cut, top_cut
                crop = [crop[0] + (r[0] - box[0]) * sxw, crop[1] + top_cut,
                        crop[2] + (box[2] - r[2]) * sxw, crop[3] + bottom_cut]
                box = list(r)
                W, H = r[2] - r[0], r[3] - r[1]
                vis = r
        rid = self.add_media(mime, raw)
        blip = '<a:blip r:embed="%s"' % rid
        if a < 0.99999:
            blip += '><a:alphaModFix amt="%d"/></a:blip>' % dml.alpha_val(a)
        else:
            blip += '/>'
        src = ''
        if any(c > 1e-7 for c in crop):
            src = '<a:srcRect%s/>' % ''.join(
                ' %s="%d"' % (k, round(v * 100000)) for k, v in zip('ltrb', crop) if v > 1e-7)
        body = ('<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
                '<p:blipFill>' + blip + src + '<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
                '<p:spPr>' + dml.xfrm(box[0], box[1], W, H, theta if rotated else 0.0, False, flip_v_attr) +
                dml.prst_geom('rect') + '</p:spPr></p:pic>')
        return [dml.Leaf(vis, 'picture', el.get('id'), '<p:pic><p:nvPicPr>', body)]

    def add_media(self, mime, raw):
        digest = hashlib.sha1(raw).hexdigest()
        rid = self._media_ids.get(digest)
        if rid is None:
            n = len(self.media) + 1
            rid = 'rId%d' % (n + 1)
            self.media.append(('image%d.%s' % (n, img.EXT[mime]), raw))
            self._media_ids[digest] = rid
        return rid

    # --- text --------------------------------------------------------------

    def text(self, el, st, m, clip, a):
        on_path = [c for c in el if isinstance(c.tag, str) and self.local(c) == 'textPath']
        out = self.text_lines(el, st, m, clip, a, not on_path)
        for c in on_path:
            out.extend(self.text_on_path(c, st, m, clip, a))
        if len(out) > 1 or (on_path and out):
            return [dml.Group(el.get('id'), out)]
        return out

    def text_lines(self, el, st, m, clip, a, own_label):
        runs = []
        self._collect(el, st, runs, True)
        if not runs:
            return []
        preserve = any(r['st'].get('xml:space') == 'preserve' or
                       r['st'].get('white-space') in ('pre', 'pre-wrap', 'break-spaces') for r in runs)
        _normalise_space(runs, preserve)
        # position runs into chunks
        chunks = []
        cx = cy = 0.0
        force_new = False
        svg_chunk = 0      # number of the SVG text chunk (started by an absolute x or y)
        for r in runs:
            if r['x'] is not None or r['y'] is not None:
                svg_chunk += 1
            if not r['text']:
                if r['x'] is not None or r['y'] is not None:
                    cx = r['x'] if r['x'] is not None else cx
                    cy = r['y'] if r['y'] is not None else cy
                    force_new = True
                cx += r['dx']
                cy += r['dy']
                continue
            rst = r['st']
            fs = css.number(rst.get('font-size', '16'), 16.0)
            split = False
            if r.get('pc') and chunks and not force_new:
                # a character with its own position: a new box where it jumps
                # more than half an em from where the previous character's
                # estimated advance puts it, or moves vertically; smaller
                # adjustments (kerning) are ignored
                nx = (r['x'] if r['x'] is not None else cx) + r['dx']
                ny = (r['y'] if r['y'] is not None else cy) + r['dy']
                new = split = (abs(nx - cx) > 0.5 * fs or abs(ny - cy) > 1e-6 * fs or
                               (r['x'] is not None and rst.get('text-anchor', 'start') != 'start'))
                if new:
                    cx, cy = nx, ny
            else:
                new = r['x'] is not None or r['y'] is not None or not chunks or force_new
                if r['x'] is not None:
                    cx = r['x']
                if r['y'] is not None:
                    cy = r['y']
                cx += r['dx']
                cy += r['dy']
                if r['dx'] > fs:
                    new = True
            force_new = False
            shift = _baseline_shift(rst.get('baseline-shift'), fs)
            if new:
                chunks.append({'x': cx, 'y': cy, 'st': rst, 'runs': [], 'split': split, 'svg': svg_chunk})
            ch = chunks[-1]
            dy = cy - ch['y'] - shift
            if ch['runs'] and ch['runs'][-1][1] is rst and ch['runs'][-1][2] == dy:
                # same style and shift (characters placed one by one): one run
                ch['runs'][-1] = (ch['runs'][-1][0] + r['text'], rst, dy)
            else:
                ch['runs'].append((r['text'], rst, dy))
            cx += txt.text_width(r['text'], fs, _bold(rst))
        # merge chunks that continue the same line
        lines = []
        for ch in chunks:
            if lines and not ch['split']:
                ln = lines[-1]
                fs = css.number(ln['st'].get('font-size', '16'), 16.0)
                end = ln['x'] + ln['w']
                if (abs(ch['y'] - ln['y']) <= 0.6 * fs and ln['anchor'] == 'start'
                        and end - 0.5 * fs <= ch['x'] <= end + 1.0 * fs):
                    for t, rst, dy in ch['runs']:
                        ln['runs'].append((t, rst, dy + ch['y'] - ln['y']))
                    ln['w'] = ch['x'] - ln['x'] + _width(ch['runs'])
                    continue
            anchor = ch['st'].get('text-anchor', 'start')
            if ch['st'].get('direction') == 'rtl':
                anchor = {'start': 'end', 'end': 'start'}.get(anchor, anchor)
            lines.append({'x': ch['x'], 'y': ch['y'], 'st': ch['st'], 'runs': list(ch['runs']),
                          'w': _width(ch['runs']), 'anchor': anchor, 'svg': ch['svg']})
        # boxes split by relative offsets (dx/dy) inside one SVG text chunk
        # share its anchor: lay them out from the chunk's start
        k = 0
        while k < len(lines):
            j = k + 1
            while j < len(lines) and lines[j]['svg'] == lines[k]['svg']:
                j += 1
            anchor = lines[k]['anchor']
            if j - k > 1 and anchor in ('middle', 'end'):
                ext = max(ln['x'] + ln['w'] for ln in lines[k:j]) - lines[k]['x']
                for ln in lines[k:j]:
                    ln['x'] -= ext / 2 if anchor == 'middle' else ext
                    ln['anchor'] = 'start'
            k = j
        tl = el.get('textLength')
        if tl and len(lines) == 1:
            v = self.vp_len(tl, 'x', st)
            if v > 0:
                lines[0]['w'] = v
        out = []
        label = el.get('id') if len(lines) == 1 and own_label else None
        for ln in lines:
            leaf = self._text_box(ln, m, clip, a, label)
            if leaf is not None:
                out.append(leaf)
        return out

    def text_on_path(self, tp, st, m, clip, a):
        """One text box per character of a <textPath>, centred on the path at
        its arc-length position and turned to the path's direction there."""
        tst = css.cascade(st, self.spec(tp))
        if tst.get('display') == 'none':
            return []
        d = tp.get('path')
        if d:
            subs = g.parse_path(d)
        else:
            target = self.ids.get(self.href(tp) or '')
            if target is None or self.local(target) not in _SHAPES:
                self.skip('textPath without a path (unresolvable href)', tp)
                return []
            subs, _ = self.local_path(target, self.doc_style(target))
            subs = g.transform_path(subs, g.parse_transform(target.get('transform')))
        runs = []
        self._collect(tp, tst, runs, True)
        if not runs:
            return []
        preserve = any(r['st'].get('xml:space') == 'preserve' or
                       r['st'].get('white-space') in ('pre', 'pre-wrap', 'break-spaces') for r in runs)
        _normalise_space(runs, preserve)
        fs0 = css.number(tst.get('font-size', '16'), 16.0)
        polys = g.flatten_tol(subs, 0.001 * fs0)
        if tp.get('side') == 'right':
            polys = [list(reversed(p)) for p in reversed(polys)]
        # chords (x0, y0, x1, y1, length, angle, subpath number) and their start arc lengths
        segs, starts, total = [], [], 0.0
        for n, pts in enumerate(polys):
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                ln = math.hypot(x1 - x0, y1 - y0)
                if ln > 0:
                    segs.append((x0, y0, x1, y1, ln, math.atan2(y1 - y0, x1 - x0), n))
                    starts.append(total)
                    total += ln
        if not segs:
            return []
        for attr, bad in (('method', 'stretch'), ('spacing', 'auto'),
                          ('lengthAdjust', 'spacingAndGlyphs')):
            if tp.get(attr) == bad:
                self.skip('textPath %s="%s"' % (attr, bad), tp)
        # characters: (arc position of the glyph middle, char, style, perpendicular shift, width)
        items = []
        pos = perp = 0.0
        for r in runs:
            rst = r['st']
            fs = css.number(rst.get('font-size', '16'), 16.0)
            bold = _bold(rst)
            ls_v = rst.get('letter-spacing')
            ls = self.vp_len(ls_v, 'x', rst) if ls_v and ls_v != 'normal' else 0.0
            pos += r['dx']
            perp += r['dy']
            shift = _baseline_shift(rst.get('baseline-shift'), fs)
            rst = {k: v for k, v in rst.items() if k not in ('letter-spacing', 'baseline-shift')}
            for ch in _clusters(r['text']):
                w = txt.text_width(''.join(c for c in ch if not _is_mark(c)), fs, bold)
                items.append((pos + w / 2, ch, rst, perp - shift, w))
                pos += w + ls
        if not items:
            return []
        length = pos
        tl = tp.get('textLength')
        if tl:
            want = self.vp_len(tl, 'x', tst)
            if want > 0 and length > 0:
                if len(items) > 1:
                    extra = (want - length) / (len(items) - 1)
                    items = [(it[0] + k * extra,) + it[1:] for k, it in enumerate(items)]
                length = want
        anchor = tst.get('text-anchor', 'start')
        if tst.get('direction') == 'rtl':
            anchor = {'start': 'end', 'end': 'start'}.get(anchor, anchor)
        off = tp.get('startOffset')
        s0 = css.length(off, ref=total, font_size=fs0) if off else 0.0
        s0 -= {'middle': length / 2, 'end': length}.get(anchor, 0.0)
        out = []
        before = beyond = False
        for mid, ch, rst, dy, w in items:
            s = s0 + mid
            if s < 0:
                before = True
                continue
            if s > total:
                beyond = True
                continue
            if ch.isspace():
                continue
            k = max(0, bisect.bisect_right(starts, s) - 1)
            x0, y0, x1, y1, ln, ang, sub = segs[k]
            t = min(1.0, (s - starts[k]) / ln)
            px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            # tangent: the chord's direction at its middle, turning towards
            # the neighbouring chord's direction at either end
            j = k - 1 if t < 0.5 else k + 1
            if 0 <= j < len(segs) and segs[j][6] == sub:
                turn = (segs[j][5] - ang + math.pi) % (2 * math.pi) - math.pi
                ang += turn * abs(t - 0.5)
            ang = math.degrees(ang)
            mm = g.mul(m, g.mul(g.translate(px, py), g.rotate(ang)))
            leaf = self._text_box({'x': 0.0, 'y': dy, 'st': rst, 'runs': [(ch, rst, 0.0)],
                                   'w': w, 'anchor': 'middle'}, mm, clip, a, None)
            if leaf is not None:
                out.append(leaf)
        if before:
            self.skip('textPath text before path start', tp)
        if beyond:
            self.skip('textPath text beyond path end', tp)
        return out

    def _collect(self, node, st, runs, is_text, pos=None):
        if pos is None:
            pos = {'i': 0, 'x': {}, 'y': {}, 'dx': {}, 'dy': {}}
        # x, y, dx and dy are lists: value k belongs to the k-th character
        # from the element's start, even when that character sits in a
        # descendant; lists on descendants override for their characters
        i0 = pos['i']
        for k in ('x', 'y', 'dx', 'dy'):
            for j, v in enumerate(g.parse_numbers(node.get(k))):
                pos[k][i0 + j] = v
        if node.get('rotate') and any(v != 0 for v in g.parse_numbers(node.get('rotate'))):
            self.skip('per-glyph text rotate', node)

        def emit(text, rst):
            if not text:
                return
            i = pos['i']
            n = len(text)
            pos['i'] += n
            vals = {k: [pos[k].get(i + j) for j in range(n)] for k in ('x', 'y', 'dx', 'dy')}
            if n > 1 and any(v is not None for k in vals for v in vals[k][1:]):
                # positions for later characters: one run per character
                for j, ch in enumerate(text):
                    runs.append({'text': ch, 'st': rst, 'x': vals['x'][j], 'y': vals['y'][j],
                                 'dx': vals['dx'][j] or 0.0, 'dy': vals['dy'][j] or 0.0, 'pc': True})
                return
            runs.append({'text': text, 'st': rst, 'x': vals['x'][0], 'y': vals['y'][0],
                         'dx': vals['dx'][0] or 0.0, 'dy': vals['dy'][0] or 0.0})

        hidden = st.get('visibility', 'visible') in ('hidden', 'collapse')
        emit(node.text, st if not hidden else None)
        for c in node:
            if not isinstance(c.tag, str):
                if c.tail:
                    emit(c.tail, st)
                continue
            tag = self.local(c)
            cst = css.cascade(st, self.spec(c))
            if tag in ('tspan', 'a') and cst.get('display') != 'none':
                self._collect(c, cst, runs, False, pos)
            elif tag == 'textPath' and not is_text:
                # textPath directly in <text> is drawn by text_on_path
                self.skip('textPath (text along a path)', c)
            if c.tail:
                emit(c.tail, st)
        if not is_text:
            return
        # drop hidden runs
        runs[:] = [r for r in runs if r['st'] is not None and
                   r['st'].get('visibility', 'visible') not in ('hidden', 'collapse')]

    def _text_box(self, ln, m, clip, a, label):
        lst = ln['st']
        fs = max(css.number(r[1].get('font-size', '16'), 16.0) for r in ln['runs'])
        base_fs = css.number(lst.get('font-size', '16'), 16.0)
        # The box is wider than the estimated text so that renderers which
        # wrap text inside grouped boxes despite wrap="none" (LibreOffice)
        # keep it on one line; alignment keeps the anchor exact.
        W = max(ln['w'], 0.1 * fs) * 1.25 + 0.5 * fs
        anchor = ln['anchor']
        ax, ay = ln['x'], ln['y']
        db = lst.get('dominant-baseline') or lst.get('alignment-baseline')
        ay += {'middle': 0.35, 'central': 0.35, 'hanging': 0.75, 'text-before-edge': 0.9,
               'text-top': 0.9, 'mathematical': 0.3, 'text-after-edge': -0.2,
               'text-bottom': -0.2, 'ideographic': -0.2}.get(db, 0.0) * base_fs
        left = ax - (W / 2 if anchor == 'middle' else W if anchor == 'end' else 0.0)
        top = ay - txt.ASCENT * fs
        H = txt.LINE_HEIGHT * fs
        dec = g.decompose(m)
        if dec is None:
            sx_ = math.hypot(m[0], m[1])
            sy_ = math.hypot(m[2], m[3])
            dec = (math.atan2(m[1], m[0]), sx_, sy_, False)
        theta, sx, sy, _ = dec
        scale = g.mean_scale(m)
        Ws, Hs = W * sx, H * sy
        C = g.apply(m, left + W / 2, top + H / 2)
        box = (C[0] - Ws / 2, C[1] - Hs / 2, C[0] + Ws / 2, C[1] + Hs / 2)
        vis = g.rect_corners_bbox(m, left, top, W, H)
        if clip is not None and _disjoint(vis, clip.rect):
            return None
        parts = []
        for t, rst, dy in ln['runs']:
            parts.append(self._run_xml(t, rst, dy, scale, a))
        algn = {'middle': 'ctr', 'end': 'r'}.get(anchor, 'l')
        rotated = abs(math.sin(theta)) > 1e-9 or math.cos(theta) < 0
        body = ('<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>' +
                dml.xfrm(box[0], box[1], Ws, Hs, theta if rotated else 0.0) +
                dml.prst_geom('rect') + '<a:noFill/>' + dml.NO_LINE + '</p:spPr>'
                '<p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" rtlCol="0" '
                'anchor="t"><a:spAutoFit/></a:bodyPr><a:lstStyle/><a:p><a:pPr algn="%s"/>%s</a:p>'
                '</p:txBody></p:sp>') % (algn, ''.join(parts))
        return dml.Leaf(vis, 'text', label, '<p:sp><p:nvSpPr>', body)

    def _run_xml(self, t, rst, dy, scale, a):
        fs = css.number(rst.get('font-size', '16'), 16.0)
        sz = int(round(fs * scale / 12700 * 100))
        sz = max(100, min(400000, sz))
        attrs = ' lang="en-US" sz="%d"' % sz
        if _bold(rst):
            attrs += ' b="1"'
        if rst.get('font-style', 'normal') in ('italic', 'oblique'):
            attrs += ' i="1"'
        deco = rst.get('text-decoration', '')
        if 'underline' in deco:
            attrs += ' u="sng"'
        if 'line-through' in deco:
            attrs += ' strike="sngStrike"'
        ls = rst.get('letter-spacing')
        if ls and ls != 'normal':
            v = self.vp_len(ls, 'x', rst)
            if v:
                attrs += ' spc="%d"' % int(round(v * scale / 12700 * 100))
        if abs(dy) > 0.05 * fs:
            attrs += ' baseline="%d"' % int(round(max(-100, min(100, -dy / fs * 100)) * 1000))
        attrs += ' dirty="0"'
        fill = self.paint(rst, 'fill', None, a)
        if fill is None:
            fx = '<a:noFill/>'
        elif fill[0] == 'solid':
            fx = dml.solid(fill[1], fill[2])
        else:
            _, _, stops = self.gradient_info(fill[1])
            fx = dml.solid(stops[0][1], stops[0][2] * fill[2]) if stops else '<a:noFill/>'
        ln = ''
        stroke = self.paint(rst, 'stroke', None, a)
        sw = self.stroke_width(rst)
        if stroke is not None and stroke[0] == 'solid' and sw > 0:
            ln = '<a:ln w="%d">%s</a:ln>' % (int(round(sw * scale)), dml.solid(stroke[1], stroke[2]))
        name = txt.first_family(rst.get('font-family'))
        self.found_fonts.setdefault(name, None)
        face = dml.esc(txt.replace(name, self.fonts))
        return ('<a:r><a:rPr%s>%s%s<a:latin typeface="%s"/><a:ea typeface="%s"/><a:cs typeface="%s"/>'
                '</a:rPr><a:t>%s</a:t></a:r>' % (attrs, ln, fx, face, face, face, dml.esc_text(t)))


def _look(leaf, fx, lx, geom):
    """Record what a shape looks like and its geometry, so that identical
    shapes can be merged later.  Gradients are laid out over each shape's own
    box, so gradient shapes never merge."""
    if 'gradFill' not in fx and 'gradFill' not in lx:
        leaf.look = (fx, lx)
        leaf.geom = geom
    return leaf


def _ps_font(name):
    """(family, bold, italic) from a font's PostScript name, e.g.
    DejaVuSans-BoldOblique -> ('DejaVu Sans', True, True)."""
    name = urllib.parse.unquote(name).replace('_', ' ')
    base, _, style = name.partition('-')
    for suffix in ('PSMT', 'MT', 'PS'):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[:-len(suffix)]
        if style.endswith(suffix):
            style = style[:-len(suffix)]
    family = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', base).replace('Deja Vu', 'DejaVu')
    low = style.lower()
    return family, any(w in low for w in ('bold', 'black', 'heavy')), 'italic' in low or 'oblique' in low


def _is_mark(c):
    """A character drawn on its neighbour: combining mark or zero-width joiner."""
    return unicodedata.category(c) in ('Mn', 'Me', 'Cf')


def _clusters(s):
    """Split text into the units placed along a path: a character with its
    combining marks, and consonants joined by a virama (Indic conjuncts)."""
    out = []
    for c in s:
        if out and (_is_mark(c) or unicodedata.category(c) == 'Mc' or
                    unicodedata.combining(out[-1][-1]) == 9 or out[-1][-1] == '\u200d'):
            out[-1] += c
        else:
            out.append(c)
    return out


def _grow(bb, d):
    return (bb[0] - d, bb[1] - d, bb[2] + d, bb[3] + d)


def _bbox_of(subs):
    return g.path_bbox(subs) or (0, 0, 0, 0)


def _bold(st):
    w = st.get('font-weight', 'normal')
    return w in ('bold', 'bolder', '600', '700', '800', '900')


def _width(runs):
    return sum(txt.text_width(t, css.number(rst.get('font-size', '16'), 16.0), _bold(rst))
               for t, rst, _ in runs)


def _baseline_shift(v, fs):
    if not v or v == 'baseline':
        return 0.0
    if v == 'super':
        return 0.4 * fs
    if v == 'sub':
        return -0.2 * fs
    return css.length(v, ref=fs, font_size=fs)


def _normalise_space(runs, preserve):
    if preserve:
        for r in runs:
            r['text'] = r['text'].replace('\r\n', ' ').replace('\n', ' ').replace('\t', ' ')
        return
    prev_space = True
    for r in runs:
        s = r['text'].replace('\r', '').replace('\n', '').replace('\t', ' ')
        s = re.sub(' +', ' ', s)
        if prev_space and s.startswith(' '):
            s = s[1:]
        if s:
            prev_space = s.endswith(' ')
        r['text'] = s
    for r in reversed(runs):
        if r['text']:
            r['text'] = r['text'].rstrip(' ')
            if r['text']:
                break


def _stop_at(stops, o):
    if o <= stops[0][0]:
        return stops[0][1], stops[0][2]
    for (o1, c1, a1), (o2, c2, a2) in zip(stops, stops[1:]):
        if o1 <= o <= o2:
            t = 0.0 if o2 == o1 else (o - o1) / (o2 - o1)
            return _mix(c1, c2, t), a1 + (a2 - a1) * t
    return stops[-1][1], stops[-1][2]


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(c2[i:i + 2], 16) for i in (0, 2, 4)]
    return '%02x%02x%02x' % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _clip_stops(stops):
    """Clip stops (positions may lie outside 0..1) to 0..1, padding the ends."""
    inside = [s for s in stops if 0.0 <= s[0] <= 1.0]
    out = []
    if not inside or inside[0][0] > 0.0:
        c, a = _stop_at(stops, 0.0)
        out.append((0.0, c, a))
    out.extend(inside)
    if not inside or inside[-1][0] < 1.0:
        c, a = _stop_at(stops, 1.0)
        out.append((1.0, c, a))
    return out


def _seg_dirs(p0, s):
    """Start and end tangent angles of a segment starting at p0."""
    x0, y0 = p0
    if s[0] == 'L':
        pts = [(x0, y0), (s[1], s[2])]
    elif s[0] == 'Q':
        pts = [(x0, y0), (s[1], s[2]), (s[3], s[4])]
    else:
        pts = [(x0, y0), (s[1], s[2]), (s[3], s[4]), (s[5], s[6])]
    a_out = a_in = None
    for q in pts[1:]:
        if q != pts[0]:
            a_out = math.atan2(q[1] - pts[0][1], q[0] - pts[0][0])
            break
    for q in reversed(pts[:-1]):
        if q != pts[-1]:
            a_in = math.atan2(pts[-1][1] - q[1], pts[-1][0] - q[0])
            break
    return a_out, a_in


def _vertices(subs):
    """[(x, y, angle_in, angle_out)] for marker placement."""
    verts = []
    for (sx, sy), segs, closed in subs:
        segs = list(segs)
        if closed:
            last = (segs[-1][-2], segs[-1][-1]) if segs else (sx, sy)
            if last != (sx, sy):
                segs.append(('L', sx, sy))
        start = len(verts)
        verts.append([sx, sy, None, None])
        p = (sx, sy)
        for s in segs:
            a_out, a_in = _seg_dirs(p, s)
            if verts[-1][3] is None:
                verts[-1][3] = a_out
            p = (s[-2], s[-1])
            verts.append([p[0], p[1], a_in, None])
        if closed and len(verts) - start > 1:
            # the closing vertex is the start vertex: share both directions
            verts[start][2] = verts[-1][2]
            verts[-1][3] = verts[start][3]
    return [tuple(v) for v in verts]


def _bisect(a_in, a_out):
    if a_in is None:
        return a_out
    if a_out is None:
        return a_in
    d = (a_out - a_in + math.pi) % (2 * math.pi) - math.pi
    return a_in + d / 2
