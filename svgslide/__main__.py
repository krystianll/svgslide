"""Command line: ``svgslide INPUT... [-o OUTPUT] [--font OLD=NEW]... [--pages LIST]``."""

import argparse
import os
import sys

from . import convert
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
                    '.pptx next to it; an existing output file is overwritten. With -o NAME.svg the '
                    'converted drawing is written as SVG instead (one page).')
    p.add_argument('inputs', nargs='+', metavar='INPUT', help='SVG, SVGZ or PDF file (recognised by content)')
    p.add_argument('-o', '--output', metavar='OUTPUT', help='output .pptx, or .svg for an SVG file (only with '
                   'one input; default: the input with the suffix .pptx)')
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
    if args.output is not None and len(args.inputs) > 1:
        parser.print_usage(sys.stderr)
        print('svgslide: error: -o/--output works with one input only', file=sys.stderr)
        return 2
    if sum(1 for old, _ in args.font if old == '*') > 1:
        parser.print_usage(sys.stderr)
        print('svgslide: error: give at most one bare --font NAME', file=sys.stderr)
        return 2
    fonts = dict(args.font)
    status = 0
    for src in args.inputs:
        dst = args.output if args.output is not None else os.path.splitext(src)[0] + '.pptx'
        clash = [i for i in args.inputs if os.path.exists(i) and _same(i, dst)]
        if clash:
            print('svgslide: %s: not converted: the output %s is an input file' % (src, dst), file=sys.stderr)
            status = 1
            continue
        try:
            rep = convert(src, dst, fonts=fonts or None, pages=args.pages, merge=args.merge,
                          merge_scope=args.merge_scope, precision=args.precision)
        except Exception as e:     # one bad file must not stop the others
            print('svgslide: %s: %s' % (src, e), file=sys.stderr)
            status = 1
            continue
        print('%s -> %s: %s, %s, %s, %s, %d merged, %d skipped' % (
            src, dst, _plural(rep.shapes, 'shape'), _plural(rep.text_boxes, 'text box'),
            _plural(rep.pictures, 'picture'), _plural(rep.groups, 'group'), rep.merged, len(rep.skipped)))
        if rep.fonts:
            names = []
            for name in rep.fonts:
                new = replace(name, fonts)
                names.append(name if new == name else '%s -> %s' % (name, new))
            # stdout keeps one line per file; details go to stderr
            print('  fonts in %s: %s' % (src, ', '.join(names)), file=sys.stderr)
        for feature, element in rep.skipped:
            print('  skipped in %s: %s (%s)' % (src, feature, element), file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.exit(main())
