"""svgslide: convert an SVG drawing or a PDF into an editable PowerPoint file.

    >>> import svgslide
    >>> report = svgslide.convert("figure.svg", "figure.pptx", fonts={"DejaVu Sans": "Arial"})
    >>> report.skipped
    []
"""

import contextlib
import gzip
import io
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

from . import _convert, _drawingml, _merge, _package

__all__ = ['convert', 'convert_figure']
__version__ = '0.2.0'


class Report:
    """What :func:`convert` wrote and what it had to leave out.

    Attributes: ``shapes``, ``text_boxes``, ``pictures``, ``groups`` (counts),
    ``slide_size`` ((width, height) in EMU) and ``skipped``: a list of
    ``(feature, element)`` pairs such as ``("filter", "#shadow")``; for a
    PDF the counts are summed over the slides and each element names its
    page, e.g. ``("shading (smooth colour gradient)", "page 2 object 14")``.
    ``merged`` is the number of shapes that went into merged shapes (a set of
    n shapes merged into one counts n - 1).  ``fonts`` lists the font names
    of the drawing's text as written (PDF subset prefixes removed), in the
    order first seen; text drawn as glyph outlines has none.  ``notes`` lists
    what the conversion changed on its own, in words, e.g. ``"scaled to 82%
    to fit the canvas"``.  For several sources each skipped element and each
    note starts with the source's file name (``"source 2"`` for data given
    in memory) and the page where there is one.
    """

    __slots__ = ('shapes', 'text_boxes', 'pictures', 'groups', 'skipped', 'slide_size', 'merged', 'fonts',
                 'notes')

    def __init__(self, counts, skipped, slide_size, merged=0, fonts=(), notes=()):
        self.notes = list(notes)
        self.merged = merged
        self.fonts = list(fonts)
        self.shapes = counts['shape']
        self.text_boxes = counts['text']
        self.pictures = counts['picture']
        self.groups = counts['group']
        self.skipped = skipped
        self.slide_size = slide_size

    def __repr__(self):
        return ('Report(shapes=%d, text_boxes=%d, pictures=%d, groups=%d, merged=%d, fonts=%r, skipped=%r, '
                'notes=%r)' % (self.shapes, self.text_boxes, self.pictures, self.groups, self.merged, self.fonts,
                               self.skipped, self.notes))


def _read_source(source):
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    elif isinstance(source, str) and source.lstrip('﻿ \t\r\n').startswith('<'):
        data = source.encode('utf-8')
        # an XML declaration naming another encoding would now be wrong
        if data.lstrip().startswith(b'<?xml'):
            end = data.find(b'?>')
            data = data[end + 2:] if end > 0 else data
    else:
        with open(os.fspath(source), 'rb') as fh:
            data = fh.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


def _is_pdf(data):
    # a PDF starts with %PDF-; readers accept a byte order mark or white
    # space before it
    return data[:1024].lstrip(b'\xef\xbb\xbf \t\r\n\f\x00').startswith(b'%PDF-')


def _check_merge(merge, merge_scope):
    if merge is not None and (isinstance(merge, bool) or not isinstance(merge, int) or merge < 0):
        raise ValueError('merge must be a whole number >= 0 or None, got %r' % (merge,))
    if merge_scope not in _merge.SCOPES:
        raise ValueError('merge_scope must be one of %s, got %r' % (', '.join(_merge.SCOPES), merge_scope))


def _check_fonts(fonts):
    if fonts is None:
        return None
    if isinstance(fonts, str):
        return {'*': fonts}
    if isinstance(fonts, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in fonts.items()):
        return dict(fonts)
    raise ValueError('fonts must be a font name, a {name: replacement} dict or None, got %r' % (fonts,))


def _check_precision(precision):
    if precision is None:
        return None
    if isinstance(precision, bool) or not isinstance(precision, (int, float)) or \
            not math.isfinite(precision) or precision <= 0:
        raise ValueError('precision must be a positive number of points or None, got %r' % (precision,))
    return float(precision)


_EMU_PER = {'pt': 12700, 'px': 9525, 'in': 914400, 'cm': 360000, 'mm': 36000}
_NUM = r'([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(cm|mm|in|pt|px)?'
_LENGTH = re.compile(r'^\s*%s\s*$' % _NUM, re.IGNORECASE)
_PAIR = re.compile(r'^\s*%s\s*[xX]\s*%s\s*$' % (_NUM, _NUM), re.IGNORECASE)
CANVAS_NAMES = {'widescreen': (12192000, 6858000), '4:3': (9144000, 6858000),
                'a4': (10692000, 7560000), 'a4-portrait': (7560000, 10692000)}


def _emu(number, unit):
    v = float(number) * _EMU_PER[(unit or 'pt').lower()]
    return v if math.isfinite(v) and v > 0 else None


def _length(value, name):
    """A length in EMU from a number of points or a string such as "20cm"."""
    v = None
    if isinstance(value, str):
        m = _LENGTH.match(value)
        v = _emu(*m.groups()) if m else None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        v = _emu(value, 'pt')
    if v is None:
        raise ValueError('%s must be a positive number of points or a string such as "20cm" (units cm, mm, in, '
                         'pt, px), got %r' % (name, value))
    return v


def _check_scale(scale, width, height):
    if scale is None:
        return None
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ValueError('scale must be a positive number or None, got %r' % (scale,))
    if width is not None or height is not None:
        raise ValueError('give scale or width/height, not both')
    return float(scale)


def _canvas(value):
    """canvas_size= -> (width, height) in whole EMU, or None."""
    if value is None:
        return None
    size = None
    if isinstance(value, str):
        size = CANVAS_NAMES.get(value.strip().lower())
        m = _PAIR.match(value) if size is None else None
        if m:
            size = (_emu(*m.groups()[:2]), _emu(*m.groups()[2:]))
    elif isinstance(value, (tuple, list)) and len(value) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        size = (_emu(value[0], 'pt'), _emu(value[1], 'pt'))
    if size is None or None in size:
        raise ValueError('canvas_size must be widescreen, 4:3, A4, A4-portrait, a string such as "25cm x 14cm" '
                         'or a (width, height) tuple in points, got %r' % (value,))
    size = (int(round(size[0])), int(round(size[1])))
    if not all(_convert.MIN_SLIDE <= v <= _convert.MAX_SLIDE for v in size):
        raise ValueError('canvas_size must be 1 to 56 inch on each side (PowerPoint\'s limits), got %r' % (value,))
    return size


def _percent(f):
    return ('%.1f' % (100 * f)).rstrip('0').rstrip('.') + '%'


def _label(source, i):
    """A source's name in reports of several sources."""
    if isinstance(source, (bytes, bytearray)) or (
            isinstance(source, str) and source.lstrip('\ufeff \t\r\n').startswith('<')):
        return 'source %d' % (i + 1)
    return os.path.basename(os.fspath(source))


def _place(sizes, wheres, scale, width, height, canvas, centre):
    """EMU per px, offsets, slide size and notes for figures of sizes [(w, h)] px."""
    notes = []
    ks = []
    for (w, h), where in zip(sizes, wheres):
        k = _drawingml.EMU_PER_PX
        if scale is not None:
            k = k * scale
        elif width is not None or height is not None:
            kw = width / w if width is not None else None
            kh = height / h if height is not None else None
            if kw is not None and kh is not None and abs(kw / kh - 1) > 0.01:
                raise ValueError('%swidth and height do not match the figure\'s aspect ratio (%g x %g pt); give '
                                 'only one of them' % (where + ': ' if where else '', w * 0.75, h * 0.75))
            k = min(v for v in (kw, kh) if v is not None)
        if canvas is not None:
            fit = min(canvas[0] / (w * k), canvas[1] / (h * k))
            reason = 'the canvas'
        else:
            fit = min(_convert.MAX_SLIDE / (w * k), _convert.MAX_SLIDE / (h * k))
            reason = "PowerPoint's 56-inch slide limit"
        if fit < (1 - 1e-9 if canvas is not None else 1):
            # the formula of earlier versions, so that their output stays the same
            k = min(_convert.MAX_SLIDE / w, _convert.MAX_SLIDE / h) if canvas is None else k * fit
            notes.append('%sscaled to %s to fit %s' % (where + ': ' if where else '', _percent(fit), reason))
        ks.append(k)
    if canvas is None:
        if not centre:      # one figure (or the pages of one PDF): the slide has the first one's size
            w, h = sizes[0]
            return ks, [None] * len(ks), (max(_convert.MIN_SLIDE, int(round(w * ks[0]))),
                                          max(_convert.MIN_SLIDE, int(round(h * ks[0])))), notes
        canvas = (max(_convert.MIN_SLIDE, max(int(round(w * k)) for (w, h), k in zip(sizes, ks))),
                  max(_convert.MIN_SLIDE, max(int(round(h * k)) for (w, h), k in zip(sizes, ks))))
    offsets = [((canvas[0] - w * k) / 2, (canvas[1] - h * k) / 2) for (w, h), k in zip(sizes, ks)]
    return ks, offsets, canvas, notes


def _format(destination, format):
    if format is None:
        name = '' if hasattr(destination, 'write') else os.fspath(destination)
        return 'svg' if str(name).lower().endswith('.svg') else 'pptx'
    if format not in ('pptx', 'svg'):
        raise ValueError('format must be "pptx" or "svg", got %r' % (format,))
    return format


def convert(source, destination, *, fonts=None, pages=None, merge=2000, merge_scope='group', format=None,
            precision=None, scale=None, width=None, height=None, canvas_size=None):
    """Convert an SVG or a PDF into a .pptx whose parts are editable shapes.

    source: a file path, or the SVG itself as ``str`` or ``bytes``, or the
        PDF as ``bytes`` (recognised by its content, not the file name;
        reading PDFs needs ``pip install svgslide[pdf]``); or a list of
        these, which gives one deck: one slide per SVG and per PDF page, in
        order, each figure at its own size and centred on a slide as wide as
        the widest and as tall as the tallest figure.
    destination: a file path or a binary file object.
    fonts: optional ``{font_name: office_font_name}`` replacement table,
        e.g. ``{"DejaVu Sans": "Arial"}`` (names matched without regard to
        case); the key ``"*"`` stands for every font not named, and a plain
        string replaces every font (``"Arial"``).  Names not in it are kept
        as written.
    pages: PDF only: the 1-based page number or list of page numbers to
        convert, e.g. ``2`` or ``[1, 3]``; default all pages.  Each page
        becomes a slide; the slides have the first page's size and other
        pages are scaled to fit and centred (with ``canvas_size``, every
        page keeps its own size).  An SVG gives one slide.  Not with a list
        of sources.
    merge: shapes that look the same (fill, line, opacity) become one shape
        (one outline per member) when a set has more than this many members;
        0 merges every set of two or more, None never merges.  Text boxes and
        pictures never merge.  A member joins only if nothing that looks
        different, drawn in between, overlaps it, so the picture is unchanged.
    merge_scope: what forms a set: ``"group"`` (shapes in one SVG group,
        the default), ``"touching"`` (shapes whose boxes overlap or touch,
        chained) or ``"figure"`` (anywhere in the drawing).
    format: ``"pptx"`` or ``"svg"`` (the converted drawing as an SVG file,
        one page only); default from the destination's suffix (``.svg``),
        else ``"pptx"``.
    precision: None (full precision), or a grid size in points, e.g. 0.01:
        the coordinates of free-form and merged shapes are rounded to that
        grid and written with fewer digits (smaller files); shape positions
        and sizes, text boxes and pictures stay exact.
    scale: None (1) or a positive factor: the whole drawing, with its text,
        line widths and pictures, is made that much larger (or smaller).
    width, height: the figure's final size instead of ``scale``: a number
        of points or a string such as ``"20cm"`` or ``"20 cm"`` (units cm,
        mm, in, pt, px; px = 1/96 inch).  Give one and the other follows the
        figure's aspect ratio; both must match it within 1%.
    canvas_size: None (the slide has the figure's size), or the slide size
        with the figure centred on it: ``"widescreen"`` (33.867 x 19.05 cm,
        PowerPoint's default), ``"4:3"`` (25.4 x 19.05 cm), ``"A4"``
        (landscape), ``"A4-portrait"``, a string such as ``"25cm x 14cm"``,
        or a ``(width, height)`` tuple in points; 1 to 56 inch on each
        side.  A figure larger than the canvas is shrunk to fit.

    Sizes above PowerPoint's 56-inch limit are shrunk to fit; the report's
    ``notes`` say so.  Returns a :class:`Report` with counts and the skipped
    features.
    """
    _check_merge(merge, merge_scope)
    fonts = _check_fonts(fonts)
    precision = _check_precision(precision)
    width = None if width is None else _length(width, 'width')
    height = None if height is None else _length(height, 'height')
    scale = _check_scale(scale, width, height)
    canvas = _canvas(canvas_size)
    grid = precision * 12700 if precision else None      # EMU
    fmt = _format(destination, format)
    deck = isinstance(source, (list, tuple))
    sources = list(source) if deck else [source]
    if deck and not sources:
        raise ValueError('source is an empty list')
    if deck and pages is not None:
        raise ValueError('pages= works with one source only, not with a list of sources')
    docs = []         # (svg root, page number or None, skipped, label or None)
    for i, src in enumerate(sources):
        label = _label(src, i) if deck else None
        data = _read_source(src)
        if _is_pdf(data):
            from . import _pdf
            docs.extend(d + (label,) for d in _pdf.read(data, pages, fit=not deck and canvas is None))
        else:
            if pages is not None:
                raise ValueError('pages= applies to PDF input only; this source is an SVG')
            # comments are kept: Matplotlib writes each outlined text's string in one
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            root = ET.fromstring(data, parser=parser)
            if _convert.css.local_name(root.tag) != 'svg':
                raise ValueError('%snot an SVG document (root element is <%s>)'
                                 % (label + ': ' if label else '', _convert.css.local_name(root.tag)))
            docs.append((root, None, [], label))
    if fmt == 'svg' and len(docs) > 1:
        raise ValueError('SVG output holds one page; this %s has %d pages here%s'
                         % ('deck' if deck else 'PDF', len(docs), '' if deck else ': choose one with pages='))
    series = max(merge, 1) if merge is not None and merge_scope == 'group' else None
    convs = [_convert.Converter(root, fonts, series, grid) for root, _, _, _ in docs]
    wheres = [' '.join(p for p in (label, 'page %d' % page if page is not None else None) if p)
              for _, page, _, label in docs]
    ks, offsets, size, notes = _place([c.size() for c in convs], wheres, scale, width, height, canvas,
                                      deck or canvas is not None)
    slides = []
    trees = []
    merged = 0
    found = {}
    counts = {'shape': 0, 'text': 0, 'picture': 0, 'group': 0}
    skipped = []
    for (root, page, own_skipped, label), conv, k, offset in zip(docs, convs, ks, offsets):
        nodes, absorbed = _merge.merge(conv.run(k, offset), merge, merge_scope, grid)
        merged += conv.absorbed + absorbed
        for name in conv.found_fonts:
            found.setdefault(name, None)
        trees.append(nodes)
        ser = _drawingml.Serializer()
        parts = []
        ser.write(nodes, parts)
        slides.append((''.join(parts), conv.media))
        for kind, v in ser.counts.items():
            counts[kind] += v
        own = list(own_skipped)
        if page is None:
            own.extend(conv.skipped)
        else:
            own.extend((f, ('page %d %s' % (page, e)).rstrip()) for f, e in conv.skipped)
        if label is not None:
            own = [(f, ('%s %s' % (label, e)).rstrip()) for f, e in own]
        skipped.extend(own)
    if fmt == 'svg':
        from . import _svgout
        data = _svgout.write(trees[0], size[0], size[1], slides[0][1], precision)
    else:
        data = _package.build(slides, size[0], size[1])
    if hasattr(destination, 'write'):
        destination.write(data)
    else:
        with open(os.fspath(destination), 'wb') as fh:
            fh.write(data)
    return Report(counts, skipped, size, merged, found, notes)


def convert_figure(figure, destination, *, fonts=None, merge=2000, merge_scope='group', format=None,
                   precision=None, scale=None, width=None, height=None, canvas_size=None):
    """Convert a Matplotlib figure into a one-slide .pptx with editable parts.

    figure: a Matplotlib ``Figure``, or any object with a Matplotlib-like
        ``savefig(file, format='svg', dpi=...)``.  svgslide does not import
        Matplotlib: when Matplotlib is already loaded, the figure is saved
        with ``svg.fonttype = 'none'`` so that its text stays text.
    destination, fonts, merge, merge_scope, format, precision, scale, width,
        height, canvas_size: as for :func:`convert`.

    Returns the :class:`Report`.
    """
    buf = io.BytesIO()
    mpl = sys.modules.get('matplotlib')
    ctx = mpl.rc_context({'svg.fonttype': 'none'}) if mpl is not None else contextlib.nullcontext()
    with ctx:
        figure.savefig(buf, format='svg', dpi='figure')
    return convert(buf.getvalue(), destination, fonts=fonts, merge=merge, merge_scope=merge_scope, format=format,
                   precision=precision, scale=scale, width=width, height=height, canvas_size=canvas_size)
