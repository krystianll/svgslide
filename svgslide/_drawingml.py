"""DrawingML fragments: fills, lines, geometry and the shape tree."""

import math

EMU_PER_PX = 9525  # 914400 EMU per inch / 96 px per inch


def esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def esc_text(s):
    # XML 1.0 forbids most control characters
    s = ''.join(ch for ch in s if ch >= ' ' or ch in '\t')
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def alpha_val(a):
    return int(round(max(0.0, min(1.0, a)) * 100000))


def srgb(hexc, alpha=1.0):
    if alpha < 0.99999:
        return '<a:srgbClr val="%s"><a:alpha val="%d"/></a:srgbClr>' % (hexc.upper(), alpha_val(alpha))
    return '<a:srgbClr val="%s"/>' % hexc.upper()


def solid(hexc, alpha=1.0):
    return '<a:solidFill>' + srgb(hexc, alpha) + '</a:solidFill>'


def angle(rad):
    """ST_Angle (60000ths of a degree), normalised to [0, 360)."""
    deg = math.degrees(rad) % 360.0
    v = int(round(deg * 60000))
    return 0 if v >= 21600000 else v


def grad_fill(stops, lin_angle=None, focus=None):
    """stops = [(pos 0..1, hex, alpha)]; lin_angle in radians or focus=(fx, fy) 0..1."""
    gs = ''.join('<a:gs pos="%d">%s</a:gs>' % (int(round(p * 100000)), srgb(h, a))
                 for p, h, a in stops)
    if focus is not None:
        fx, fy = focus
        l, t = int(round(fx * 100000)), int(round(fy * 100000))
        shade = ('<a:path path="circle"><a:fillToRect l="%d" t="%d" r="%d" b="%d"/></a:path>'
                 % (l, t, 100000 - l, 100000 - t))
    else:
        shade = '<a:lin ang="%d" scaled="0"/>' % angle(lin_angle or 0.0)
    return '<a:gradFill rotWithShape="1"><a:gsLst>%s</a:gsLst>%s</a:gradFill>' % (gs, shade)


def line(fill_xml, width_emu, cap, join, miter, dash):
    """a:ln with children in schema order: fill, dash, join."""
    caps = {'round': 'rnd', 'square': 'sq'}.get(cap, 'flat')
    if join == 'round':
        j = '<a:round/>'
    elif join == 'bevel':
        j = '<a:bevel/>'
    else:
        j = '<a:miter lim="%d"/>' % int(round(max(1.0, miter) * 100000))
    d = ''
    if dash:
        d = '<a:custDash>' + ''.join('<a:ds d="%d" sp="%d"/>' % (a, b) for a, b in dash) + '</a:custDash>'
    return '<a:ln w="%d" cap="%s">%s%s%s</a:ln>' % (max(0, int(round(width_emu))), caps, fill_xml, d, j)


NO_LINE = '<a:ln><a:noFill/></a:ln>'


def cust_geom(paths, grid=None):
    """paths = [(subpaths, w, h, filled, stroked)] in shape-local EMU floats.

    grid: None, or a grid size in EMU: the path coordinate system then has
    one unit per grid step (at least one unit across), so coordinates are
    rounded to that grid and written with fewer digits.
    """
    out = ['<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>'
           '<a:rect l="l" t="t" r="r" b="b"/><a:pathLst>']
    for subs, w, h, filled, stroked in paths:
        attrs = ''
        if not filled:
            attrs += ' fill="none"'
        if not stroked:
            attrs += ' stroke="0"'
        if grid:
            wu, hu = max(1, round(w / grid)), max(1, round(h / grid))
            m = (wu / w if w > 0 else 1.0, 0, 0, hu / h if h > 0 else 1.0, 0, 0)
            subs = [((sx * m[0], sy * m[3]), [(s[0],) + tuple(v * (m[0] if i % 2 == 0 else m[3])
                                                               for i, v in enumerate(s[1:])) for s in segs], closed)
                    for (sx, sy), segs, closed in subs]
            w, h = wu, hu
        out.append('<a:path w="%d" h="%d"%s>' % (w, h, attrs))
        for (sx, sy), segs, closed in subs:
            out.append('<a:moveTo><a:pt x="%d" y="%d"/></a:moveTo>' % (round(sx), round(sy)))
            for s in segs:
                k = s[0]
                if k == 'L':
                    out.append('<a:lnTo><a:pt x="%d" y="%d"/></a:lnTo>' % (round(s[1]), round(s[2])))
                elif k == 'C':
                    out.append('<a:cubicBezTo><a:pt x="%d" y="%d"/><a:pt x="%d" y="%d"/>'
                               '<a:pt x="%d" y="%d"/></a:cubicBezTo>'
                               % (round(s[1]), round(s[2]), round(s[3]), round(s[4]),
                                  round(s[5]), round(s[6])))
                else:
                    out.append('<a:quadBezTo><a:pt x="%d" y="%d"/><a:pt x="%d" y="%d"/></a:quadBezTo>'
                               % (round(s[1]), round(s[2]), round(s[3]), round(s[4])))
            if closed:
                out.append('<a:close/>')
        out.append('</a:path>')
    out.append('</a:pathLst></a:custGeom>')
    return ''.join(out)


def prst_geom(name, adj=None):
    if adj is None:
        return '<a:prstGeom prst="%s"><a:avLst/></a:prstGeom>' % name
    return '<a:prstGeom prst="%s"><a:avLst><a:gd name="adj" fmla="val %d"/></a:avLst></a:prstGeom>' % (name, adj)


def xfrm(x, y, w, h, rot=0.0, flip_h=False, flip_v=False):
    attrs = ''
    r = angle(rot) if rot else 0
    if r:
        attrs += ' rot="%d"' % r
    if flip_h:
        attrs += ' flipH="1"'
    if flip_v:
        attrs += ' flipV="1"'
    return '<a:xfrm%s><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>' % (
        attrs, round(x), round(y), max(0, round(w)), max(0, round(h)))


# --- shape tree ------------------------------------------------------------

class Leaf:
    __slots__ = ('bbox', 'kind', 'label', 'head', 'body', 'look', 'geom')

    def __init__(self, bbox, kind, label, head, body):
        self.bbox = bbox      # (x0, y0, x1, y1) slide EMU, visual extent
        self.kind = kind      # 'shape', 'text', 'picture'
        self.label = label    # SVG id or None
        self.head = head      # opening tags up to cNvPr
        self.body = body      # everything after cNvPr
        self.look = None      # (fill xml, line xml) of a shape that may merge with identical ones
        self.geom = None      # its geometry in slide EMU: ('paths', [(subpaths, filled, stroked)])
        #                       or ('prst', 'rect'|'roundRect'|'ellipse', box, rotation, corner fraction)


class Group:
    __slots__ = ('label', 'children')

    def __init__(self, label, children):
        self.label = label
        self.children = children


def collapse(nodes):
    """Drop empty groups and replace single-child groups by their child."""
    out = []
    for n in nodes:
        if isinstance(n, Group):
            kids = collapse(n.children)
            if not kids:
                continue
            if len(kids) == 1:
                k = kids[0]
                if k.label is None and n.label is not None:
                    k.label = n.label
                out.append(k)
                continue
            n.children = kids
        out.append(n)
    return out


_DEFAULT_NAMES = {'shape': 'Freeform', 'text': 'TextBox', 'picture': 'Picture'}


class Serializer:
    def __init__(self):
        self.next_id = 2
        self.counts = {'shape': 0, 'text': 0, 'picture': 0, 'group': 0}

    def write(self, nodes, out):
        for n in nodes:
            self._node(n, out)

    def _node(self, n, out):
        nid = self.next_id
        self.next_id += 1
        if isinstance(n, Leaf):
            self.counts[n.kind] += 1
            name = esc(n.label) if n.label else '%s %d' % (_DEFAULT_NAMES[n.kind], nid)
            out.append(n.head)
            out.append('<p:cNvPr id="%d" name="%s"/>' % (nid, name))
            out.append(n.body)
            return
        self.counts['group'] += 1
        bb = node_bbox(n)
        x0, y0 = round(bb[0]), round(bb[1])
        w, h = max(1, round(bb[2]) - x0), max(1, round(bb[3]) - y0)
        name = esc(n.label) if n.label else 'Group %d' % nid
        out.append('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="%d" name="%s"/><p:cNvGrpSpPr/><p:nvPr/>'
                   '</p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/>'
                   '<a:chOff x="%d" y="%d"/><a:chExt cx="%d" cy="%d"/></a:xfrm></p:grpSpPr>'
                   % (nid, name, x0, y0, w, h, x0, y0, w, h))
        for k in n.children:
            self._node(k, out)
        out.append('</p:grpSp>')


def node_bbox(n):
    if isinstance(n, Leaf):
        return n.bbox
    bb = None
    for k in n.children:
        b = node_bbox(k)
        if bb is None:
            bb = list(b)
        else:
            bb[0] = min(bb[0], b[0])
            bb[1] = min(bb[1], b[1])
            bb[2] = max(bb[2], b[2])
            bb[3] = max(bb[3], b[3])
    return tuple(bb) if bb else (0, 0, 0, 0)
