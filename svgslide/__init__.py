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
import sys
import xml.etree.ElementTree as ET

from . import _convert, _drawingml, _merge, _package

__all__ = ['convert', 'convert_figure']
__version__ = '0.1.0'


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
    order first seen; text drawn as glyph outlines has none.
    """

    __slots__ = ('shapes', 'text_boxes', 'pictures', 'groups', 'skipped', 'slide_size', 'merged', 'fonts')

    def __init__(self, counts, skipped, slide_size, merged=0, fonts=()):
        self.merged = merged
        self.fonts = list(fonts)
        self.shapes = counts['shape']
        self.text_boxes = counts['text']
        self.pictures = counts['picture']
        self.groups = counts['group']
        self.skipped = skipped
        self.slide_size = slide_size

    def __repr__(self):
        return ('Report(shapes=%d, text_boxes=%d, pictures=%d, groups=%d, merged=%d, fonts=%r, skipped=%r)'
                % (self.shapes, self.text_boxes, self.pictures, self.groups, self.merged, self.fonts,
                   self.skipped))


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


def _format(destination, format):
    if format is None:
        name = '' if hasattr(destination, 'write') else os.fspath(destination)
        return 'svg' if str(name).lower().endswith('.svg') else 'pptx'
    if format not in ('pptx', 'svg'):
        raise ValueError('format must be "pptx" or "svg", got %r' % (format,))
    return format


def convert(source, destination, *, fonts=None, pages=None, merge=2000, merge_scope='group', format=None,
            precision=None):
    """Convert an SVG or a PDF into a .pptx whose parts are editable shapes.

    source: a file path, or the SVG itself as ``str`` or ``bytes``, or the
        PDF as ``bytes`` (recognised by its content, not the file name;
        reading PDFs needs ``pip install svgslide[pdf]``).
    destination: a file path or a binary file object.
    fonts: optional ``{font_name: office_font_name}`` replacement table,
        e.g. ``{"DejaVu Sans": "Arial"}`` (names matched without regard to
        case); the key ``"*"`` stands for every font not named, and a plain
        string replaces every font (``"Arial"``).  Names not in it are kept
        as written.
    pages: PDF only: the 1-based page number or list of page numbers to
        convert, e.g. ``2`` or ``[1, 3]``; default all pages.  Each page
        becomes a slide; the slides have the first page's size and other
        pages are scaled to fit and centred.  An SVG gives one slide.
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

    Returns a :class:`Report` with counts and the skipped features.
    """
    _check_merge(merge, merge_scope)
    fonts = _check_fonts(fonts)
    precision = _check_precision(precision)
    grid = precision * 12700 if precision else None      # EMU
    fmt = _format(destination, format)
    data = _read_source(source)
    if _is_pdf(data):
        from . import _pdf
        docs = _pdf.read(data, pages)
    else:
        if pages is not None:
            raise ValueError('pages= applies to PDF input only; this source is an SVG')
        # comments are kept: Matplotlib writes each outlined text's string in one
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.fromstring(data, parser=parser)
        if _convert.css.local_name(root.tag) != 'svg':
            raise ValueError('not an SVG document (root element is <%s>)' % _convert.css.local_name(root.tag))
        docs = [(root, None, [])]
    if fmt == 'svg' and len(docs) > 1:
        raise ValueError('SVG output holds one page; this PDF has %d pages here: choose one with pages=' % len(docs))
    series = max(merge, 1) if merge is not None and merge_scope == 'group' else None
    slides = []
    trees = []
    merged = 0
    found = {}
    counts = {'shape': 0, 'text': 0, 'picture': 0, 'group': 0}
    skipped = []
    size = None
    for root, page, own_skipped in docs:
        conv = _convert.Converter(root, fonts, series, grid)
        nodes, absorbed = _merge.merge(conv.run(), merge, merge_scope, grid)
        merged += conv.absorbed + absorbed
        for name in conv.found_fonts:
            found.setdefault(name, None)
        trees.append(nodes)
        ser = _drawingml.Serializer()
        parts = []
        ser.write(nodes, parts)
        slides.append((''.join(parts), conv.media))
        for k, v in ser.counts.items():
            counts[k] += v
        skipped.extend(own_skipped)
        if page is None:
            skipped.extend(conv.skipped)
        else:
            skipped.extend((f, ('page %d %s' % (page, e)).rstrip()) for f, e in conv.skipped)
        if size is None:
            size = (conv.slide_w, conv.slide_h)
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
    return Report(counts, skipped, size, merged, found)


def convert_figure(figure, destination, *, fonts=None, merge=2000, merge_scope='group', format=None,
                   precision=None):
    """Convert a Matplotlib figure into a one-slide .pptx with editable parts.

    figure: a Matplotlib ``Figure``, or any object with a Matplotlib-like
        ``savefig(file, format='svg', dpi=...)``.  svgslide does not import
        Matplotlib: when Matplotlib is already loaded, the figure is saved
        with ``svg.fonttype = 'none'`` so that its text stays text.
    destination, fonts, merge, merge_scope, format, precision: as for :func:`convert`.

    Returns the :class:`Report`.
    """
    buf = io.BytesIO()
    mpl = sys.modules.get('matplotlib')
    ctx = mpl.rc_context({'svg.fonttype': 'none'}) if mpl is not None else contextlib.nullcontext()
    with ctx:
        figure.savefig(buf, format='svg', dpi='figure')
    return convert(buf.getvalue(), destination, fonts=fonts, merge=merge, merge_scope=merge_scope, format=format,
                   precision=precision)
