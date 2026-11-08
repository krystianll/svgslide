"""Write the converted drawing (the slide's shape tree) as an SVG file.

The writer reads the DrawingML that svgslide makes for each object, so the
SVG shows what the slide shows: shapes become <rect>, <ellipse> or <path>
(a merged shape is one <path>), text boxes become <text> with one <tspan>
per run, pictures become <image> with their crop, groups become <g>.
Coordinates are CSS pixels (96 per inch).
"""

import base64
import math
import re
import xml.etree.ElementTree as ET

from . import _drawingml as dml
from . import _geom as g
from . import _text as txt

A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
R_EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
_WRAP = ('<x xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
         'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">%s</x>')
PX = dml.EMU_PER_PX
_MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'gif': 'image/gif'}
_ID = re.compile(r'[^A-Za-z0-9_.-]')
_DEFAULT_NAME = re.compile(r'(Freeform|TextBox|Picture|Group) \d+$')


def _n(v, decimals=3):
    s = '%.*f' % (decimals, v)
    s = s.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def _px(v):
    return _n(v / PX)


def write(nodes, slide_w, slide_h, media, precision=None):
    """SVG bytes for a shape tree; media = [(name, bytes)] as rId2, rId3, ...

    precision: None, or the coordinate grid in points: path coordinates are
    then written with the decimals of the largest power of ten in pixels not
    above that grid (0.01 pt -> 0.01 px, 0.1 pt -> 0.1 px)."""
    w = _Writer(media)
    if precision:
        step = 10.0 ** math.floor(math.log10(precision * 96.0 / 72.0))
        decimals = max(0, -int(round(math.log10(step))))
        w.coord = lambda v: _n(round(v / PX / step) * step, decimals)
    root = ET.Element('svg', {'xmlns': 'http://www.w3.org/2000/svg',
                              'xmlns:xlink': 'http://www.w3.org/1999/xlink',
                              'width': _px(slide_w), 'height': _px(slide_h),
                              'viewBox': '0 0 %s %s' % (_px(slide_w), _px(slide_h))})
    root.append(w.defs)
    for n in nodes:
        root.extend(w.node(n))
    if not len(w.defs):
        root.remove(w.defs)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode') + '\n').encode('utf-8')


class _Writer:
    def __init__(self, media):
        self.media = {'rId%d' % (i + 2): data for i, (_, data) in enumerate(media)}
        self.mime = {'rId%d' % (i + 2): _MIME.get(name.rsplit('.', 1)[1], 'image/png')
                     for i, (name, _) in enumerate(media)}
        self.defs = ET.Element('defs')
        self.ids = set()
        self.n_defs = 0
        self.coord = _px         # formats a path coordinate (EMU) in pixels

    def new_id(self, prefix):
        self.n_defs += 1
        return '%s%d' % (prefix, self.n_defs)

    def label(self, el, label):
        if not label or _DEFAULT_NAME.match(label):
            return
        i = _ID.sub('_', label)
        if not i or not (i[0].isalpha() or i[0] == '_'):
            i = '_' + i
        base, k = i, 1
        while i in self.ids:
            k += 1
            i = '%s_%d' % (base, k)
        self.ids.add(i)
        el.set('id', i)

    def node(self, n):
        if isinstance(n, dml.Group):
            grp = ET.Element('g')
            self.label(grp, n.label)
            for k in n.children:
                grp.extend(self.node(k))
            return [grp] if len(grp) else []
        x = ET.fromstring(_WRAP % (n.head + '<p:cNvPr id="1" name=""/>' + n.body))[0]
        if x.tag == P + 'pic':
            out = self.picture(x)
        elif x.find(P + 'txBody') is not None:
            out = self.text(x)
        else:
            out = self.shape(x)
        if out is not None:
            self.label(out, n.label)
            return [out]
        return []

    # --- placement -----------------------------------------------------

    @staticmethod
    def _xfrm(sppr):
        xf = sppr.find(A + 'xfrm')
        off, ext = xf.find(A + 'off'), xf.find(A + 'ext')
        x, y = int(off.get('x')), int(off.get('y'))
        w, h = int(ext.get('cx')), int(ext.get('cy'))
        return x, y, w, h, int(xf.get('rot', '0')) / 60000.0, xf.get('flipH') == '1', xf.get('flipV') == '1'

    @staticmethod
    def _transform(el, x, y, w, h, rot, fh=False, fv=False):
        if not (rot or fh or fv):
            return
        cx, cy = _px(x + w / 2), _px(y + h / 2)
        t = 'translate(%s %s)' % (cx, cy)
        if rot:
            t += ' rotate(%s)' % _n(rot)
        if fh or fv:
            t += ' scale(%d %d)' % (-1 if fh else 1, -1 if fv else 1)
        el.set('transform', t + ' translate(-%s -%s)' % (cx, cy))

    # --- paint ---------------------------------------------------------

    @staticmethod
    def _color(el):
        c = el.find(A + 'srgbClr')
        a = c.find(A + 'alpha')
        return '#' + c.get('val').lower(), (int(a.get('val')) / 100000.0 if a is not None else 1.0)

    def _paint(self, parent, box):
        """(paint, opacity) of the fill element in parent, or None."""
        for c in parent:
            tag = c.tag[len(A):] if c.tag.startswith(A) else ''
            if tag == 'noFill':
                return None
            if tag == 'solidFill':
                return self._color(c)
            if tag == 'gradFill':
                return 'url(#%s)' % self._gradient(c, box), 1.0
        return None

    def _gradient(self, gf, box):
        x, y, w, h = box
        stops = [(int(s.get('pos')) / 100000.0,) + self._color(s) for s in gf.find(A + 'gsLst')]
        lin = gf.find(A + 'lin')
        gid = self.new_id('gradient')
        if lin is not None:
            # stops run from the box corner first reached along the angle to the last one
            ang = math.radians(int(lin.get('ang', '0')) / 60000.0)
            ux, uy = math.cos(ang), math.sin(ang)
            proj = [cx * ux + cy * uy for cx in (x, x + w) for cy in (y, y + h)]
            c = (x + w / 2) * ux + (y + h / 2) * uy
            mx, my = x + w / 2, y + h / 2
            p0, p1 = min(proj) - c, max(proj) - c
            el = ET.SubElement(self.defs, 'linearGradient', {
                'id': gid, 'gradientUnits': 'userSpaceOnUse',
                'x1': _px(mx + ux * p0), 'y1': _px(my + uy * p0),
                'x2': _px(mx + ux * p1), 'y2': _px(my + uy * p1)})
        else:
            ftr = gf.find(A + 'path/' + A + 'fillToRect')
            fu = int(ftr.get('l', '50000')) / 100000.0 if ftr is not None else 0.5
            fv = int(ftr.get('t', '50000')) / 100000.0 if ftr is not None else 0.5
            r = max(math.hypot(cu - fu, cv - fv) for cu in (0.0, 1.0) for cv in (0.0, 1.0))
            el = ET.SubElement(self.defs, 'radialGradient', {
                'id': gid, 'cx': _n(fu), 'cy': _n(fv), 'r': _n(r)})
        for pos, col, a in stops:
            st = ET.SubElement(el, 'stop', {'offset': _n(pos), 'stop-color': col})
            if a < 1:
                st.set('stop-opacity', _n(a))
        return gid

    def _fill(self, el, parent, box):
        p = self._paint(parent, box)
        if p is None:
            el.set('fill', 'none')
            return
        el.set('fill', p[0])
        if p[1] < 1:
            el.set('fill-opacity', _n(p[1]))

    def _line(self, el, ln, box):
        p = self._paint(ln, box) if ln is not None else None
        if p is None:
            el.set('stroke', 'none')
            return
        w = int(ln.get('w', '12700'))
        el.set('stroke', p[0])
        if p[1] < 1:
            el.set('stroke-opacity', _n(p[1]))
        el.set('stroke-width', _px(w))
        cap = {'rnd': 'round', 'sq': 'square'}.get(ln.get('cap'))
        if cap:
            el.set('stroke-linecap', cap)
        if ln.find(A + 'round') is not None:
            el.set('stroke-linejoin', 'round')
        elif ln.find(A + 'bevel') is not None:
            el.set('stroke-linejoin', 'bevel')
        else:
            mt = ln.find(A + 'miter')
            if mt is not None and mt.get('lim') and int(mt.get('lim')) != 400000:
                el.set('stroke-miterlimit', _n(int(mt.get('lim')) / 100000.0))
        dash = ln.find(A + 'custDash')
        if dash is not None:
            el.set('stroke-dasharray', ' '.join(
                '%s %s' % (_px(int(d.get('d')) * w / 100000.0), _px(int(d.get('sp')) * w / 100000.0))
                for d in dash))

    # --- shapes --------------------------------------------------------

    def shape(self, sp):
        sppr = sp.find(P + 'spPr')
        x, y, w, h, rot, fh, fv = self._xfrm(sppr)
        box = (x, y, w, h)
        prst = sppr.find(A + 'prstGeom')
        if prst is not None:
            kind = prst.get('prst')
            if kind == 'ellipse':
                el = ET.Element('ellipse', {'cx': _px(x + w / 2), 'cy': _px(y + h / 2),
                                            'rx': _px(w / 2), 'ry': _px(h / 2)})
            else:
                el = ET.Element('rect', {'x': _px(x), 'y': _px(y), 'width': _px(w), 'height': _px(h)})
                gd = prst.find(A + 'avLst/' + A + 'gd')
                if kind == 'roundRect':
                    adj = int(gd.get('fmla').split()[1]) if gd is not None else 16667
                    r = adj / 100000.0 * min(w, h)
                    el.set('rx', _px(r))
                    el.set('ry', _px(r))
            self._fill(el, sppr, box)
            self._line(el, sppr.find(A + 'ln'), box)
            self._transform(el, x, y, w, h, rot, fh, fv)
            return el
        paths = []
        for p in sppr.iter(A + 'path'):
            sx = w / max(int(p.get('w', '1')), 1)
            sy = h / max(int(p.get('h', '1')), 1)
            paths.append((_subpaths(p, x, y, sx, sy), p.get('fill') != 'none', p.get('stroke') != '0'))
        if not paths:
            return None
        parts = []
        # paths with the same fill/line use become one <path>
        for subs_list, filled, stroked in _runs(paths):
            el = ET.Element('path', {'d': _path_data(subs_list, self.coord)})
            if len(subs_list) == 1:
                el.set('fill-rule', 'evenodd')
            if filled:
                self._fill(el, sppr, box)
            else:
                el.set('fill', 'none')
            self._line(el, sppr.find(A + 'ln') if stroked else None, box)
            parts.append(el)
        if len(parts) == 1:
            out = parts[0]
        else:
            out = ET.Element('g')
            out.extend(parts)
        self._transform(out, x, y, w, h, rot, fh, fv)
        return out

    # --- text ----------------------------------------------------------

    def text(self, sp):
        sppr = sp.find(P + 'spPr')
        x, y, w, h, rot, fh, fv = self._xfrm(sppr)
        para = sp.find(P + 'txBody/' + A + 'p')
        algn = para.find(A + 'pPr').get('algn', 'l') if para.find(A + 'pPr') is not None else 'l'
        ax = x + (w / 2 if algn == 'ctr' else w if algn == 'r' else 0)
        base = y + h * txt.ASCENT / txt.LINE_HEIGHT
        el = ET.Element('text', {'x': _px(ax), 'y': _px(base),
                                 'xml:space': 'preserve'})
        if algn != 'l':
            el.set('text-anchor', 'middle' if algn == 'ctr' else 'end')
        for r in para.findall(A + 'r'):
            rpr = r.find(A + 'rPr')
            ts = ET.SubElement(el, 'tspan')
            ts.text = r.findtext(A + 't') or ''
            sz = int(rpr.get('sz', '1800')) / 100.0
            ts.set('font-size', _n(sz * 96 / 72))
            latin = rpr.find(A + 'latin')
            if latin is not None:
                ts.set('font-family', latin.get('typeface'))
            if rpr.get('b') == '1':
                ts.set('font-weight', 'bold')
            if rpr.get('i') == '1':
                ts.set('font-style', 'italic')
            deco = ' '.join(d for k, d in (('u', 'underline'), ('strike', 'line-through'))
                            if rpr.get(k) not in (None, 'none', 'noStrike'))
            if deco:
                ts.set('text-decoration', deco)
            if rpr.get('spc'):
                ts.set('letter-spacing', _n(int(rpr.get('spc')) / 100.0 * 96 / 72))
            if rpr.get('baseline') and int(rpr.get('baseline')):
                ts.set('baseline-shift', _n(int(rpr.get('baseline')) / 1000.0) + '%')
            self._fill(ts, rpr, (x, y, w, h))
            ln = rpr.find(A + 'ln')
            if ln is not None:
                self._line(ts, ln, (x, y, w, h))
        self._transform(el, x, y, w, h, rot, fh, fv)
        return el

    # --- pictures ------------------------------------------------------

    def picture(self, pic):
        sppr = pic.find(P + 'spPr')
        x, y, w, h, rot, fh, fv = self._xfrm(sppr)
        blip = pic.find(P + 'blipFill/' + A + 'blip')
        rid = blip.get(R_EMBED)
        data = self.media.get(rid)
        if data is None:
            return None
        crop = {k: 0.0 for k in 'ltrb'}
        src = pic.find(P + 'blipFill/' + A + 'srcRect')
        if src is not None:
            for k in 'ltrb':
                crop[k] = int(src.get(k, '0')) / 100000.0
        fw = w / max(1e-9, 1 - crop['l'] - crop['r'])
        fhh = h / max(1e-9, 1 - crop['t'] - crop['b'])
        el = ET.Element('image', {
            'x': _px(x - crop['l'] * fw), 'y': _px(y - crop['t'] * fhh),
            'width': _px(fw), 'height': _px(fhh), 'preserveAspectRatio': 'none',
            'xlink:href': 'data:%s;base64,%s' % (self.mime[rid], base64.b64encode(data).decode('ascii'))})
        amt = blip.find(A + 'alphaModFix')
        if amt is not None:
            el.set('opacity', _n(int(amt.get('amt')) / 100000.0))
        if any(crop.values()):
            cid = self.new_id('crop')
            cp = ET.SubElement(self.defs, 'clipPath', {'id': cid})
            ET.SubElement(cp, 'rect', {'x': _px(x), 'y': _px(y), 'width': _px(w), 'height': _px(h)})
            el.set('clip-path', 'url(#%s)' % cid)
        self._transform(el, x, y, w, h, rot, fh, fv)
        return el


def _subpaths(p, x0, y0, sx, sy):
    subs = []
    cur = None

    def pt(e):
        return (x0 + int(e.get('x')) * sx, y0 + int(e.get('y')) * sy)
    for c in p:
        tag = c.tag[len(A):]
        pts = [pt(e) for e in c]
        if tag == 'moveTo':
            cur = [pts[0], [], False]
            subs.append(cur)
        elif tag == 'lnTo':
            cur[1].append(('L',) + pts[0])
        elif tag == 'cubicBezTo':
            cur[1].append(('C',) + pts[0] + pts[1] + pts[2])
        elif tag == 'quadBezTo':
            cur[1].append(('Q',) + pts[0] + pts[1])
        elif tag == 'close':
            cur[2] = True
    return subs


def _runs(paths):
    """Consecutive DrawingML paths with the same fill/line use: (list of subpath lists, filled, stroked)."""
    out = []
    for subs, f, k in paths:
        if out and out[-1][1] == f and out[-1][2] == k:
            out[-1][0].append(subs)
        else:
            out.append(([subs], f, k))
    return out


def _path_data(subs_list, px=_px):
    """One SVG path for several DrawingML paths.  DrawingML fills each path
    even-odd on its own; with several paths in one SVG path (nonzero), each
    path's outlines are turned so that its outer outlines run one way and
    its holes the other."""
    out = []
    many = len(subs_list) > 1
    for subs in subs_list:
        if many:
            subs = _orient(subs)
        for (sx, sy), segs, closed in subs:
            out.append('M%s %s' % (px(sx), px(sy)))
            for s in segs:
                out.append(s[0] + ' '.join(px(v) for v in s[1:]))
            if closed:
                out.append('Z')
    return ' '.join(out)


def _orient(subs):
    if len(subs) > 50:
        return subs
    polys = [pts for pts, _ in g.flatten(subs, steps=4)]
    out = []
    for i, sub in enumerate(subs):
        pts = polys[i]
        if len(pts) < 3:
            out.append(sub)
            continue
        depth = sum(1 for j, q in enumerate(polys) if j != i and len(q) >= 3
                    and g.point_in_polygon(pts[0][0], pts[0][1], q))
        want = 1 if depth % 2 == 0 else -1
        have = 1 if g.signed_area(pts) >= 0 else -1
        out.append(sub if want == have else _reverse(sub))
    return out


def _reverse(sub):
    start, segs, closed = sub
    pts = [start] + [s[-2:] for s in segs]
    rsegs = []
    for k in range(len(segs) - 1, -1, -1):
        s = segs[k]
        end = pts[k]
        if s[0] == 'C':
            rsegs.append(('C', s[3], s[4], s[1], s[2]) + tuple(end))
        elif s[0] == 'Q':
            rsegs.append(('Q', s[1], s[2]) + tuple(end))
        else:
            rsegs.append(('L',) + tuple(end))
    return [tuple(pts[-1]), rsegs, closed]
