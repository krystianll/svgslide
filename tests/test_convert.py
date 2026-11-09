"""Behaviour of svgslide.convert, checked on the .pptx it writes.

Each test feeds a small hand-written SVG and reads the slide back: which
objects exist, where they are (EMU, compared with the SVG geometry), their
colours, text, groups and pictures, and what the report says.
"""

import base64
import io
import math
import os
import re
import struct
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import svgslide

NS = {
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
}
EMU = 9525          # per CSS px (1/96 inch)
TOL = 0.02 * EMU    # positions are rounded to whole EMU


def svg(body, w=200, h=100, extra=''):
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="%s" height="%s" %s>%s</svg>' % (w, h, extra, body))


class Slide:
    """The parts of a written .pptx that a user can see."""

    def __init__(self, data, number=1):
        self.data = data
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.tree = ET.fromstring(self.zip.read('ppt/slides/slide%d.xml' % number))
        self.sptree = self.tree.find('p:cSld/p:spTree', NS)
        pres = ET.fromstring(self.zip.read('ppt/presentation.xml'))
        sz = pres.find('p:sldSz', NS)
        self.size = (int(sz.get('cx')), int(sz.get('cy')))
        rels = ET.fromstring(self.zip.read('ppt/slides/_rels/slide%d.xml.rels' % number))
        self.rels = {r.get('Id'): r.get('Target') for r in rels}

    def objects(self, kind=None, root=None):
        """All shapes/pictures/groups, depth first: [(element, kind)]."""
        out = []
        for el in (root if root is not None else self.sptree):
            tag = el.tag.rsplit('}', 1)[1]
            if tag in ('sp', 'pic', 'grpSp'):
                k = {'pic': 'picture', 'grpSp': 'group'}.get(tag)
                if tag == 'sp':
                    k = 'text' if el.find('p:txBody', NS) is not None else 'shape'
                if kind is None or k == kind:
                    out.append(el)
                if tag == 'grpSp':
                    out.extend(self.objects(kind, el))
        return out

    def by_name(self, name):
        for el in self.sptree.iter():
            c = el.find('*/p:cNvPr', NS)
            if c is not None and c.get('name') == name and el.tag.rsplit('}', 1)[1] in ('sp', 'pic', 'grpSp'):
                return el
        raise KeyError(name)

    def media(self, pic):
        rid = pic.find('.//a:blip', NS).get('{%s}embed' % NS['r'])
        return self.zip.read(os.path.normpath('ppt/slides/' + self.rels[rid]).replace(os.sep, '/'))


def box(el):
    """(x, y, w, h, rotation_degrees) of an object's xfrm in EMU."""
    x = el.find('*/a:xfrm', NS)
    off, ext = x.find('a:off', NS), x.find('a:ext', NS)
    return (int(off.get('x')), int(off.get('y')), int(ext.get('cx')), int(ext.get('cy')),
            int(x.get('rot', '0')) / 60000)


def fill_of(el):
    """(hex, alpha) of a solid fill, 'none', or the fill element tag."""
    sppr = el.find('p:spPr', NS)
    for child in sppr:
        tag = child.tag.rsplit('}', 1)[1]
        if tag == 'noFill':
            return 'none'
        if tag == 'solidFill':
            return _color(child)
        if tag in ('gradFill', 'pattFill', 'blipFill'):
            return tag
    return None


def _color(parent):
    c = parent.find('a:srgbClr', NS)
    a = c.find('a:alpha', NS)
    return (c.get('val'), int(a.get('val')) / 100000 if a is not None else 1.0)


def line_of(el):
    ln = el.find('p:spPr/a:ln', NS)
    if ln is None or ln.find('a:noFill', NS) is not None:
        return None
    return ln


def texts(slide):
    return [''.join(t.text or '' for t in el.iter('{%s}t' % NS['a'])) for el in slide.objects('text')]


def convert(source, **kw):
    buf = io.BytesIO()
    report = svgslide.convert(source, buf, **kw)
    return Slide(buf.getvalue()), report


def png(rows):
    """8-bit RGB PNG from rows of (r, g, b) pixels."""
    h, w = len(rows), len(rows[0])
    raw = b''.join(b'\x00' + bytes(v for px in row for v in px) for row in rows)

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def decode_png(data):
    """[[(r, g, b)...]...] of an 8-bit RGB/RGBA, non-interlaced PNG."""
    i, idat = 8, []
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
    rows = []
    for r in range(h):
        ft = raw[r * (stride + 1)]
        line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for k in range(stride):
            a = line[k - bpp] if k >= bpp else 0
            b = prev[k]
            c = prev[k - bpp] if k >= bpp else 0
            if ft == 1:
                line[k] = (line[k] + a) & 255
            elif ft == 2:
                line[k] = (line[k] + b) & 255
            elif ft == 3:
                line[k] = (line[k] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[k] = (line[k] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        row = []
        for k in range(0, stride, bpp):
            px = line[k:k + 3]
            if bpp == 4:
                al = line[k + 3]
                px = [(v * al + 255 * (255 - al)) // 255 for v in px]
            row.append(tuple(px))
        rows.append(row)
    return rows


def data_uri(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


# --- package ----------------------------------------------------------------

@pytest.mark.parametrize('attrs, expected', [
    pytest.param('width="200" height="100"', (200 * EMU, 100 * EMU), id='px'),
    pytest.param('width="72pt" height="144pt"', (914400, 1828800), id='pt'),
    pytest.param('width="25.4mm" height="50.8mm"', (914400, 1828800), id='mm'),
    pytest.param('viewBox="0 0 300 150"', (300 * EMU, 150 * EMU), id='viewBox only'),
    pytest.param('width="100%" height="200" viewBox="0 0 50 100"', (100 * EMU, 200 * EMU),
                 id='percent width follows viewBox aspect'),
    pytest.param('width="10000" height="1000"', (51206400, 5120640), id='too large: scaled to 56 inch'),
    pytest.param('width="10" height="7"', (914400, 914400), id='too small: 1 inch minimum'),
])
def test_slide_size_follows_drawing(attrs, expected):
    s, rep = convert('<svg xmlns="http://www.w3.org/2000/svg" %s><rect width="10" height="10"/></svg>' % attrs)
    assert s.size == expected
    assert rep.slide_size == expected


# New test, not a widened one: test_slide_size_follows_drawing reads the size
# from the SVG's own attributes; these cases add the size options, and check
# where the whole drawing lands and how large its parts become.
IN, CM = 914400, 360000
POINT = IN // 72     # EMU
_FIGURE = ('<rect x="10" y="10" width="50" height="20" fill="red" stroke="black" stroke-width="2"/>'
           '<text x="10" y="80" font-size="12">Sample A</text>'
           '<image x="150" y="10" width="20" height="20" xlink:href="%s"/>' % data_uri(png([[(1, 2, 3)]])))


@pytest.mark.parametrize('figure, kw, slide, k, offset, note', [
    pytest.param((200, 100), {}, (200 * EMU, 100 * EMU), EMU, (0, 0), None, id='default: 1 px = 1/96 inch'),
    pytest.param((200, 100), {'scale': 2}, (400 * EMU, 200 * EMU), 2 * EMU, (0, 0), None,
                 id='scale grows geometry, text, lines and pictures'),
    pytest.param((200, 100), {'scale': 0.5}, (100 * EMU, IN), EMU / 2, (0, 0), None,
                 id='scaled below 1 inch: the slide stays 1 inch, figure at top left'),
    pytest.param((200, 100), {'width': '10cm'}, (10 * CM, 5 * CM), 10 * CM / 200, (0, 0), None,
                 id='width in cm, height follows'),
    pytest.param((200, 100), {'width': '4 in'}, (4 * IN, 2 * IN), 4 * IN / 200, (0, 0), None,
                 id='width with a space before the unit'),
    pytest.param((200, 100), {'height': '150px'}, (300 * EMU, 150 * EMU), 1.5 * EMU, (0, 0), None,
                 id='height in px, width follows'),
    pytest.param((200, 100), {'width': 300}, (300 * POINT, 150 * POINT), 300 * POINT / 200, (0, 0), None,
                 id='plain number is points'),
    pytest.param((200, 100), {'width': '300pt', 'height': 151}, (300 * POINT, 150 * POINT), 300 * POINT / 200, (0, 0),
                 None, id='width and height within 1 percent of the aspect ratio'),
    pytest.param((200, 100), {'width': '80mm'}, (80 * CM // 10, 40 * CM // 10), 80 * CM / 10 / 200, (0, 0),
                 None, id='width in mm'),
    pytest.param((200, 100), {'canvas_size': 'widescreen'}, (12192000, 6858000), EMU,
                 ((12192000 - 200 * EMU) / 2, (6858000 - 100 * EMU) / 2), None,
                 id='widescreen canvas: PowerPoint size, figure centred at its own size'),
    pytest.param((200, 100), {'canvas_size': '4:3', 'scale': 2}, (9144000, 6858000), 2 * EMU,
                 ((9144000 - 400 * EMU) / 2, (6858000 - 200 * EMU) / 2), None, id='4:3 canvas with scale'),
    pytest.param((200, 100), {'canvas_size': 'a4'}, (29.7 * CM, 21 * CM), EMU,
                 ((29.7 * CM - 200 * EMU) / 2, (21 * CM - 100 * EMU) / 2), None,
                 id='A4 is landscape, names ignore case'),
    pytest.param((200, 100), {'canvas_size': 'A4-portrait', 'width': '40cm'}, (21 * CM, 29.7 * CM),
                 21 * CM / 200, (0, (29.7 * CM - 10.5 * CM) / 2), 'scaled to 52.5% to fit the canvas',
                 id='figure larger than the canvas is shrunk to fit and centred'),
    pytest.param((200, 100), {'canvas_size': '25cm x 14cm'}, (25 * CM, 14 * CM), EMU,
                 ((25 * CM - 200 * EMU) / 2, (14 * CM - 100 * EMU) / 2), None, id='custom canvas with units'),
    pytest.param((200, 100), {'canvas_size': '10inx5in'}, (10 * IN, 5 * IN), EMU,
                 ((10 * IN - 200 * EMU) / 2, (5 * IN - 100 * EMU) / 2), None, id='custom canvas without spaces'),
    pytest.param((200, 100), {'canvas_size': (720, 360)}, (720 * POINT, 360 * POINT), EMU,
                 ((720 * POINT - 200 * EMU) / 2, (360 * POINT - 100 * EMU) / 2), None, id='canvas tuple in points'),
    pytest.param((200, 100), {'scale': 100}, (56 * IN, 28 * IN), 56 * IN / 200, (0, 0),
                 "scaled to 26.9% to fit PowerPoint's 56-inch slide limit", id='scaled past 56 inch: limit and note'),
    pytest.param((10000, 1000), {}, (56 * IN, 5.6 * IN), 56 * IN / 10000, (0, 0),
                 "scaled to 53.8% to fit PowerPoint's 56-inch slide limit",
                 id='drawing past 56 inch without options: note too'),
])
def test_size_options_place_the_whole_drawing(figure, kw, slide, k, offset, note):
    s, rep = convert(svg(_FIGURE, *figure), **kw)
    assert s.size == rep.slide_size == tuple(int(round(v)) for v in slide)
    ox, oy = offset
    rect, = s.objects('shape')
    assert all(abs(g - v) <= 2 for g, v in zip(box(rect)[:4], (ox + 10 * k, oy + 10 * k, 50 * k, 20 * k)))
    assert abs(int(line_of(rect).get('w')) - 2 * k) <= 1
    pic, = s.objects('picture')
    assert all(abs(g - v) <= 2 for g, v in zip(box(pic)[:4], (ox + 150 * k, oy + 10 * k, 20 * k, 20 * k)))
    sz = int(s.objects('text')[0].find('.//a:rPr', NS).get('sz'))
    assert abs(sz - 12 * k / POINT * 100) <= 1                     # hundredths of a point
    assert rep.notes == ([] if note is None else [note])


def test_size_and_deck_options_are_checked():
    fig = svg('<rect width="5" height="5"/>', 200, 100)
    for kw, match in [({'scale': 0}, 'scale'), ({'scale': float('inf')}, 'scale'), ({'scale': '2'}, 'scale'),
                      ({'scale': True}, 'scale'), ({'scale': 2, 'width': '3cm'}, 'not both'),
                      ({'width': '3 furlongs'}, 'width'), ({'width': -3}, 'width'), ({'height': '0cm'}, 'height'),
                      ({'width': '10cm', 'height': '10cm'}, 'aspect ratio'),
                      ({'canvas_size': 'huge'}, 'canvas_size'), ({'canvas_size': (720,)}, 'canvas_size'),
                      ({'canvas_size': '0.5in x 3in'}, '56 inch'), ({'canvas_size': '57in x 3in'}, '56 inch'),
                      ({'canvas_size': '25cm'}, 'canvas_size')]:
        with pytest.raises(ValueError, match=match):
            convert(fig, **kw)
    for source, kw, match in [([], {}, 'empty'), ([fig, fig], {'pages': 1}, 'one source'),
                              ([fig, fig], {'format': 'svg'}, 'one page')]:
        with pytest.raises(ValueError, match=match):
            convert(source, **kw)


def test_package_is_complete_deterministic_and_accepts_every_input_kind(tmp_path):
    src = svg('<rect id="r" x="10" y="10" width="50" height="20" fill="red"/>'
              '<image x="0" y="0" width="4" height="4" xlink:href="%s"/>' % data_uri(png([[(1, 2, 3)]])))
    a, _ = convert(src)
    # every part named by a relationship or content type exists
    names = set(a.zip.namelist())
    assert a.zip.namelist()[0] == '[Content_Types].xml'
    for rels in [n for n in names if n.endswith('.rels')]:
        base = rels.replace('_rels/', '').rsplit('.rels', 1)[0]
        folder = base.rsplit('/', 1)[0] if '/' in base else ''
        for r in ET.fromstring(a.zip.read(rels)):
            target = os.path.normpath(os.path.join(folder, r.get('Target'))).replace(os.sep, '/')
            assert target in names, (rels, target)
    ct = a.zip.read('[Content_Types].xml').decode()
    assert 'Extension="png"' in ct
    # same bytes from text, bytes, a path, and into a path or a file object
    p = tmp_path / 'in.svg'
    p.write_bytes(src.encode())
    b, _ = convert(src.encode())
    c, _ = convert(p)
    out = tmp_path / 'out.pptx'
    svgslide.convert(str(p), str(out))
    assert a.data == b.data == c.data == out.read_bytes()
    # shape ids are unique
    ids = [c.get('id') for c in a.tree.iter('{%s}cNvPr' % NS['p'])]
    assert len(ids) == len(set(ids))


def test_svgz_and_bad_input(tmp_path):
    import gzip
    p = tmp_path / 'x.svgz'
    p.write_bytes(gzip.compress(svg('<rect width="5" height="5"/>').encode()))
    s, rep = convert(p)
    assert rep.shapes == 1
    with pytest.raises(ValueError):
        convert('<html></html>')


# --- shapes -------------------------------------------------------------------

@pytest.mark.parametrize('body, bbox', [
    pytest.param('<rect id="s" x="10" y="20" width="50" height="30"/>', (10, 20, 50, 30), id='rect'),
    pytest.param('<rect id="s" x="10" y="20" width="50" height="30" rx="5"/>', (10, 20, 50, 30),
                 id='rounded rect'),
    pytest.param('<circle id="s" cx="50" cy="50" r="20"/>', (30, 30, 40, 40), id='circle'),
    pytest.param('<ellipse id="s" cx="50" cy="50" rx="30" ry="10"/>', (20, 40, 60, 20), id='ellipse'),
    pytest.param('<g transform="translate(5 6) scale(2)"><rect id="s" width="10" height="5"/></g>',
                 (5, 6, 20, 10), id='group transform'),
    pytest.param('<path id="s" d="M10 10 h40 v20 H10 z"/>', (10, 10, 40, 20), id='path h/v'),
    pytest.param('<path id="s" d="m10 10 l40 0 l0 20 l-40 0 z"/>', (10, 10, 40, 20), id='relative path'),
    pytest.param('<path id="s" d="M10 50 A40 40 0 0 1 90 50 z"/>', (10, 10, 80, 40), id='arc'),
    pytest.param('<path id="s" d="M10 50 Q50 10 90 50 T170 50 z"/>', (10, 30, 160, 40), id='quadratic'),
    pytest.param('<path id="s" d="M10 50 C10 10 90 10 90 50 S170 90 170 50"/>', (10, 20, 160, 60),
                 id='cubic, smooth'),
    pytest.param('<polygon id="s" points="10,10 60,10 35,40"/>', (10, 10, 50, 30), id='polygon'),
    pytest.param('<svg x="10" y="10" width="40" height="40" viewBox="0 0 10 10">'
                 '<rect id="s" width="5" height="10"/></svg>', (10, 10, 20, 40), id='nested viewport'),
    pytest.param('<rect id="s" x="10" y="20" width="50" height="30" transform="rotate(90 35 35)"/>',
                 (20, 10, 30, 50), id='rotated rect (visual extent)'),
])
def test_shape_geometry_matches_svg(body, bbox):
    s, rep = convert(svg(body))
    el = s.by_name('s')
    x, y, w, h, rot = box(el)
    if rot:  # report the visual extent of the rotated box
        t = math.radians(rot)
        cx, cy = x + w / 2, y + h / 2
        hw = abs(w / 2 * math.cos(t)) + abs(h / 2 * math.sin(t))
        hh = abs(w / 2 * math.sin(t)) + abs(h / 2 * math.cos(t))
        x, y, w, h = cx - hw, cy - hh, 2 * hw, 2 * hh
    for got, want in zip((x, y, w, h), bbox):
        assert abs(got - want * EMU) <= TOL + 1, (body, (x, y, w, h), bbox)
    assert rep.shapes == 1 and rep.skipped == []


@pytest.mark.parametrize('body, expected_fill, expected_line', [
    pytest.param('<rect id="s" width="10" height="10"/>', ('000000', 1.0), None, id='default black fill'),
    pytest.param('<rect id="s" width="10" height="10" fill="none" stroke="#f00" stroke-width="2"/>',
                 'none', ('FF0000', 2), id='stroke only'),
    pytest.param('<rect id="s" width="10" height="10" style="fill: rgb(0, 128, 255); fill-opacity: .5"/>',
                 ('0080FF', 0.5), None, id='style attribute, opacity'),
    pytest.param('<style>.c { fill: teal } #s { stroke: blue }</style><rect id="s" class="c" fill="red" '
                 'width="10" height="10"/>', ('008080', 1.0), ('0000FF', 1), id='css beats attribute'),
    pytest.param('<style>.c { fill: teal }</style><rect id="s" class="c" style="fill: gold" '
                 'width="10" height="10"/>', ('FFD700', 1.0), None, id='style beats css'),
    pytest.param('<style>g.k rect { fill: none; stroke: #123456 }</style><g class="k"><g>'
                 '<rect id="s" width="10" height="10"/></g></g>', 'none', ('123456', 1), id='descendant selector'),
    pytest.param('<style>* { stroke: lime }</style><rect id="s" width="10" height="10"/>',
                 ('000000', 1.0), ('00FF00', 1), id='universal selector'),
    pytest.param('<g fill="purple" stroke="black" stroke-width="3" opacity="0.5"><g opacity="0.5">'
                 '<rect id="s" width="10" height="10"/></g></g>', ('800080', 0.25), ('000000', 3),
                 id='inherited, group opacity multiplies'),
    pytest.param('<g style="color: #abcdef"><rect id="s" fill="currentColor" width="10" height="10"/></g>',
                 ('ABCDEF', 1.0), None, id='currentColor'),
    pytest.param('<rect id="s" fill="hsl(120, 100%, 50%)" width="10" height="10"/>', ('00FF00', 1.0), None,
                 id='hsl colour'),
    pytest.param('<line id="s" x1="0" y1="0" x2="10" y2="0" stroke="#0000ff"/>', 'none', ('0000FF', 1),
                 id='line has no fill'),
])
def test_paint_cascade(body, expected_fill, expected_line):
    s, _ = convert(svg(body))
    el = s.by_name('s')
    assert fill_of(el) == expected_fill
    ln = line_of(el)
    if expected_line is None:
        assert ln is None
    else:
        assert _color(ln.find('a:solidFill', NS))[0] == expected_line[0]
        assert abs(int(ln.get('w')) - expected_line[1] * EMU) <= 1


@pytest.mark.parametrize('style, cap, join, dash', [
    pytest.param('stroke-dasharray: 4 2; stroke-linecap: butt', 'flat', 'miter', [(200000, 100000)], id='dash'),
    pytest.param('stroke-dasharray: 2; stroke-linecap: round; stroke-linejoin: round', 'rnd', 'round',
                 [(100000, 100000)], id='odd dash list repeats'),
    pytest.param('stroke-linecap: square; stroke-linejoin: bevel', 'sq', 'bevel', None, id='solid'),
])
def test_stroke_details(style, cap, join, dash):
    s, _ = convert(svg('<path id="s" d="M0 0 L50 50" stroke="black" stroke-width="2" style="%s"/>' % style))
    ln = line_of(s.by_name('s'))
    assert ln.get('cap') == cap
    assert ln.find('a:' + join, NS) is not None
    got = [(int(d.get('d')), int(d.get('sp'))) for d in ln.iter('{%s}ds' % NS['a'])]
    assert got == (dash or [])


def test_fill_rule_keeps_holes_and_overlaps():
    # evenodd donut: one path (hole); nonzero same-direction squares: no hole
    s, _ = convert(svg('<path id="donut" fill-rule="evenodd" d="M0 0h40v40H0z M10 10h20v20H10z"/>'
                       '<path id="nested" d="M50 0h40v40H50z M60 10h20v20H60z"/>'
                       '<path id="hole" d="M100 0h40v40h-40z M110 10v20h20v-20z"/>'))
    paths = {n: len(s.by_name(n).findall('.//a:path', NS)) for n in ('donut', 'nested', 'hole')}
    assert paths == {'donut': 1, 'nested': 2, 'hole': 1}


@pytest.mark.parametrize('body, expected', [
    pytest.param('<rect id="s" x="0" y="0" width="100" height="100" fill="red"/>', (20, 10, 60, 40),
                 id='rect cut to clip'),
    pytest.param('<path id="s" d="M0 30 L100 30" stroke="red"/>', (20, 30, 60, 0), id='line cut to clip'),
    pytest.param('<rect id="s" x="30" y="20" width="10" height="10" fill="red"/>', (30, 20, 10, 10),
                 id='inside: unchanged'),
    pytest.param('<path id="s" d="M0 30 L100 30" fill="none" stroke="red" stroke-width="4" stroke-linecap="square"/>',
                 (22, 30, 56, 0), id='square caps at the clip edge pulled back by half the width'),
    pytest.param('<path id="s" d="M0 30 L100 30" fill="none" stroke="red" stroke-width="4" stroke-linecap="round"/>',
                 (22, 30, 56, 0), id='round caps at the clip edge pulled back by half the width'),
    pytest.param('<path id="s" d="M50 30 L100 30" fill="none" stroke="red" stroke-width="4" stroke-linecap="square"/>',
                 (50, 30, 28, 0), id='an end inside the clip keeps its place'),
    pytest.param('<path id="s" d="M0 30 L100 30" fill="none" stroke="red" stroke-width="4" stroke-linecap="butt"/>',
                 (20, 30, 60, 0), id='butt caps stay on the clip edge'),
])
def test_rectangular_clip(body, expected):
    s, rep = convert(svg('<clipPath id="c"><rect x="20" y="10" width="60" height="40"/></clipPath>'
                         '<g clip-path="url(#c)">%s</g><rect x="150" y="0" width="5" height="5"/>' % body))
    x, y, w, h, _ = box(s.by_name('s'))
    for got, want in zip((x, y, w, h), expected):
        assert abs(got - want * EMU) <= max(TOL, 1.5 * EMU if want == 0 else 0) + 1
    assert rep.skipped == []


def test_shape_fully_outside_clip_is_dropped_and_off_canvas_content_too():
    _, rep = convert(svg('<clipPath id="c"><rect width="10" height="10"/></clipPath>'
                         '<rect clip-path="url(#c)" x="50" y="50" width="5" height="5"/>'
                         '<rect x="500" y="500" width="5" height="5"/>'))
    assert rep.shapes == 0


# --- gradients ------------------------------------------------------------------

def test_gradients_follow_href_chains():
    s, rep = convert(svg(
        '<defs><linearGradient id="base"><stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" style="stop-color: #0000ff; stop-opacity: 0.5"/></linearGradient>'
        '<linearGradient id="lin" xlink:href="#base" x1="0" y1="0" x2="0" y2="1"/>'
        '<radialGradient id="rad" xlink:href="#base" cx="0.5" cy="0.5" r="0.5"/></defs>'
        '<rect id="v" width="50" height="50" fill="url(#lin)"/>'
        '<rect id="r" x="60" width="50" height="50" fill="url(#rad)"/>'
        '<rect id="f" x="120" width="50" height="50" fill="url(#missing) green"/>'))
    for name in ('v', 'r'):
        grad = s.by_name(name).find('p:spPr/a:gradFill', NS)
        stops = [(int(gs.get('pos')), _color(gs)) for gs in grad.findall('a:gsLst/a:gs', NS)]
        assert stops[0][1] == ('FF0000', 1.0) and stops[-1][1] == ('0000FF', 0.5), name
    v = s.by_name('v').find('p:spPr/a:gradFill', NS)
    assert int(v.find('a:lin', NS).get('ang')) == 90 * 60000          # top to bottom
    r = s.by_name('r').find('p:spPr/a:gradFill', NS)
    assert r.find('a:path', NS).get('path') == 'circle'
    assert fill_of(s.by_name('f')) == ('008000', 1.0)                   # fallback colour
    assert rep.skipped == []


# --- text --------------------------------------------------------------------------

def _anchor_in_box(el, ax, ay):
    """The SVG anchor point in the text box's own (unrotated) frame."""
    x, y, w, h, rot = box(el)
    cx, cy = x + w / 2, y + h / 2
    t = -math.radians(rot)
    dx, dy = ax * EMU - cx, ay * EMU - cy
    return (cx + dx * math.cos(t) - dy * math.sin(t), cy + dx * math.sin(t) + dy * math.cos(t)), (x, y, w, h)


@pytest.mark.parametrize('attrs, align, rot', [
    pytest.param('', 'l', 0, id='start'),
    pytest.param('text-anchor="middle"', 'ctr', 0, id='middle'),
    pytest.param('text-anchor="end"', 'r', 0, id='end'),
    pytest.param('transform="rotate(-90 100 60)"', 'l', 270, id='rotated start'),
    pytest.param('text-anchor="end" transform="rotate(30 100 60)"', 'r', 30, id='rotated end'),
    pytest.param('text-anchor="middle" transform="rotate(90 100 60)"', 'ctr', 90, id='rotated middle'),
])
def test_text_box_is_anchored_at_the_svg_point(attrs, align, rot):
    s, rep = convert(svg('<text id="t" x="100" y="60" font-size="12" %s>GENE1 label</text>' % attrs, 300, 200))
    el = s.by_name('t')
    assert texts(s) == ['GENE1 label']
    assert el.find('.//a:pPr', NS).get('algn') == align
    assert el.find('.//a:bodyPr', NS).get('wrap') == 'none'
    (lx, ly), (x, y, w, h) = _anchor_in_box(el, 100, 60)
    assert abs(box(el)[4] - rot) < 1e-3
    # the anchor lies on the box edge given by the alignment, inside its height
    edge = {'l': x, 'ctr': x + w / 2, 'r': x + w}[align]
    assert abs(lx - edge) <= TOL + 1
    assert y < ly < y + h
    assert rep.text_boxes == 1


# PowerPoint lays a single-spaced line out 1.2 em high with the baseline at
# Arial's ascent, 0.905 em, below its top.
BASELINE_FRAC = 0.905 / 1.2


def _curve(t, path_shift=(0.0, 0.0), turn=0.0):
    """Point and tangent angle (degrees) of the test curve M0 50 C50 0 100 100 150 50,
    moved by the path's translate and turned by the text's rotate (about 0, 0)."""
    p = [(0, 50), (50, 0), (100, 100), (150, 50)]
    u = 1 - t
    x = u ** 3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t ** 3 * p[3][0]
    y = u ** 3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t ** 3 * p[3][1]
    dx = 3 * u * u * (p[1][0] - p[0][0]) + 6 * u * t * (p[2][0] - p[1][0]) + 3 * t * t * (p[3][0] - p[2][0])
    dy = 3 * u * u * (p[1][1] - p[0][1]) + 6 * u * t * (p[2][1] - p[1][1]) + 3 * t * t * (p[3][1] - p[2][1])
    x, y = x + path_shift[0], y + path_shift[1]
    r = math.radians(turn)
    return (x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r),
            math.degrees(math.atan2(dy, dx)) + turn)


@pytest.mark.parametrize('text_attrs, path_attrs, tp_attrs, n_chars, report', [
    pytest.param('', '', '', 5, None, id='start of path'),
    pytest.param('', '', 'startOffset="40"', 5, None, id='startOffset length'),
    pytest.param('', '', 'startOffset="25%"', 5, None, id='startOffset percent'),
    pytest.param('text-anchor="middle"', '', 'startOffset="50%"', 5, None, id='middle anchor'),
    pytest.param('transform="rotate(15)"', 'transform="translate(20 10)"', '', 5, None,
                 id='path and text transforms'),
    pytest.param('', '', 'startOffset="145"', 3, 'textPath text beyond path end', id='beyond path end'),
    pytest.param('', '', 'method="stretch"', 5, 'textPath method="stretch"', id='unsupported method'),
    pytest.param('', '', 'xlink:href="#missing"', 0, 'textPath without a path (unresolvable href)',
                 id='unresolvable href'),
])
def test_text_on_path_is_one_box_per_character(text_attrs, path_attrs, tp_attrs, n_chars, report):
    tp = tp_attrs if 'href' in tp_attrs else 'xlink:href="#c" ' + tp_attrs
    s, rep = convert(svg('<path id="c" fill="none" %s d="M0 50 C50 0 100 100 150 50"/>'
                         '<text id="t" font-size="12" font-family="Arial" font-weight="bold" fill="#336699" %s>'
                         '<textPath id="e" %s>along</textPath></text>' % (path_attrs, text_attrs, tp)))
    assert rep.skipped == ([(report, '#e')] if report else [])
    if not n_chars:
        assert texts(s) == []
        return
    shift = (20.0, 10.0) if 'translate' in path_attrs else (0.0, 0.0)
    turn = 15.0 if 'rotate' in text_attrs else 0.0
    # the curve sampled densely: points, tangent angles and arc length (px)
    samples, arc = [], [0.0]
    for k in range(4001):
        samples.append(_curve(k / 4000, shift, turn))
        if k:
            arc.append(arc[-1] + math.hypot(samples[k][0] - samples[k - 1][0],
                                            samples[k][1] - samples[k - 1][1]))
    total = arc[-1]
    group = s.by_name('t')
    assert group.tag.endswith('grpSp')
    boxes = s.objects('text', group)
    assert texts(s) == list('along'[:n_chars]) and len(boxes) == n_chars
    positions = []
    for el in boxes:
        rpr = el.find('.//a:rPr', NS)
        assert int(rpr.get('sz')) == 900 and rpr.get('b') == '1'           # 12 px bold
        assert rpr.find('a:latin', NS).get('typeface') == 'Arial'
        assert _color(rpr.find('a:solidFill', NS))[0] == '336699'
        assert el.find('.//a:pPr', NS).get('algn') == 'ctr'
        # the middle of the character on its baseline, back in slide px
        x, y, w, h, rot = box(el)
        cx, cy = x + w / 2, y + h / 2
        by = y + BASELINE_FRAC * h - cy
        t = math.radians(rot)
        qx, qy = (cx - by * math.sin(t)) / EMU, (cy + by * math.cos(t)) / EMU
        k = min(range(len(samples)), key=lambda i: (samples[i][0] - qx) ** 2 + (samples[i][1] - qy) ** 2)
        # on the curve, within 0.05 px: half the 0.04 px sample spacing plus the
        # converter's flattening (0.001 em = 0.012 px) and EMU rounding
        assert math.hypot(samples[k][0] - qx, samples[k][1] - qy) < 0.05
        # turned to the tangent there, within 0.2 degrees (measured error ~0.02)
        assert abs((rot - samples[k][2] + 180) % 360 - 180) < 0.2
        positions.append(arc[k])
    assert positions == sorted(positions)                                   # a, l, o, n, g in order
    first, last = positions[0], positions[-1]
    if 'startOffset="40"' in tp_attrs:
        assert 40 < first < 40 + 0.6 * 12
    elif '25%' in tp_attrs:
        assert 0.25 * total < first < 0.25 * total + 0.6 * 12
    elif 'middle' in text_attrs:
        # 'a' and 'g' are equally wide: the label is centred on the half-way point
        assert abs((first + last) / 2 - total / 2) < 0.5
    elif 'startOffset="145"' in tp_attrs:
        assert first > 145 and last < total
    else:
        assert 0 < first < 0.6 * 12


@pytest.mark.parametrize('style, fonts, face, bold, italic', [
    pytest.param("font: italic bold 16px 'DejaVu Sans', sans-serif", None, 'DejaVu Sans', True, True,
                 id='font shorthand, name kept'),
    pytest.param("font-family: 'DejaVu Sans', sans-serif; font-size: 16px", {'DejaVu Sans': 'Arial'},
                 'Arial', False, False, id='font table'),
    pytest.param('font-family: sans-serif; font-size: 12pt; font-weight: 700', None, 'Arial', True, False,
                 id='generic family'),
    pytest.param("font-family: 'DejaVu Sans'; font-size: 16px", 'Calibri', 'Calibri', False, False,
                 id='a string replaces every font'),
    pytest.param('font-family: sans-serif; font-size: 16px', 'Calibri', 'Calibri', False, False,
                 id='a string replaces generic families too'),
    pytest.param("font-family: 'DejaVu Sans'; font-size: 16px", {'*': 'Arial', 'Symbol': 'Symbol'}, 'Arial',
                 False, False, id='"*" replaces fonts not named'),
    pytest.param('font-family: Symbol; font-size: 16px', {'*': 'Arial', 'Symbol': 'Symbol'}, 'Symbol',
                 False, False, id='a named font wins over "*"'),
    pytest.param('font-family: symbol; font-size: 16px', {'*': 'Arial', 'SYMBOL': 'Symbol'}, 'Symbol',
                 False, False, id='names match without regard to case'),
])
def test_text_font(style, fonts, face, bold, italic):
    s, _ = convert(svg('<text id="t" x="10" y="50" style="%s">Sample A</text>' % style), fonts=fonts)
    rpr = s.by_name('t').find('.//a:rPr', NS)
    assert rpr.find('a:latin', NS).get('typeface') == face
    assert int(rpr.get('sz')) == 1200                          # 16 px = 12 pt
    assert (rpr.get('b') == '1') == bold and (rpr.get('i') == '1') == italic


def test_text_runs_lines_and_whitespace():
    s, _ = convert(svg(
        '<text id="t" x="10" y="20" font-size="10" fill="#ff0000">  Sample\n   <tspan fill="#0000ff" '
        'font-weight="bold">A</tspan><tspan dy="-4" font-size="6">2</tspan></text>'
        '<text id="m" x="10" y="60" font-size="10"><tspan x="10" y="60">line one</tspan>'
        '<tspan x="10" y="72">line two</tspan></text>', 200, 100))
    t = s.by_name('t')
    runs = [(r.find('a:t', NS).text, r.find('a:rPr', NS)) for r in t.iter('{%s}r' % NS['a'])]
    assert [r[0] for r in runs] == ['Sample ', 'A', '2']
    assert _color(runs[0][1].find('a:solidFill', NS))[0] == 'FF0000'
    assert _color(runs[1][1].find('a:solidFill', NS))[0] == '0000FF' and runs[1][1].get('b') == '1'
    assert int(runs[2][1].get('baseline')) > 0                 # superscript
    m = s.by_name('m')
    assert m.tag.endswith('grpSp')
    assert sorted(texts(s)) == ['Sample A2', 'line one', 'line two']


# Helvetica advance widths (em) of the characters used below
_ADV = {'0': 0.556, '1': 0.556, '2': 0.556, '4': 0.556, '6': 0.556, '8': 0.556, 'a': 0.556, 'b': 0.556,
        'c': 0.5, 'd': 0.556}


def _pen(fs, *steps):
    """Pen positions: each step is (characters already drawn, dx before the next)."""
    x, out = 0.0, []
    for chars, dx in steps:
        x += sum(_ADV[c] for c in chars) * fs + dx
        out.append(x)
    return out


_INK_X = _pen(12, ('', 0), ('2', 63.84), ('4', 63.851402), ('6', 63.84), ('8', 60.3475))


# New test, not a widened one: the existing text tests check one box per
# line; per-character position lists decide how a line splits into boxes.
@pytest.mark.parametrize('body, expected', [
    pytest.param('<text transform="matrix(1.3333,0,0,1.3333,137.99,319.63)"><tspan style="font-size:12px;'
                 'font-family:Helvetica" x="0" dx="0 63.84 63.851402 63.84 60.3475 0.324" y="0">246810</tspan>'
                 '</text>',
                 [(t, 137.99 + 1.3333 * x, 319.63) for t, x in zip(['2', '4', '6', '8', '10'], _INK_X)],
                 id='Inkscape PDF import: dx list, one box per tick label'),
    pytest.param('<text x="10" y="50" font-size="12" font-family="Helvetica" dx="0 0.3 -0.2 0.4">abcd</text>',
                 [('abcd', 10, 50)], id='small kerning dx stays one box'),
    pytest.param('<text x="10 40 46.7" y="50" font-size="12" font-family="Helvetica">abc</text>',
                 [('a', 10, 50), ('bc', 40, 50)], id='absolute x list'),
    pytest.param('<text x="10" y="50" font-size="10" font-family="Helvetica" dx="0 30 30 30 30">'
                 'ab<tspan dx="0 0">cd</tspan>a</text>',
                 [('a', 10, 50)] + [(t, 10 + x, 50) for t, x in zip(['bcd', 'a'],
                                                                 _pen(10, ('a', 30), ('bcd', 30)))],
                 id='tspan list overrides its characters, parent list resumes after it'),
    pytest.param('<text x="10" y="50" font-size="10" font-family="Helvetica" dy="0 0 4">abc</text>',
                 [('ab', 10, 50), ('c', 10 + _pen(10, ('ab', 0))[0], 54)], id='vertical change'),
    pytest.param('<text x="100" y="50" font-size="10" font-family="Helvetica" text-anchor="middle" '
                 'dx="0 0 20">abc</text>',
                 [('ab', 100 - _pen(10, ('abc', 20))[0] / 2, 50),
                  ('c', 100 - _pen(10, ('abc', 20))[0] / 2 + _pen(10, ('ab', 20))[0], 50)],
                 id='middle anchor: boxes split by dx share one centred chunk'),
])
def test_text_per_character_positions(body, expected):
    s, rep = convert(svg(body, 600, 400))
    boxes = s.objects('text')
    assert texts(s) == [e[0] for e in expected]
    for el, (_, x, base) in zip(boxes, expected):
        bx, by, bw, bh, rot = box(el)
        assert el.find('.//a:pPr', NS).get('algn') == 'l' and rot == 0
        assert abs(bx - x * EMU) <= 0.01 * EMU                       # start of the box's text
        assert abs(by + BASELINE_FRAC * bh - base * EMU) <= 0.01 * EMU   # baseline
    assert rep.text_boxes == len(expected)


def _mpl_text(string, glyph_ids, transform, style='', comment=None):
    """A text as Matplotlib's SVG backend writes it with svg.fonttype 'path':
    the string in a comment, then one <use> per glyph of glyph paths in
    1/64 font units (here 64 font units = 1 glyph unit; 100 glyph units = 1 em)."""
    defs = ''.join('<path id="%s" d="M 0 0 L 3000 0 L 3000 4000 L 0 4000 z" transform="scale(0.015625)"/>' % i
                   for i in dict.fromkeys(glyph_ids))
    uses = ''.join('<use xlink:href="#%s"%s/>' % (i, ' transform="translate(%g 0)"' % (60 * k) if k else '')
                   for k, i in enumerate(glyph_ids))
    return ('<g id="text_1"><!-- %s --><g %s transform="%s"><defs>%s</defs>%s</g></g>'
            % (string if comment is None else comment, style, transform, defs, uses))


# New test: no existing test covers Matplotlib's outlined text.
@pytest.mark.parametrize('body, fonts, text, face, bold, italic, sz, rot, colour', [
    pytest.param(_mpl_text('A b', ['DejaVuSans-Bold-24', 'DejaVuSans-Bold-3', 'DejaVuSans-Bold-45'],
                           'translate(50 80) rotate(-45) scale(0.12 -0.12)', 'style="fill: #cc0000"'),
                 None, 'A b', 'DejaVu Sans', True, False, 900, 315, ('CC0000', 1.0),
                 id='rotated bold coloured, space glyph counted'),
    pytest.param(_mpl_text('Ab', ['DejaVuSans-Oblique-24', 'DejaVuSans-Oblique-45'],
                           'translate(50 80) scale(0.16 -0.16)', 'style="opacity: 0.5"'),
                 {'DejaVu Sans': 'Arial'}, 'Ab', 'Arial', False, True, 1200, 0, ('000000', 0.5),
                 id='oblique, font table, opacity'),
    pytest.param(_mpl_text('a<b&c', ['ArialMT-44', 'ArialMT-1f', 'ArialMT-45', 'ArialMT-9', 'ArialMT-46'],
                           'translate(50 80) rotate(-90) scale(0.1 -0.1)', comment='a&lt;b&amp;c'),
                 None, 'a<b&c', 'Arial', False, False, 750, 270, ('000000', 1.0),
                 id='escaped string, PostScript MT suffix'),
])
def test_matplotlib_outlined_text_becomes_text(body, fonts, text, face, bold, italic, sz, rot, colour):
    s, rep = convert(svg(body, 200, 160), fonts=fonts)
    assert texts(s) == [text] and rep.shapes == 0
    el = s.by_name('text_1')
    rpr = el.find('.//a:rPr', NS)
    assert rpr.find('a:latin', NS).get('typeface') == face
    assert (rpr.get('b') == '1', rpr.get('i') == '1') == (bold, italic)
    assert int(rpr.get('sz')) == sz                       # font size = 100 x the group's scale, px -> pt
    assert _color(rpr.find('a:solidFill', NS)) == colour
    assert abs(box(el)[4] - rot) < 1e-3
    # the translate is the left end of the baseline
    (lx, ly), (x, y, w, h) = _anchor_in_box(el, 50, 80)
    assert el.find('.//a:pPr', NS).get('algn') == 'l'
    assert abs(lx - x) <= TOL + 1 and abs(ly - (y + BASELINE_FRAC * h)) <= TOL + 1


_CAIRO_DEFS = ('<defs><g id="glyph-0-1"><path d="M0 0 L4 0 L4 -6 z"/></g>'
               '<g id="glyph-0-2"><path d="M0 0 L3 0 L3 -5 z"/></g>'
               '<symbol overflow="visible" id="glyph0-1"><path style="stroke:none;" d="M0 0 L4 0 L4 -6 z"/></symbol>'
               '<path id="dot" d="M0 0 L2 0 L2 2 z"/></defs>')


# New test: test_groups_and_use checks one glyph label among other groups;
# this one checks which groups count as one label and the merged geometry.
@pytest.mark.parametrize('body, paths, fills, bbox', [
    pytest.param('<g id="lab" fill="rgb(100%, 0%, 40%)" fill-opacity="1"><use xlink:href="#glyph-0-1" x="10" y="20"/>'
                 '<use xlink:href="#glyph-0-2" x="15" y="20"/><use xlink:href="#glyph-0-1" x="19" y="20"/></g>',
                 [3], [('FF0066', 1.0)], (10, 14, 23, 20), id='cairo label: one shape, a path per glyph'),
    pytest.param('<g id="lab" style="fill:#00ff00"><use xlink:href="#glyph0-1" x="10" y="20"/>'
                 '<use xlink:href="#glyph0-1" x="15" y="20"/></g>',
                 [2], [('00FF00', 1.0)], (10, 14, 19, 20), id='older cairo symbols'),
    pytest.param(_mpl_text('abc', ['DejaVuSans-44', 'DejaVuSans-45'], 'translate(10 20) scale(0.1 -0.1)'),
                 [2], [('000000', 1.0)], (10, 13.75, 20.6875, 20),
                 id='Matplotlib text, glyph count not the string: one shape'),
    pytest.param(_mpl_text('$x^2$', ['DejaVuSans-Oblique-5b', 'DejaVuSans-15'], 'translate(10 20) scale(0.1 -0.1)')
                 .replace('</g></g>', '<path d="M 0 -10 L 100 -10 L 100 -15 L 0 -15 z"/></g></g>'),
                 [3], [('000000', 1.0)], (10, 13.75, 20.6875, 21.5), id='Matplotlib mathtext: one shape'),
    pytest.param('<g id="lab" fill="red"><use xlink:href="#dot" x="10" y="20"/><use xlink:href="#dot" x="15" y="20"/>'
                 '</g>', [1, 1], [('FF0000', 1.0)] * 2, None, id='uses of ordinary shapes stay apart'),
    pytest.param('<g id="lab"><use xlink:href="#glyph-0-1" x="10" y="20" fill="red"/>'
                 '<use xlink:href="#glyph-0-1" x="15" y="20" fill="blue"/></g>',
                 [1, 1], [('FF0000', 1.0), ('0000FF', 1.0)], None, id='glyphs of different colours stay apart'),
])
def test_glyph_outline_labels_become_one_shape(body, paths, fills, bbox):
    s, rep = convert(svg(_CAIRO_DEFS + body))
    shapes = s.objects('shape')
    assert texts(s) == []
    assert [len(el.findall('.//a:path', NS)) for el in shapes] == paths
    assert [fill_of(el) for el in shapes] == fills
    if bbox:
        name = 'text_1' if 'text_1' in body else 'lab'
        x, y, w, h, _ = box(s.by_name(name))
        assert all(abs(a - b * EMU) <= TOL + 1 for a, b in zip((x, y, x + w, y + h), bbox))


# --- groups, use, markers -----------------------------------------------------------

def test_groups_and_use():
    s, rep = convert(svg(
        '<defs><symbol id="sym" viewBox="0 0 10 10"><rect width="10" height="10"/>'
        '<circle cx="5" cy="5" r="2" fill="white"/></symbol>'
        '<g id="glyph-0-1"><path d="M0 0 L4 0 L4 -6 z"/></g></defs>'
        '<g id="panel"><rect width="5" height="5"/><g id="inner"><rect x="10" width="5" height="5"/>'
        '<rect x="20" width="5" height="5"/></g></g>'
        '<g id="lonely"><rect id="only" x="40" width="5" height="5"/></g>'
        '<use id="u" xlink:href="#sym" x="100" y="10" width="20" height="20"/>'
        '<g id="word" style="fill: #ff0000"><use xlink:href="#glyph-0-1" x="50" y="80"/>'
        '<use xlink:href="#glyph-0-1" x="55" y="80"/></g>'))
    panel = s.by_name('panel')
    assert panel.tag.endswith('grpSp')
    assert len(s.objects(root=panel)) == 4                    # rect, group inner, 2 rects
    with pytest.raises(KeyError):
        s.by_name('lonely')                                   # single-child group collapsed
    assert s.by_name('only').tag.endswith('sp')
    u = s.by_name('u')
    kids = s.objects('shape', u)
    assert len(kids) == 2
    x, y, w, h, _ = box(kids[0])
    assert (abs(x - 100 * EMU) <= 1, abs(w - 20 * EMU) <= 1) == (True, True)
    word = s.by_name('word')                                  # the two glyph uses: one shape
    assert word.tag.endswith('sp') and len(word.findall('.//a:path', NS)) == 2
    assert fill_of(word) == ('FF0000', 1.0)                   # glyph uses inherit the use's style
    assert rep.groups == 3                                    # panel, inner, u ('word' is now one shape)


@pytest.mark.parametrize('copies, shapes', [
    pytest.param(2000, 2000, id='2000 copies: one shape each'),
    pytest.param(2001, 1, id='2001 copies: one shape for the series'),
])
def test_repeated_markers(copies, shapes):
    uses = ''.join('<use xlink:href="#m" x="%d" y="%d" style="fill: #1f77b4"/>' % (i % 100, i // 100)
                   for i in range(copies))
    s, rep = convert(svg('<defs><path id="m" d="M -1 0 A 1 1 0 1 0 1 0 A 1 1 0 1 0 -1 0"/></defs>'
                         '<g id="pts">%s</g>' % uses, 120, 40))
    assert rep.shapes == shapes
    one = s.objects('shape')[0]
    assert fill_of(one) == ('1F77B4', 1.0)
    if shapes == 1:
        assert len(one.findall('.//a:path', NS)) == copies   # each point still its own outline


# New test, not a widened one: test_repeated_markers checks the series
# rule for <use> copies; this checks merge= and merge_scope= on any shapes.
_DOT = '<circle cx="%g" cy="%g" r="3" fill="#1f77b4"/>'
_CLOUD = ''.join(_DOT % (20 + 4 * i, 50) for i in range(3))           # three dots touching in a row
_LEGEND = _DOT % (180, 10)                                              # same look, far away
_BARS = ''.join('<rect x="%d" y="60" width="10" height="20" fill="#0072b2"/>'
                '<rect x="%d" y="40" width="10" height="20" fill="#e69f00"/>' % (10 * i, 10 * i)
                for i in range(4))                                      # Gviz-like: colours alternate


@pytest.mark.parametrize('body, kw, n_shapes, outlines', [
    pytest.param('<g id="data">%s</g><g id="legend">%s</g>' % (_CLOUD, _LEGEND), {'merge': 0},
                 2, [3, 1], id='group: legend key in its own group stays apart'),
    pytest.param('<g id="data">%s</g><g id="legend">%s</g>' % (_CLOUD, _LEGEND),
                 {'merge': 0, 'merge_scope': 'figure'}, 1, [4], id='figure: across groups'),
    pytest.param(_CLOUD + _LEGEND, {'merge': 0}, 1, [4], id='group: flat file merges the legend key too'),
    pytest.param(_CLOUD + _LEGEND, {'merge': 0, 'merge_scope': 'touching'}, 2, [3, 1],
                 id='touching: isolated legend key stays apart in a flat file'),
    pytest.param(_CLOUD + _LEGEND, {'merge': 3}, 1, [4], id='set of 4 > merge=3: merged'),
    pytest.param(_CLOUD + _LEGEND, {'merge': 4}, 4, [1, 1, 1, 1], id='set of 4, merge=4: kept'),
    pytest.param(_CLOUD + _LEGEND, {'merge': None}, 4, [1, 1, 1, 1], id='merge=None: nothing merged'),
    pytest.param(_BARS, {'merge': 0}, 2, [4, 4], id='alternating bars: one shape per colour'),
    pytest.param(_BARS, {'merge': 0, 'merge_scope': 'touching'}, 2, [4, 4], id='touching bars too'),
    pytest.param('<rect x="0" y="0" width="20" height="20" fill="red"/>'
                 '<rect x="30" y="0" width="20" height="20" fill="blue"/>'
                 '<rect x="40" y="0" width="20" height="20" fill="red"/>', {'merge': 0, 'merge_scope': 'figure'},
                 3, [1, 1, 1], id='a different shape drawn between and overlapping blocks the merge'),
    pytest.param('<rect x="0" y="0" width="20" height="20" fill="red"/>'
                 '<rect x="30" y="0" width="20" height="20" fill="blue"/>'
                 '<rect x="70" y="0" width="20" height="20" fill="red"/>', {'merge': 0},
                 2, [2, 1], id='a different shape drawn between but apart does not'),
    pytest.param('<rect x="0" y="0" width="20" height="20" fill="red" fill-opacity="0.5"/>'
                 '<rect x="10" y="0" width="20" height="20" fill="red" fill-opacity="0.5"/>'
                 '<rect x="40" y="0" width="20" height="20" fill="red" fill-opacity="0.5"/>', {'merge': 0},
                 2, [2, 1], id='translucent shapes merge only where they do not overlap'),
])
def test_merge_identical_shapes(body, kw, n_shapes, outlines):
    s, rep = convert(svg(body, 200, 100), **kw)
    shapes = s.objects('shape')
    assert rep.shapes == len(shapes) == n_shapes
    # merged shapes keep one outline per member and the members' look
    got = sorted(len(el.findall('.//a:path', NS)) or 1 for el in shapes)
    assert got == sorted(outlines)
    assert rep.merged == sum(outlines) - len(outlines)
    for el in shapes:
        if len(el.findall('.//a:path', NS)) > 1:
            assert fill_of(el)[0] in ('1F77B4', '0072B2', 'E69F00', 'FF0000')
    with pytest.raises(ValueError):
        convert(svg(body), merge=-1)
    with pytest.raises(ValueError):
        convert(svg(body), merge_scope='page')


# New test, not a widened one: no existing test measures written outline
# coordinates against a grid; this one checks precision= on .pptx, SVG and CLI.
def test_precision_rounds_outlines_to_the_grid(tmp_path):
    import random
    rnd = random.Random(5)
    dots = ''.join('<path d="M %.4f %.4f c 1.3 0 2.1 0.7 2.1 2.1 s -0.8 2.1 -2.1 2.1 z" fill="#9e9e9e"/>'
                   % (rnd.uniform(5, 380), rnd.uniform(5, 180)) for _ in range(400))
    body = ('<path id="p" d="M 10.123 20.456 L 150.789 30.012 L 90.555 97.333 Z" fill="red"/>'
            '<path id="tiny" d="M 50 50 L 50.0004 50 L 50.0004 50.0004 Z" fill="blue"/>' + dots)
    doc = svg(body, 400, 200)
    exact, rep0 = convert(doc, merge=0)
    assert convert(doc, merge=0, precision=None)[0].data == exact.data          # None: today's bytes
    sizes = {}
    for prec in (0.01, 0.1):
        s, rep = convert(doc, merge=0, precision=prec)
        sizes[prec] = len(s.data)
        assert (rep.shapes, rep.merged) == (rep0.shapes, rep0.merged)
        grid = prec * PT
        el = s.by_name('p')
        x, y, w, h, _ = box(el)
        assert box(el)[:4] == box(exact.by_name('p'))[:4]                       # position and size exact
        path = el.find('.//a:path', NS)
        pw, ph = int(path.get('w')), int(path.get('h'))
        assert abs(pw - w / grid) <= 1 and abs(ph - h / grid) <= 1              # one path unit per grid step
        pts = [(x + int(pt.get('x')) * w / pw, y + int(pt.get('y')) * h / ph) for pt in path.iter('{%s}pt' % NS['a'])]
        want = [(10.123, 20.456), (150.789, 30.012), (90.555, 97.333)]
        for (gx, gy), (wx, wy) in zip(pts, want):
            assert abs(gx - wx * EMU) <= grid and abs(gy - wy * EMU) <= grid
        tiny = s.by_name('tiny').find('.//a:path', NS)
        assert int(tiny.get('w')) >= 1 and int(tiny.get('h')) >= 1             # never collapses to nothing
        out = tmp_path / ('p%s.svg' % prec)
        svgslide.convert(doc, out, merge=0, precision=prec)
        d = ET.parse(out).getroot().find('.//*[@fill="#ff0000"]').get('d')
        nums = re.findall(r'-?\d+(?:\.\d+)?', d)
        assert all(len(n.partition('.')[2]) <= (2 if prec == 0.01 else 1) for n in nums)
        assert all(abs(float(a) - b) <= prec * 4 / 3 for a, b in zip(nums, [10.123, 20.456, 150.789, 30.012]))
    assert sizes[0.1] < sizes[0.01] < len(exact.data)                           # merged scatter gets smaller
    for bad in (0, -1, float('nan'), float('inf'), True, '0.1'):
        with pytest.raises(ValueError):
            convert(doc, precision=bad)
    from svgslide.__main__ import main
    src = tmp_path / 'dots.svg'
    src.write_bytes(doc.encode())
    assert main([str(src), '--merge', '0', '--precision', '0.1']) == 0
    assert len((tmp_path / 'dots.pptx').read_bytes()) == sizes[0.1]
    for bad in ('0', '-1', 'nan', 'fine'):
        assert main([str(src), '--precision', bad]) == 2


def test_svg_markers_are_drawn_oriented():
    s, rep = convert(svg(
        '<defs><marker id="arrow" orient="auto" markerWidth="4" markerHeight="4" refX="0" refY="2" '
        'markerUnits="strokeWidth"><path id="head" d="M0 0 L4 2 L0 4 z" fill="red"/></marker></defs>'
        '<path id="p" d="M10 50 L10 10" stroke="black" stroke-width="2" marker-end="url(#arrow)"/>'))
    assert rep.shapes == 2
    head = s.by_name('head')
    x, y, w, h, _ = box(head)
    # pointing up at the end point (10, 10): 8 px long upwards, 8 px wide
    assert abs(x - 6 * EMU) <= TOL + 1 and abs(y - 2 * EMU) <= TOL + 1
    assert abs(w - 8 * EMU) <= TOL + 1 and abs(h - 8 * EMU) <= TOL + 1
    assert fill_of(head) == ('FF0000', 1.0)


# --- images ---------------------------------------------------------------------------

RED, BLUE = (255, 0, 0), (0, 0, 255)


@pytest.mark.parametrize('attrs, expected_box, top_row', [
    pytest.param('x="10" y="20" width="40" height="80"', (10, 20, 40, 80), RED, id='placed'),
    pytest.param('x="10" y="-100" width="40" height="80" transform="scale(1 -1)"', (10, 20, 40, 80), BLUE,
                 id='flipped (matplotlib imshow) stored upright'),
    pytest.param('x="10" y="20" width="80" height="80"', (30, 20, 40, 80), RED, id='meet keeps aspect'),
])
def test_embedded_image(attrs, expected_box, top_row):
    data = png([[RED], [BLUE]])
    s, rep = convert(svg('<image id="i" %s xlink:href="%s"/>' % (attrs, data_uri(data)), 200, 200))
    pic = s.by_name('i')
    x, y, w, h, _ = box(pic)
    for got, want in zip((x, y, w, h), expected_box):
        assert abs(got - want * EMU) <= TOL + 1
    xf = pic.find('p:spPr/a:xfrm', NS)
    rows = decode_png(s.media(pic))
    shown_top = rows[-1][0] if xf.get('flipV') == '1' else rows[0][0]
    assert shown_top == top_row
    assert rep.pictures == 1 and rep.skipped == []


def test_clipped_image_is_cropped():
    data = png([[RED] * 4] * 4)
    s, _ = convert(svg('<clipPath id="c"><rect x="0" y="0" width="30" height="100"/></clipPath>'
                       '<image id="i" clip-path="url(#c)" x="10" y="10" width="40" height="40" '
                       'xlink:href="%s"/>' % data_uri(data)))
    pic = s.by_name('i')
    x, y, w, h, _ = box(pic)
    assert abs(x - 10 * EMU) <= 1 and abs(w - 20 * EMU) <= 1
    assert int(pic.find('.//a:srcRect', NS).get('r')) == 50000


# --- report ----------------------------------------------------------------------------

@pytest.mark.parametrize('body, feature, element', [
    pytest.param('<filter id="f"><feGaussianBlur stdDeviation="2"/></filter>'
                 '<rect id="e" filter="url(#f)" width="5" height="5"/>', 'filter', '#e', id='filter'),
    pytest.param('<mask id="k"><rect width="5" height="5" fill="white"/></mask>'
                 '<rect id="e" mask="url(#k)" width="5" height="5"/>', 'mask', '#e', id='mask'),
    pytest.param('<pattern id="p" width="2" height="2"><rect width="1" height="1"/></pattern>'
                 '<rect id="e" fill="url(#p)" width="5" height="5"/>', 'pattern fill', '#e', id='pattern'),
    pytest.param('<flowRoot xmlns="http://www.w3.org/2000/svg" id="e"/>', 'flowed text (Inkscape flowRoot)',
                 '#e', id='flowRoot'),
    pytest.param('<rect width="5" height="5"><animate id="e" attributeName="x" to="10"/></rect>',
                 'animation', '#e', id='animation'),
    pytest.param('<image id="e" width="5" height="5" xlink:href="picture.png"/>',
                 'external or unreadable image', '#e', id='external image'),
])
def test_skipped_features_are_reported(body, feature, element):
    _, rep = convert(svg(body))
    assert (feature, element) in rep.skipped


# --- PDF input ----------------------------------------------------------------------------
# New tests, not widened ones: the tests above feed SVG markup; these feed
# hand-written PDF files, which need pypdfium2 (pip install svgslide[pdf]).

try:
    import pypdfium2  # noqa: F401
    HAVE_PDFIUM = True
except ImportError:
    HAVE_PDFIUM = False
needs_pdfium = pytest.mark.skipif(not HAVE_PDFIUM, reason='pypdfium2 is not installed (pip install svgslide[pdf])')
PT = 12700          # EMU per PDF point


def pdf(pages):
    """A PDF file whose pages are (width, height, content stream) in points.
    Resources on every page: fonts /F1 Helvetica and /F2 Helvetica-Bold,
    /G1 fill alpha 0.5 and stroke alpha 0.25, /Im1 a 2x1 image (red, blue),
    /Im2 the same with a soft mask (opaque, transparent), /Sh1 an axial shading."""
    objs = []

    def add(b):
        objs.append(b)
        return len(objs)
    f1 = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    f2 = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    pixels = zlib.compress(bytes(RED + BLUE))
    im = add(b'<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 '
             b'/Filter /FlateDecode /Length %d >>\nstream\n' % len(pixels) + pixels + b'\nendstream')
    mask = add(b'<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceGray '
               b'/BitsPerComponent 8 /Length 2 >>\nstream\n\xff\x00\nendstream')
    im2 = add(b'<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 '
              b'/SMask %d 0 R /Filter /FlateDecode /Length %d >>\nstream\n' % (mask, len(pixels)) + pixels +
              b'\nendstream')
    gs = add(b'<< /Type /ExtGState /ca 0.5 /CA 0.25 >>')
    sh = add(b'<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 100 0] /Function << /FunctionType 2 '
             b'/Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>')
    res = (b'<< /Font << /F1 %d 0 R /F2 %d 0 R >> /XObject << /Im1 %d 0 R /Im2 %d 0 R >> '
           b'/ExtGState << /G1 %d 0 R >> /Shading << /Sh1 %d 0 R >> >>' % (f1, f2, im, im2, gs, sh))
    parent = len(objs) + 2 * len(pages) + 1
    kids = []
    for w, h, content in pages:
        c = content.encode('latin-1')
        cid = add(b'<< /Length %d >>\nstream\n' % len(c) + c + b'\nendstream')
        kids.append(add(b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %g %g] /Resources %s /Contents %d 0 R >>'
                        % (parent, w, h, res, cid)))
    add(b'<< /Type /Pages /Kids [%s] /Count %d >>' % (b' '.join(b'%d 0 R' % k for k in kids), len(kids)))
    root = add(b'<< /Type /Catalog /Pages %d 0 R >>' % parent)
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % o for o in offsets)
    out += b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, root, xref)
    return bytes(out)


_PDF_PAGE = (
    'BT /F1 10 Tf 2 0 0 2 20 150 Tm (Hello) Tj ET '                        # 20 pt text, baseline y=150
    'BT /F1 12 Tf 1 0 0 1 20 100 Tm [(1) -3000 (2)] TJ ET '                 # one string, two tick labels
    'BT /F1 12 Tf 1 0 0 1 120 100 Tm [(A) 120 (V)] TJ ET '                  # kerning: one box
    'BT /F2 12 Tf 0 1 -1 0 290 20 Tm (Up) Tj ET '                           # bold, turned to read upwards
    'q /G1 gs 1 0 0 RG 2 w [6 3] 0 d 1 J 1 j 20 20 m 120 20 l S Q '        # dashed, round caps, alpha
    '0 0.5 0 rg 150 20 m 250 20 l 250 80 l 150 80 l h 170 40 m 230 40 l 230 60 l 170 60 l h f* '
    'q 0 0 100 200 re W n 0 0 1 rg 50 120 100 30 re f Q '                   # clipped at x = 100
    'q 40 0 0 20 200 150 cm /Im1 Do Q '                                     # image 40 x 20 at (200, 30)
    'q 40 0 0 20 200 100 cm /Im2 Do Q')                                     # soft-masked, at (200, 80)


@needs_pdfium
def test_pdf_page_becomes_editable_objects(tmp_path):
    data = b'\xef\xbb\xbf \r\n' + pdf([(300, 200, _PDF_PAGE)])
    path = tmp_path / 'drawing.svg'            # recognised by content, not by name
    path.write_bytes(data)
    s, rep = convert(path, fonts={'Helvetica': 'Arial'})
    assert s.size == (300 * PT, 200 * PT) and rep.slide_size == s.size
    assert s.data == convert(data, fonts={'Helvetica': 'Arial'})[0].data   # path or bytes: same bytes
    # text: one box per run the PDF sets in one go, at the PDF's baseline start
    boxes = {''.join(t.text for t in el.iter('{%s}t' % NS['a'])): el for el in s.objects('text')}
    assert sorted(boxes) == ['1', '2', 'AV', 'Hello', 'Up']
    for text, x, base, size in [('Hello', 20, 50, 20), ('1', 20, 100, 12),
                                ('2', 20 + 0.556 * 12 + 36, 100, 12), ('AV', 120, 100, 12)]:
        bx, by, bw, bh, rot = box(boxes[text])
        rpr = boxes[text].find('.//a:rPr', NS)
        assert int(rpr.get('sz')) == size * 100 and rot == 0, text
        assert rpr.find('a:latin', NS).get('typeface') == 'Arial' and rpr.get('b') is None, text
        assert abs(bx - x * PT) <= 0.05 * PT, text
        assert abs(by + BASELINE_FRAC * bh - base * PT) <= 0.05 * PT, text
    up = boxes['Up']
    assert box(up)[4] == 270 and up.find('.//a:rPr', NS).get('b') == '1'
    # shapes: colours, alpha, dash, caps, the even-odd hole and the clip
    shapes = s.objects('shape')
    assert len(shapes) == 3 and rep.shapes == 3
    dashed = [el for el in shapes if line_of(el) is not None][0]
    ln = line_of(dashed)
    colour, alpha = _color(ln.find('a:solidFill', NS))
    assert colour == 'FF0000' and abs(alpha - 0.25) <= 1 / 255     # PDFium keeps alpha in 8 bits
    assert abs(int(ln.get('w')) - 2 * PT) <= 1 and ln.get('cap') == 'rnd'
    assert [(int(d.get('d')), int(d.get('sp'))) for d in ln.iter('{%s}ds' % NS['a'])] == [(300000, 150000)]
    filled = sorted(((box(el)[:4], fill_of(el), el) for el in shapes if line_of(el) is None), key=lambda f: f[0])
    (clip_box, clip_fill, _), (donut_box, donut_fill, donut) = filled
    assert clip_fill == ('0000FF', 1.0)
    assert all(abs(g - w * PT) <= 1 for g, w in zip(clip_box, (50, 50, 50, 30)))
    assert donut_fill in (('007F00', 1.0), ('008000', 1.0))
    assert all(abs(g - w * PT) <= 1 for g, w in zip(donut_box, (150, 120, 100, 60)))
    assert len(list(donut.iter('{%s}moveTo' % NS['a']))) == 2           # the hole is kept
    # images: placed by their matrix, first row on top, soft mask kept as alpha
    pic, masked = sorted(s.objects('picture'), key=lambda el: box(el)[1])
    assert all(abs(g - w * PT) <= 1 for g, w in zip(box(pic)[:4], (200, 30, 40, 20)))
    assert all(abs(g - w * PT) <= 1 for g, w in zip(box(masked)[:4], (200, 80, 40, 20)))
    assert decode_png(s.media(pic))[0] == [RED, BLUE]
    assert decode_png(s.media(masked))[0] == [RED, (255, 255, 255)]     # transparent: white behind
    assert rep.skipped == []


@needs_pdfium
@pytest.mark.parametrize('pages, expected', [
    pytest.param(None, [1, 2, 3], id='all pages'),
    pytest.param(2, [2], id='one page'),
    pytest.param([3, 1], [3, 1], id='chosen order'),
])
def test_pdf_pages_become_slides(pages, expected):
    sizes = {1: (300, 200), 2: (150, 200), 3: (600, 100)}
    # each page: a rectangle filling the page, then a shading on page 2
    data = pdf([(w, h, '1 0 0 rg 0 0 %d %d re f%s' % (w, h, ' /Sh1 sh' if n == 2 else ''))
                for n, (w, h) in sorted(sizes.items())])
    s, rep = convert(data, pages=pages)
    names = s.zip.namelist()
    assert sorted(n for n in names if n.startswith('ppt/slides/slide')) == sorted(
        'ppt/slides/slide%d.xml' % k for k in range(1, len(expected) + 1))
    for rels in [n for n in names if n.endswith('.rels')]:      # every relationship target exists
        folder = rels.replace('_rels/', '').rsplit('.rels', 1)[0].rpartition('/')[0]
        for r in ET.fromstring(s.zip.read(rels)):
            assert os.path.normpath(os.path.join(folder, r.get('Target'))).replace(os.sep, '/') in names
    first = sizes[expected[0]]
    assert s.size == rep.slide_size == (first[0] * PT, first[1] * PT)
    for k, n in enumerate(expected, 1):
        rect, = Slide(s.data, k).objects('shape')
        w, h = sizes[n]
        scale = min(first[0] / w, first[1] / h)                  # fitted and centred
        want = ((first[0] - w * scale) / 2, (first[1] - h * scale) / 2, w * scale, h * scale)
        assert all(abs(g - v * PT) <= 2 for g, v in zip(box(rect)[:4], want)), n
    assert rep.shapes == len(expected)
    assert rep.skipped == ([('shading (smooth colour gradient)', 'page 2 object 2')] if 2 in expected else [])
    assert convert(data, pages=pages)[0].data == s.data


# New test, not a widened one: the tests above convert one source; this one a
# list of sources into one deck (and PDF pages on a canvas, which keep their size).
@pytest.mark.parametrize('kind', ['svg', pytest.param('pdf', marks=needs_pdfium)])
def test_several_sources_make_one_deck(kind, tmp_path):
    a = tmp_path / 'a.svg'
    a.write_bytes(svg('<filter id="f"/><rect id="r" width="200" height="100" filter="url(#f)"/>', 200, 100).encode())
    sources = [a, svg('<rect width="100" height="300"/>', 100, 300)]          # a path, then the SVG as text
    sizes = [(200, 100), (100, 300)]                                         # px, one per slide
    skipped = [('filter', 'a.svg #r')]
    if kind == 'pdf':      # two pages of 150 x 75 pt and 300 x 60 pt (200 x 100 px and 400 x 80 px)
        sources.append(pdf([(150, 75, '0 0 150 75 re f /Sh1 sh'), (300, 60, '0 0 300 60 re f')]))
        sizes += [(200, 100), (400, 80)]
        skipped.append(('shading (smooth colour gradient)', 'source 3 page 1 object 2'))
    s, rep = convert(sources)
    canvas = (max(w for w, _ in sizes) * EMU, max(h for _, h in sizes) * EMU)   # widest x tallest
    assert s.size == rep.slide_size == canvas
    assert sum(n.startswith('ppt/slides/slide') for n in s.zip.namelist()) == len(sizes)
    for n, (w, h) in enumerate(sizes, 1):                                     # own size, centred
        rect, = Slide(s.data, n).objects('shape')
        want = ((canvas[0] - w * EMU) / 2, (canvas[1] - h * EMU) / 2, w * EMU, h * EMU)
        assert all(abs(g - v) <= 2 for g, v in zip(box(rect)[:4], want)), n
    assert rep.shapes == len(sizes) and rep.skipped == skipped and rep.notes == []
    # on a canvas, a figure larger than it is shrunk; the note names the source and page
    s, rep = convert(sources, canvas_size='4in x 3in')                        # 384 x 288 px
    assert s.size == (4 * 914400, 3 * 914400)
    assert rep.notes == ['source 2: scaled to 96% to fit the canvas'] + (
        ['source 3 page 2: scaled to 96% to fit the canvas'] if kind == 'pdf' else [])
    if kind == 'pdf':      # one PDF on a canvas: each page keeps its own size
        s, rep = convert(sources[2], canvas_size='widescreen')
        for n, (w, h) in enumerate(sizes[2:], 1):
            rect, = Slide(s.data, n).objects('shape')
            want = ((12192000 - w * EMU) / 2, (6858000 - h * EMU) / 2, w * EMU, h * EMU)
            assert all(abs(g - v) <= 2 for g, v in zip(box(rect)[:4], want)), n
        assert rep.skipped == [('shading (smooth colour gradient)', 'page 1 object 2')]


def test_pdf_errors(monkeypatch):
    if HAVE_PDFIUM:
        data = pdf([(100, 100, '0 0 10 10 re f')])
        for bad in (2, [1, 0], [], 'all', True):
            with pytest.raises(ValueError, match='page'):
                convert(data, pages=bad)
    with pytest.raises(ValueError, match='PDF'):
        convert(svg('<rect width="5" height="5"/>'), pages=1)
    # without pypdfium2: a clear error naming the install command
    monkeypatch.setitem(sys.modules, 'pypdfium2', None)
    monkeypatch.setitem(sys.modules, 'pypdfium2.raw', None)
    with pytest.raises(ImportError, match=re.escape('pip install svgslide[pdf]')):
        convert(b'%PDF-1.4\n%%EOF\n')


# New test, not a widened one: test_text_font checks one text's typeface;
# this checks the fonts the report lists, across inputs, outputs and the CLI.
@pytest.mark.parametrize('kind', ['svg', pytest.param('pdf', marks=needs_pdfium)])
def test_report_lists_fonts_and_fonts_replace_all(kind, tmp_path, capsys):
    if kind == 'svg':
        src = svg('<text x="10" y="20" font-family="DejaVu Sans">Sample A</text>'
                  '<text x="10" y="40" font-family="Symbol">a</text>'
                  '<text x="10" y="60" font-family="sans-serif">b</text>'
                  '<text x="10" y="80" font-family="DejaVu Sans">c</text>' + _CAIRO_DEFS +
                  '<g fill="red"><use xlink:href="#glyph-0-1" x="100" y="20"/>'
                  '<use xlink:href="#glyph-0-2" x="105" y="20"/></g>', 200, 100)   # glyph label: no font
        found = ['DejaVu Sans', 'Symbol', 'sans-serif']
    else:
        src = pdf([(200, 100, 'BT /F2 10 Tf 10 80 Td (Sample A) Tj ET BT /F1 10 Tf 10 60 Td (b) Tj ET')])
        found = ['Helvetica']                      # Helvetica-Bold: family Helvetica, bold
    s, rep = convert(src)
    assert rep.fonts == found
    s, rep = convert(src, fonts={'*': 'Arial', 'symbol': 'Symbol'})
    faces = {el.get('typeface') for el in s.tree.iter('{%s}latin' % NS['a'])}
    assert faces == ({'Arial', 'Symbol'} if kind == 'svg' else {'Arial'}) and rep.fonts == found
    out = tmp_path / 'f.svg'
    svgslide.convert(src, out, fonts='Calibri')
    assert {t.get('font-family') for t in ET.parse(out).getroot().iter(SVG_NS + 'tspan')} == {'Calibri'}
    with pytest.raises(ValueError):
        convert(src, fonts=['Arial'])
    from svgslide.__main__ import main
    f = tmp_path / ('in.' + kind)
    f.write_bytes(src if isinstance(src, bytes) else src.encode())
    assert main([str(f), '--font', '%s=Arial' % found[0]]) == 0
    err = capsys.readouterr().err
    assert 'fonts in %s: %s -> Arial' % (f, found[0]) in err


# --- Matplotlib figures -------------------------------------------------------------------

class FakeFigure:
    """Anything with Matplotlib's savefig works."""

    def __init__(self):
        self.calls = []

    def savefig(self, fname, **kw):
        self.calls.append(kw)
        fname.write(svg('<text x="10" y="50" font-family="DejaVu Sans" font-size="12">Sample A</text>').encode())


@pytest.mark.parametrize('kind', ['fake', 'matplotlib'])
def test_convert_figure_keeps_text_as_text(kind, tmp_path):
    if kind == 'fake':
        fig = FakeFigure()
        want = ['Sample A']
    else:
        mpl = pytest.importorskip('matplotlib', reason='Matplotlib is not installed')
        mpl.use('agg')
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(4, 3), dpi=50)
        ax.plot([0, 1], [0, 1])
        ax.set_title('Sample A')
        ax.set_xlabel('GENE1')
        want = ['Sample A', 'GENE1']
    out = tmp_path / 'figure.pptx'
    rep = svgslide.convert_figure(fig, out, fonts={'DejaVu Sans': 'Arial'})
    s = Slide(out.read_bytes())
    found = texts(s)
    assert all(w in found for w in want) and rep.text_boxes == len(found)
    faces = {el.get('typeface') for el in s.tree.iter('{%s}latin' % NS['a'])}
    assert 'Arial' in faces and 'DejaVu Sans' not in faces
    if kind == 'fake':
        assert fig.calls == [{'format': 'svg', 'dpi': 'figure'}]
    else:
        # real text boxes, not letter outlines: about one box per tick label and label
        assert rep.text_boxes < 30 and rep.shapes < 30
        assert s.size == (4 * 914400, 3 * 914400)
        import matplotlib
        assert matplotlib.rcParams['svg.fonttype'] == 'path'   # the caller's setting is left alone
        plt.close(fig)


# --- command line ---------------------------------------------------------------------------

def test_command_line(tmp_path, capsys):
    from svgslide.__main__ import main
    a, b = tmp_path / 'a.svg', tmp_path / 'b.svgz'
    a.write_bytes(svg('<text x="10" y="50" font-family="DejaVu Sans">Sample A</text>').encode())
    import gzip
    b.write_bytes(gzip.compress(svg('<rect width="5" height="5"/>').encode()))
    bad = tmp_path / 'bad.svg'
    bad.write_bytes(b'not a drawing')
    (tmp_path / 'b.pptx').write_bytes(b'old')                       # an existing output is overwritten
    assert main([str(a), str(b), '--font', 'DejaVu Sans=Arial']) == 0
    out = capsys.readouterr()
    assert Slide((tmp_path / 'a.pptx').read_bytes()).tree.find('.//a:latin', NS).get('typeface') == 'Arial'
    assert Slide((tmp_path / 'b.pptx').read_bytes()).objects('shape')
    assert len(out.out.splitlines()) == 2 and '1 text box' in out.out and '1 shape,' in out.out
    # a bad file fails (status 1), the good one is still converted
    (tmp_path / 'a.pptx').unlink()
    assert main([str(bad), str(a)]) == 1
    assert (tmp_path / 'a.pptx').exists() and not (tmp_path / 'bad.pptx').exists()
    assert 'bad.svg' in capsys.readouterr().err
    # -o names the output; never an input
    assert main([str(a), '-o', str(tmp_path / 'other.pptx')]) == 0 and (tmp_path / 'other.pptx').exists()
    assert main([str(a), '-o', str(a)]) == 1 and a.read_bytes().startswith(b'<svg')
    capsys.readouterr()
    # a bare --font NAME replaces every font
    assert main([str(a), '-o', str(tmp_path / 'all.pptx'), '--font', 'Times New Roman']) == 0
    assert Slide((tmp_path / 'all.pptx').read_bytes()).tree.find(
        './/a:latin', NS).get('typeface') == 'Times New Roman'
    capsys.readouterr()
    # several inputs with -o NAME.pptx: one deck, one stdout line; never into an input, never as SVG
    deck = tmp_path / 'deck.pptx'
    assert main([str(a), str(b), '-o', str(deck)]) == 0
    assert sum(n.startswith('ppt/slides/slide') for n in Slide(deck.read_bytes()).zip.namelist()) == 2
    assert capsys.readouterr().out.splitlines() == ['%s, %s -> %s: 1 shape, 1 text box, 0 pictures, 0 groups, '
                                                    '0 merged, 0 skipped' % (a, b, deck)]
    assert main([str(a), str(b), '-o', str(b)]) == 1 and b.read_bytes().startswith(b'\x1f\x8b')
    assert main([str(a), str(b), '-o', str(tmp_path / 'deck.svg')]) == 2
    assert not (tmp_path / 'deck.svg').exists()
    capsys.readouterr()
    # --version prints the program name and the package version
    assert main(['--version']) == 0
    assert capsys.readouterr().out == 'svgslide %s\n' % svgslide.__version__
    # usage errors: status 2
    for argv in ([str(a), '--font', 'Arial', '--font', 'Times'],
                 [str(a), '--pages', '3-1'], []):
        assert main(argv) == 2, argv
        assert 'usage' in capsys.readouterr().err
    # --pages applies to PDF only
    assert main([str(a), '--pages', '1']) == 1


@needs_pdfium
def test_command_line_pdf_pages(tmp_path, capsys):
    from svgslide.__main__ import main
    p = tmp_path / 'plots.pdf'
    p.write_bytes(pdf([(100, 100, '0 0 10 10 re f')] * 5))
    assert main([str(p), '--pages', '1,3-4']) == 0
    s = Slide((tmp_path / 'plots.pptx').read_bytes())
    assert sum(n.startswith('ppt/slides/slide') for n in s.zip.namelist()) == 3
    assert '3 shapes' in capsys.readouterr().out
    assert main([str(p), '--pages', '6']) == 1
    assert 'out of range' in capsys.readouterr().err


# New test: test_command_line covers inputs and outputs; this one the merge
# flags and SVG output.
def test_command_line_merge_and_svg(tmp_path, capsys):
    from svgslide.__main__ import main
    a = tmp_path / 'a.svg'
    a.write_bytes(svg(_CLOUD + _LEGEND, 200, 100).encode())
    out = tmp_path / 'a.pptx'
    for argv, shapes in ((['--merge', '0'], 1), (['--merge', '0', '--merge-scope', 'touching'], 2),
                         (['--merge', 'none'], 4), ([], 4)):
        assert main([str(a)] + argv) == 0, argv
        assert len(Slide(out.read_bytes()).objects('shape')) == shapes, argv
    assert '3 merged' in capsys.readouterr().out.splitlines()[0]
    assert main([str(a), '-o', str(tmp_path / 'b.svg'), '--merge', '0']) == 0
    assert len(ET.parse(tmp_path / 'b.svg').getroot().findall('.//%spath' % SVG_NS)) == 1
    for bad in (['--merge', '-1'], ['--merge', 'all'], ['--merge-scope', 'page']):
        assert main([str(a)] + bad) == 2, bad
        assert 'usage' in capsys.readouterr().err
    # size flags; notes go to stderr, stdout keeps one line per file
    for argv, size, note in ((['--scale', '2'], (400 * EMU, 200 * EMU), None),
                             (['--width', '10cm'], (3600000, 1800000), None),
                             (['--height', '3in'], (6 * 914400, 3 * 914400), None),
                             (['--canvas-size', 'Widescreen'], (12192000, 6858000), None),
                             (['--scale', '2', '--canvas-size', '4in x 1.5in'], (4 * 914400, 1371600),
                              'note in %s: scaled to 72%% to fit the canvas' % a)):
        assert main([str(a)] + argv) == 0, argv
        assert Slide(out.read_bytes()).size == size, argv
        res = capsys.readouterr()
        assert len(res.out.splitlines()) == 1 and (note in res.err if note else 'note' not in res.err), argv
    for bad in (['--scale', '0'], ['--scale', 'big'], ['--scale', '2', '--width', '3cm'], ['--width', '3 furlongs'],
                ['--height', '-2cm'], ['--canvas-size', 'huge'], ['--canvas-size', '100in x 3in']):
        assert main([str(a)] + bad) == 2, bad
        assert 'usage' in capsys.readouterr().err
    assert main([str(a), '--width', '10cm', '--height', '10cm']) == 1        # does not match this figure's shape
    assert 'aspect ratio' in capsys.readouterr().err


# --- optional: real-world corpus and render comparison -----------------------------------

CORPUS = os.environ.get('SVGSLIDE_CORPUS')


@pytest.mark.skipif(not CORPUS, reason='set SVGSLIDE_CORPUS to a folder of SVG files to run the corpus smoke test')
def test_corpus_smoke(tmp_path):
    files = sorted(Path(CORPUS).glob('*.svg'))
    assert files, 'no .svg files in SVGSLIDE_CORPUS'
    for f in files:
        out = tmp_path / (f.stem + '.pptx')
        rep = svgslide.convert(f, out)
        s = Slide(out.read_bytes())
        assert rep.shapes + rep.text_boxes + rep.pictures + len(rep.skipped) > 0, f.name
        assert len(s.objects()) >= 1 or rep.skipped, f.name


def _tool(name):
    import shutil
    return os.environ.get('SVGSLIDE_' + name.upper()) or shutil.which(name)


SOFFICE, INKSCAPE = _tool('soffice'), _tool('inkscape')

RENDER_CASES = {
    'shapes': svg('<rect x="10" y="10" width="80" height="40" fill="#ff8800" stroke="#000" stroke-width="3"/>'
                  '<circle cx="140" cy="50" r="30" fill="#2266cc"/>'
                  '<path d="M10 90 C60 60 120 120 190 70" fill="none" stroke="#00aa00" stroke-width="4"/>'),
    'transforms_and_groups': svg('<g transform="translate(100 50) rotate(30)" fill="#cc0044">'
                                 '<rect x="-40" y="-15" width="80" height="30"/>'
                                 '<g transform="scale(0.5)"><circle cx="0" cy="-70" r="20"/></g></g>'),
    'gradient_and_clip': svg('<defs><linearGradient id="g"><stop offset="0" stop-color="#ff0000"/>'
                             '<stop offset="1" stop-color="#0000ff"/></linearGradient>'
                             '<clipPath id="c"><rect x="20" y="20" width="160" height="60"/></clipPath></defs>'
                             '<rect clip-path="url(#c)" width="200" height="100" fill="url(#g)"/>'),
}


@pytest.mark.skipif(not (SOFFICE and INKSCAPE),
                    reason='LibreOffice (soffice) and Inkscape not found on PATH or via '
                           'SVGSLIDE_SOFFICE / SVGSLIDE_INKSCAPE')
@pytest.mark.parametrize('name', sorted(RENDER_CASES))
def test_render_matches_inkscape(name, tmp_path):
    """Render the SVG with Inkscape and the .pptx with LibreOffice at the same
    pixel size and compare.  Tolerance: mean absolute difference per channel
    below 4 (of 255), and fewer than 3 % of pixels off by more than 64.  This
    allows anti-aliasing differences along edges (a 1-pixel band around every
    outline at 400x200) but fails on a misplaced, missing or recoloured shape,
    which changes whole areas."""
    import subprocess
    w, h = 400, 200
    src = tmp_path / (name + '.svg')
    src.write_bytes(RENDER_CASES[name].encode())
    pptx = tmp_path / (name + '.pptx')
    svgslide.convert(src, pptx)
    ref = tmp_path / 'ref.png'
    subprocess.run([INKSCAPE, str(src), '--export-type=png', '--export-filename=' + str(ref),
                    '--export-width=%d' % w, '--export-height=%d' % h, '--export-background=white',
                    '--export-background-opacity=1'], check=True, capture_output=True, timeout=300)
    profile = (tmp_path / 'lo_profile').as_uri()
    opts = 'png:impress_png_Export:{"PixelWidth":{"type":"long","value":"%d"},' \
           '"PixelHeight":{"type":"long","value":"%d"}}' % (w, h)
    subprocess.run([SOFFICE, '-env:UserInstallation=' + profile, '--headless', '--convert-to', opts,
                    '--outdir', str(tmp_path), str(pptx)], check=True, capture_output=True, timeout=300)
    a = decode_png(ref.read_bytes())
    b = decode_png((tmp_path / (name + '.png')).read_bytes())
    assert (len(a), len(a[0])) == (len(b), len(b[0])) == (h, w)
    total = big = 0
    for ra, rb in zip(a, b):
        for pa, pb in zip(ra, rb):
            d = sum(abs(x - y) for x, y in zip(pa, pb))
            total += d
            big += max(abs(x - y) for x, y in zip(pa, pb)) > 64
    mean = total / (w * h * 3)
    assert mean < 4, mean
    assert big < 0.03 * w * h, big


# --- SVG output ---------------------------------------------------------------------------
# New tests: the tests above read the .pptx; these read the SVG file written
# instead when the destination ends in .svg or format="svg".

SVG_NS = '{http://www.w3.org/2000/svg}'


def test_svg_output(tmp_path):
    body = (_CLOUD + _LEGEND +
            '<linearGradient id="g"><stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#0000ff"/>'
            '</linearGradient><rect x="10" y="70" width="60" height="20" fill="url(#g)"/>'
            '<g id="panel"><text x="100" y="80" font-family="DejaVu Sans" font-size="12" fill="#00aa00">'
            'Sample A</text><ellipse cx="150" cy="80" rx="10" ry="5" fill="none" stroke="#123456" '
            'stroke-width="2" stroke-dasharray="4 2"/></g>'
            '<image x="170" y="60" width="20" height="20" xlink:href="%s"/>' % data_uri(png([[RED], [BLUE]])))
    out = tmp_path / 'figure.svg'
    rep = svgslide.convert(svg(body, 200, 100), out, merge=0, merge_scope='touching',
                           fonts={'DejaVu Sans': 'Arial'})
    root = ET.fromstring(out.read_bytes())
    assert root.tag == SVG_NS + 'svg' and (root.get('width'), root.get('height')) == ('200', '100')
    paths = root.findall('.//%spath' % SVG_NS)
    merged = [p for p in paths if p.get('d').count('M') == 3]          # the three touching dots: one path
    assert len(merged) == 1 and merged[0].get('fill') == '#1f77b4' and rep.merged == 2
    assert len(paths) + len(root.findall('.//%sellipse' % SVG_NS)) + len(root.findall('.//%srect' % SVG_NS)) \
        == rep.shapes
    text, = root.iter(SVG_NS + 'text')
    span, = text
    assert span.text == 'Sample A' and span.get('font-family') == 'Arial' and span.get('fill') == '#00aa00'
    assert abs(float(text.get('x')) - 100) < 0.01 and abs(float(text.get('y')) - 80) < 0.01
    assert abs(float(span.get('font-size')) - 12) < 0.01
    grad, = root.iter(SVG_NS + 'linearGradient')
    assert [st.get('stop-color') for st in grad] == ['#ff0000', '#0000ff']
    ell, = [e for e in root.iter(SVG_NS + 'ellipse') if e.get('stroke') != 'none']
    assert (ell.get('stroke'), ell.get('stroke-width'), ell.get('stroke-dasharray')) == ('#123456', '2', '4 2')
    assert [g_.get('id') for g_ in root.iter(SVG_NS + 'g')] == ['panel']
    img, = root.iter(SVG_NS + 'image')
    href = img.get('{http://www.w3.org/1999/xlink}href')
    assert decode_png(base64.b64decode(href.split(',', 1)[1])) == [[RED], [BLUE]]
    # format= for file objects, or to override the suffix; same bytes
    buf = io.BytesIO()
    svgslide.convert(svg(body, 200, 100), buf, merge=0, merge_scope='touching', fonts={'DejaVu Sans': 'Arial'},
                     format='svg')
    assert buf.getvalue() == out.read_bytes()
    assert convert(svg(body, 200, 100))[0].zip.namelist()      # default stays .pptx
    fig = FakeFigure()
    svgslide.convert_figure(fig, tmp_path / 'fig.svg')
    assert [t.text for t in ET.parse(tmp_path / 'fig.svg').getroot().iter(SVG_NS + 'tspan')] == ['Sample A']
    # size options reach convert_figure and the SVG: the canvas is the SVG's size, the drawing centred on it
    svgslide.convert_figure(fig, tmp_path / 'big.svg', scale=2, canvas_size='widescreen')
    root = ET.parse(tmp_path / 'big.svg').getroot()
    assert (root.get('width'), root.get('height')) == ('1280', '720')              # 33.867 x 19.05 cm
    text, = root.iter(SVG_NS + 'text')
    assert abs(float(text.get('x')) - (440 + 2 * 10)) < 0.01 and abs(float(text.get('y')) - (260 + 2 * 50)) < 0.01
    assert abs(float(text[0].get('font-size')) - 24) < 0.01
    with pytest.raises(ValueError):
        svgslide.convert(svg(body), io.BytesIO(), format='pdf')


@needs_pdfium
def test_svg_output_from_pdf_is_one_page(tmp_path):
    data = pdf([(200, 100, '1 0 0 rg 0 0 10 10 re f'), (200, 100, '0 0 1 rg 0 0 30 10 re f')])
    with pytest.raises(ValueError, match='pages'):
        svgslide.convert(data, tmp_path / 'x.svg')
    svgslide.convert(data, tmp_path / 'x.svg', pages=2)
    root = ET.parse(tmp_path / 'x.svg').getroot()
    path, = root.iter(SVG_NS + 'path')
    xs = [float(v) for v in re.findall(r'[-\d.]+', path.get('d'))]
    assert path.get('fill') == '#0000ff'
    assert (min(xs[0::2]), max(xs[0::2]), min(xs[1::2]), max(xs[1::2])) == pytest.approx((0, 40, 120, 133.333), abs=0.01)


@pytest.mark.skipif(not INKSCAPE, reason='Inkscape not found on PATH or via SVGSLIDE_INKSCAPE')
@pytest.mark.parametrize('name', sorted(RENDER_CASES))
def test_svg_output_renders_like_input(name, tmp_path):
    """Inkscape renders the written SVG like the input SVG (same tolerance as
    test_render_matches_inkscape)."""
    import subprocess
    w, h = 400, 200
    src = tmp_path / (name + '.svg')
    src.write_bytes(RENDER_CASES[name].encode())
    out = tmp_path / (name + '.out.svg')
    svgslide.convert(src, out, merge=0, merge_scope='figure')
    images = []
    for f in (src, out):
        png_path = tmp_path / (f.name + '.png')
        subprocess.run([INKSCAPE, str(f), '--export-type=png', '--export-filename=' + str(png_path),
                        '--export-width=%d' % w, '--export-height=%d' % h, '--export-background=white',
                        '--export-background-opacity=1'], check=True, capture_output=True, timeout=300)
        images.append(decode_png(png_path.read_bytes()))
    a, b = images
    total = big = 0
    for ra, rb in zip(a, b):
        for pa, pb in zip(ra, rb):
            total += sum(abs(x - y) for x, y in zip(pa, pb))
            big += max(abs(x - y) for x, y in zip(pa, pb)) > 64
    assert total / (w * h * 3) < 4
    assert big < 0.03 * w * h
