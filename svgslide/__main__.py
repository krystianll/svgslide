"""Command line: ``svgslide INPUT... [-o OUTPUT] [--font OLD=NEW]... [--pages LIST] [--scale F]...``."""

import argparse
import os
import sys

from . import __version__, _canvas, _length, convert
from ._text import replace


def _pages(text):
    """'1,3-5' -> [1, 3, 4, 5]."""
    out = []
    for part in text.split(','):
        part = part.strip()
        lo, sep, hi = part.partition('-')
        try:
            a = int(lo)
            b = int(hi) if sep else a
        except ValueError:
            raise argparse.ArgumentTypeError('not a page list: %r (use e.g. 1,3-5)' % text) from None
        if a < 1 or b < a:
            raise argparse.ArgumentTypeError('not a page list: %r (use e.g. 1,3-5)' % text)
        out.extend(range(a, b + 1))
    return out


def _merge(text):
    if text.lower() == 'none':
        return None
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError('expected a whole number >= 0 or "none", got %r' % text)
    return n


def _precision(text):
    try:
        v = float(text)
    except ValueError:
        v = -1.0
    if not (v > 0 and v != float('inf')):
        raise argparse.ArgumentTypeError('expected a positive number of points, got %r' % text)
    return v


def _scale(text):
    try:
        v = float(text)
    except ValueError:
        v = -1.0
    if not (v > 0 and v != float('inf')):
        raise argparse.ArgumentTypeError('expected a positive number, got %r' % text)
    return v


def _size(text):
    try:
        _length(text, 'size')
    except ValueError:
        raise argparse.ArgumentTypeError('expected a size such as 20cm, 80mm, 4in, 300pt or 400px (a plain '
                                         'number is points), got %r' % text) from None
    return text


def _canvas_size(text):
    try:
        _canvas(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e).replace('canvas_size', '--canvas-size')) from None
    return text


def _font(text):
    old, sep, new = text.partition('=')
    if not sep:                        # a bare NAME replaces every font not named
        old, new = '*', text
    if not old.strip() or not new.strip():
        raise argparse.ArgumentTypeError('expected NAME or OLD=NEW, got %r' % text)
    return old.strip(), new.strip()


def _parser():
    p = argparse.ArgumentParser(
        prog='svgslide',
        description='Convert SVG drawings (.svg, .svgz) and PDF files into PowerPoint files (.pptx) '
                    'whose parts are editable shapes, text boxes and pictures. Each input gives one '
                    '.pptx next to it; several inputs with -o NAME.pptx give one deck instead. An existing '
                    'output file is overwritten. With -o NAME.svg the converted drawing is written as SVG '
                    'instead (one input, one page).')
    p.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    p.add_argument('inputs', nargs='+', metavar='INPUT', help='SVG, SVGZ or PDF file (recognised by content)')
    p.add_argument('-o', '--output', metavar='OUTPUT', help='output .pptx (several inputs: one slide each, in '
                   'order), or .svg for an SVG file (one input; default: each input with the suffix .pptx)')
    p.add_argument('--font', action='append', type=_font, default=[], metavar='OLD=NEW',
                   help='replace a font name, e.g. --font "DejaVu Sans=Arial" (repeatable); '
                        'a bare NAME (--font Arial) replaces every font not named')
    p.add_argument('--pages', type=_pages, metavar='LIST',
                   help='PDF pages to convert, e.g. 1,3-5 (default: all); one slide each')
    p.add_argument('--merge', type=_merge, default=2000, metavar='N|none',
                   help='shapes that look the same become one shape when a set has more than N members '
                        '(default 2000; 0: every set of two or more; none: never)')
    p.add_argument('--merge-scope', choices=('group', 'touching', 'figure'), default='group',
                   help='what forms a set: shapes in one group (default), shapes whose boxes overlap or '
                        'touch, or the whole figure')
    p.add_argument('--precision', type=_precision, metavar='PT',
                   help='round shape outline coordinates to this grid in points, e.g. 0.01 (smaller '
                        'files; default: full precision)')
    p.add_argument('--scale', type=_scale, metavar='F',
                   help='make the whole drawing F times larger, text, lines and pictures included')
    p.add_argument('--width', type=_size, metavar='SIZE',
                   help='final figure width, e.g. 20cm (units cm, mm, in, pt, px; a plain number is points); '
                        'the height follows')
    p.add_argument('--height', type=_size, metavar='SIZE',
                   help='final figure height, e.g. 12cm; with --width both must match the figure\'s shape')
    p.add_argument('--canvas-size', type=_canvas_size, metavar='SIZE',
                   help='slide size with the figure centred on it: widescreen, 4:3, A4, A4-portrait or WxH, '
                        'e.g. 25cmx14cm; a larger figure is shrunk to fit (default: the figure\'s size)')
    return p


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _plural(n, word):
    return '%d %s%s' % (n, word, '' if n == 1 else 'es' if word.endswith('x') else 's')


def main(argv=None):
    """Run the command line; returns the exit status (0 ok, 1 a file failed, 2 usage error)."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code
    deck = args.output is not None and len(args.inputs) > 1
    if deck and args.output.lower().endswith('.svg'):
        parser.print_usage(sys.stderr)
        print('svgslide: error: -o NAME.svg works with one input only', file=sys.stderr)
        return 2
    if args.scale is not None and (args.width is not None or args.height is not None):
        parser.print_usage(sys.stderr)
        print('svgslide: error: give --scale or --width/--height, not both', file=sys.stderr)
        return 2
    if sum(1 for old, _ in args.font if old == '*') > 1:
        parser.print_usage(sys.stderr)
        print('svgslide: error: give at most one bare --font NAME', file=sys.stderr)
        return 2
    fonts = dict(args.font)
    status = 0
    if deck:       # (name for messages, source, output)
        jobs = [(', '.join(args.inputs), list(args.inputs), args.output)]
    else:
        jobs = [(src, src, args.output if args.output is not None else os.path.splitext(src)[0] + '.pptx')
                for src in args.inputs]
    for name, src, dst in jobs:
        # in messages about a deck, the deck is named by its output file
        where = dst if deck else src
        clash = [i for i in args.inputs if os.path.exists(i) and _same(i, dst)]
        if clash:
            print('svgslide: %s: not converted: the output %s is an input file' % (name, dst), file=sys.stderr)
            status = 1
            continue
        try:
            rep = convert(src, dst, fonts=fonts or None, pages=args.pages, merge=args.merge,
                          merge_scope=args.merge_scope, precision=args.precision, scale=args.scale,
                          width=args.width, height=args.height, canvas_size=args.canvas_size)
        except Exception as e:     # one bad file must not stop the others
            print('svgslide: %s: %s' % (name, e), file=sys.stderr)
            status = 1
            continue
        print('%s -> %s: %s, %s, %s, %s, %d merged, %d skipped' % (
            name, dst, _plural(rep.shapes, 'shape'), _plural(rep.text_boxes, 'text box'),
            _plural(rep.pictures, 'picture'), _plural(rep.groups, 'group'), rep.merged, len(rep.skipped)))
        if rep.fonts:
            names = []
            for font in rep.fonts:
                new = replace(font, fonts)
                names.append(font if new == font else '%s -> %s' % (font, new))
            # stdout keeps one line per file; details go to stderr
            print('  fonts in %s: %s' % (where, ', '.join(names)), file=sys.stderr)
        for feature, element in rep.skipped:
            print('  skipped in %s: %s (%s)' % (where, feature, element), file=sys.stderr)
        for note in rep.notes:
            print('  note in %s: %s' % (where, note), file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.exit(main())
