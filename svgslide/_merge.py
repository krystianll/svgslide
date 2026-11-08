"""Merge identical-looking shapes into one shape (one outline per member).

A set is the shapes that look the same (same fill and line, see
``Leaf.look``): within one group (scope "group"), anywhere (scope
"figure"), or connected by overlapping or touching boxes (scope
"touching").  A set with more than ``limit`` members becomes merged shapes.
Members are taken in drawing order; the merged shape is drawn where its
first member was, so a member joins it only when nothing that looks
different, drawn in between, overlaps the member.  Members of a look whose
overlaps would show (transparent paint, or a line and a fill of different
colours) must not overlap each other either.  Otherwise a new merged shape
starts.  The picture stays the same.
"""

import math
import re

from . import _drawingml as dml
from . import _geom as g

SCOPES = ('group', 'touching', 'figure')
TOUCH = 100.0           # EMU (about 0.01 px): boxes this close touch
_KAPPA = 0.5522847498307936
_WIDTH = re.compile(r'<a:ln w="(\d+)"')
_COLOR = re.compile(r'<a:srgbClr val="([0-9A-F]{6})"')


def merge(nodes, limit, scope, grid=None):
    """Return (nodes, number of shapes absorbed into merged shapes)."""
    if limit is None:
        return nodes, 0
    if scope not in SCOPES:
        raise ValueError('merge_scope must be one of %s, got %r' % (', '.join(SCOPES), scope))
    state = {'absorbed': 0, 'replace': {}, 'grid': grid}
    if scope == 'group':
        _group(nodes, limit, state)
    else:
        flat = []
        _flatten(nodes, flat)
        items = [(n, _extent(n)) for n in flat]
        if scope == 'figure':
            keys = [n.look for n in flat]
        else:
            keys = _touching(items)
        _run(items, keys, limit, state)
    if not state['replace']:
        return nodes, 0
    return dml.collapse(_rebuild(nodes, state['replace'])), state['absorbed']


def _group(nodes, limit, state):
    for n in nodes:
        if isinstance(n, dml.Group):
            _group(n.children, limit, state)
    items = [(n, _extent(n)) for n in nodes]
    keys = [n.look if isinstance(n, dml.Leaf) else None for n in nodes]
    _run(items, keys, limit, state)


def _flatten(nodes, out):
    for n in nodes:
        if isinstance(n, dml.Group):
            _flatten(n.children, out)
        else:
            out.append(n)


def _extent(n):
    """Visual box of a node: a shape's box grown by half its line width."""
    if isinstance(n, dml.Group):
        return dml.node_bbox(n)
    bb = n.bbox
    if n.look is not None:
        m = _WIDTH.search(n.look[1])
        if m:
            d = int(m.group(1)) / 2.0
            bb = (bb[0] - d, bb[1] - d, bb[2] + d, bb[3] + d)
    return bb


def _overlap(a, b, tol=0.0):
    return a[0] < b[2] + tol and b[0] < a[2] + tol and a[1] < b[3] + tol and b[1] < a[3] + tol


def _safe(look):
    """True when members of this look may overlap inside one merged shape:
    opaque paint, and not a line and a fill of different colours."""
    fx, lx = look
    if '<a:alpha' in fx or '<a:alpha' in lx:
        return False
    if fx == '<a:noFill/>' or lx == dml.NO_LINE:
        return True
    a, b = _COLOR.search(fx), _COLOR.search(lx)
    return bool(a and b and a.group(1) == b.group(1))


class _Grid:
    """Boxes in square cells, for overlap queries in about constant time."""

    def __init__(self, boxes):
        xs = [b[0] for b in boxes] + [b[2] for b in boxes]
        ys = [b[1] for b in boxes] + [b[3] for b in boxes]
        self.x0, self.y0 = min(xs), min(ys)
        span = max(max(xs) - self.x0, max(ys) - self.y0, 1.0)
        # cells about twice the size of a typical box, at most 128 per side
        sizes = sorted(max(b[2] - b[0], b[3] - b[1]) for b in boxes)
        typical = sizes[len(sizes) // 2] if sizes else span
        self.size = max(span / 128.0, typical * 2.0, 1.0)
        self.cells = {}

    def keys(self, b):
        s = self.size
        i0, i1 = int((b[0] - self.x0) // s), int((b[2] - self.x0) // s)
        j0, j1 = int((b[1] - self.y0) // s), int((b[3] - self.y0) // s)
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def _touching(items):
    """Set key per item: (look, component) for shapes of one look whose
    boxes overlap or touch, chained; None for items that never merge."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    shapes = [k for k, (n, _) in enumerate(items) if n.look is not None]
    if shapes:
        grid = _Grid([items[k][1] for k in shapes])
        for k in shapes:
            n, bb = items[k]
            grown = (bb[0] - TOUCH, bb[1] - TOUCH, bb[2] + TOUCH, bb[3] + TOUCH)
            for key in grid.keys(grown):
                # per cell and look: members grouped by their component, so
                # that a component already joined is skipped as a whole
                comps = grid.cells.setdefault(key, {}).setdefault(n.look, {})
                for r in list(comps):
                    if r not in comps:
                        continue
                    rr = find(r)
                    if rr != r:                         # components joined since: one list
                        comps.setdefault(rr, []).extend(comps.pop(r))
                        r = rr
                    if r == find(k):
                        continue
                    for j in comps[r]:
                        if _overlap(items[j][1], bb, TOUCH):
                            parent[r] = find(k)
                            break
                root = find(k)
                lst = comps.setdefault(root, [])
                lst.append(k)
                for r in [r for r in comps if r != root and find(r) == root]:
                    lst.extend(comps.pop(r))
    return [(n.look, find(k)) if n.look is not None else None for k, (n, _) in enumerate(items)]


def _run(items, keys, limit, state):
    """Merge the sets (items with equal non-None keys) with more than limit members."""
    counts = {}
    for k in keys:
        if k is not None:
            counts[k] = counts.get(k, 0) + 1
    active = {k for k, c in counts.items() if c > limit and c >= 2}
    if not active:
        return
    grid = _Grid([bb for _, bb in items])
    runs = {}       # set key -> current run: [start seq, members, members may overlap]
    done = []
    for seq, ((n, bb), key) in enumerate(zip(items, keys)):
        look = n.look if isinstance(n, dml.Leaf) else None
        run = None
        if key in active:
            run = runs.get(key)
            if run is None or not _may_join(grid, bb, look, run):
                if run is not None:
                    done.append(run)
                run = [seq, [], _safe(look)]
                runs[key] = run
            run[1].append(n)
        tag = run[0] if run is not None else None     # the run a member belongs to
        for ck in grid.keys(bb):
            grid.cells.setdefault(ck, {}).setdefault(look, []).append((seq, bb, tag))
    done.extend(runs.values())
    for run in done:
        members = run[1]
        if len(members) < 2:
            continue
        state['replace'][id(members[0])] = _merged(members, state['grid'])
        for m in members[1:]:
            state['replace'][id(m)] = None
        state['absorbed'] += len(members) - 1


def _may_join(grid, bb, look, run):
    start, _, safe = run
    for ck in grid.keys(bb):
        cell = grid.cells.get(ck)
        if not cell:
            continue
        for other, entries in cell.items():
            if other == look:
                if safe:
                    continue
                for seq, obb, otag in reversed(entries):
                    if seq < start:
                        break
                    if otag == start and _overlap(obb, bb):
                        return False
                continue
            for seq, obb, _ in reversed(entries):
                if seq <= start:
                    break
                if _overlap(obb, bb):
                    return False
    return True


def _rebuild(nodes, replace):
    out = []
    for n in nodes:
        if isinstance(n, dml.Group):
            out.append(dml.Group(n.label, _rebuild(n.children, replace)))
            continue
        r = replace.get(id(n), n)
        if r is not None:
            out.append(r)
    return out


def _merged(members, grid=None):
    fx, lx = members[0].look
    filled, stroked = fx != '<a:noFill/>', lx != dml.NO_LINE
    paths = []
    for m in members:
        if m.geom[0] == 'paths':
            paths.extend(m.geom[1])
        else:
            paths.append((preset_path(*m.geom[1:]), filled, stroked))
    bb = None
    for subs, _, _ in paths:
        bb = g.bbox_union(bb, g.path_bbox(subs))
    x0, y0 = bb[0], bb[1]
    W, H = max(bb[2] - x0, 1.0), max(bb[3] - y0, 1.0)
    shift = (1, 0, 0, 1, -x0, -y0)
    geo = dml.cust_geom([(g.transform_path(s, shift), W, H, f, k) for s, f, k in paths], grid)
    body = ('<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>' + dml.xfrm(x0, y0, W, H) +
            geo + fx + lx + '</p:spPr></p:sp>')
    return dml.Leaf((x0, y0, x0 + W, y0 + H), 'shape', None, '<p:sp><p:nvSpPr>', body)


def preset_path(kind, box, rot, corner):
    """Subpaths of a preset shape (rect, roundRect, ellipse) in slide EMU."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    if kind == 'ellipse':
        rx, ry = w / 2, h / 2
        kx, ky = rx * _KAPPA, ry * _KAPPA
        segs = [('C', cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry),
                ('C', cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy),
                ('C', cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry),
                ('C', cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy)]
        subs = [[(cx + rx, cy), segs, True]]
    elif kind == 'roundRect' and corner > 0:
        r = corner * min(w, h)
        k = r * (1 - _KAPPA)
        segs = [('L', x1 - r, y0), ('C', x1 - k, y0, x1, y0 + k, x1, y0 + r),
                ('L', x1, y1 - r), ('C', x1, y1 - k, x1 - k, y1, x1 - r, y1),
                ('L', x0 + r, y1), ('C', x0 + k, y1, x0, y1 - k, x0, y1 - r),
                ('L', x0, y0 + r), ('C', x0, y0 + k, x0 + k, y0, x0 + r, y0)]
        subs = [[(x0 + r, y0), segs, True]]
    else:
        subs = [[(x0, y0), [('L', x1, y0), ('L', x1, y1), ('L', x0, y1)], True]]
    if rot:
        subs = g.transform_path(subs, g.rotate(math.degrees(rot), cx, cy))
    return subs
