"""Affine transforms, SVG path parsing, bounding boxes and rectangle clipping.

A path is a list of subpaths; a subpath is ``[start, segments, closed]`` where
``start`` is ``(x, y)`` and each segment is one of
``('L', x, y)``, ``('Q', x1, y1, x, y)`` or ``('C', x1, y1, x2, y2, x, y)``
in absolute coordinates.  Arcs become cubic curves.
"""

import math
import re

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m, n):
    """Return m * n (apply n first, then m)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B,
            a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def translate(tx, ty):
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def scale(sx, sy=None):
    return (sx, 0.0, 0.0, sx if sy is None else sy, 0.0, 0.0)


def rotate(deg, cx=0.0, cy=0.0):
    t = math.radians(deg)
    co, si = math.cos(t), math.sin(t)
    r = (co, si, -si, co, 0.0, 0.0)
    if cx or cy:
        return mul(translate(cx, cy), mul(r, translate(-cx, -cy)))
    return r


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def det(m):
    return m[0] * m[3] - m[1] * m[2]


def mean_scale(m):
    return math.sqrt(abs(det(m)))


def invert(m):
    a, b, c, d, e, f = m
    dt = a * d - b * c
    if abs(dt) < 1e-300:
        return None
    ia, ib, ic, id_ = d / dt, -b / dt, -c / dt, a / dt
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


def decompose(m, tol=1e-6):
    """Split m into rotation, scales and a vertical flip, if it has no skew.

    Returns ``(theta_rad, sx, sy, flip)`` with ``m = T * R(theta) * S(sx, ±sy)``
    or None when the columns are not orthogonal (skew).
    """
    a, b, c, d = m[0], m[1], m[2], m[3]
    sx = math.hypot(a, b)
    sy = math.hypot(c, d)
    if sx == 0 or sy == 0:
        return None
    if abs(a * c + b * d) > tol * sx * sy:
        return None
    theta = math.atan2(b, a)
    return theta, sx, sy, (a * d - b * c) < 0


_NUM = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
_NUM_RE = re.compile(_NUM)
_TRANSFORM_RE = re.compile(r'(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)')


def parse_numbers(s):
    return [float(v) for v in _NUM_RE.findall(s or '')]


def parse_transform(s):
    m = IDENTITY
    if not s:
        return m
    for name, args in _TRANSFORM_RE.findall(s):
        v = parse_numbers(args)
        if name == 'matrix' and len(v) == 6:
            t = tuple(v)
        elif name == 'translate' and v:
            t = translate(v[0], v[1] if len(v) > 1 else 0.0)
        elif name == 'scale' and v:
            t = scale(v[0], v[1] if len(v) > 1 else v[0])
        elif name == 'rotate' and v:
            t = rotate(v[0], v[1], v[2]) if len(v) >= 3 else rotate(v[0])
        elif name == 'skewX' and v:
            t = (1.0, 0.0, math.tan(math.radians(v[0])), 1.0, 0.0, 0.0)
        elif name == 'skewY' and v:
            t = (1.0, math.tan(math.radians(v[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = mul(m, t)
    return m


# --- path data -----------------------------------------------------------

_PATH_TOKEN = re.compile(r'([MmZzLlHhVvCcSsQqTtAa])|(' + _NUM + ')')
_ARGS = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7, 'Z': 0}


def _tokens(d):
    out = []
    for cmd, num in _PATH_TOKEN.findall(d):
        out.append(cmd if cmd else num)
    return out


def parse_path(d):
    """Parse SVG path data into subpaths (see module docstring)."""
    toks = _tokens(d or '')
    subs = []
    cur = None
    x = y = 0.0
    sx = sy = 0.0
    last_ctrl = None  # (kind, x, y) for S/T reflection
    i = 0
    n = len(toks)
    cmd = None
    while i < n:
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in 'Zz':
                if cur is not None:
                    cur[2] = True
                    x, y = sx, sy
                    cur = None
                last_ctrl = None
                continue
        elif cmd is None:
            break
        up = cmd.upper()
        rel = cmd != up
        k = _ARGS[up]
        if up == 'A':
            # Arc flags may be written without separators ("a1 1 0 01 10 10").
            args = []
            while len(args) < 7 and i < n and not toks[i].isalpha():
                tok = toks[i]
                if len(args) in (3, 4) and len(tok) > 1 and tok[0] in '01' and tok[1] != '.' and tok[1] not in 'eE':
                    args.append(float(tok[0]))
                    toks[i] = tok[1:]
                    continue
                args.append(float(tok))
                i += 1
            if len(args) < 7:
                break
        else:
            if i + k > n:
                break
            try:
                args = [float(v) for v in toks[i:i + k]]
            except ValueError:
                break
            i += k
        if up == 'M':
            nx, ny = args
            if rel:
                nx += x
                ny += y
            cur = [(nx, ny), [], False]
            subs.append(cur)
            x, y = sx, sy = nx, ny
            last_ctrl = None
            cmd = 'l' if rel else 'L'
            continue
        if cur is None:
            cur = [(x, y), [], False]
            subs.append(cur)
            sx, sy = x, y
        segs = cur[1]
        if up == 'L':
            nx, ny = args
            if rel:
                nx += x
                ny += y
            segs.append(('L', nx, ny))
            x, y = nx, ny
            last_ctrl = None
        elif up == 'H':
            nx = args[0] + (x if rel else 0.0)
            segs.append(('L', nx, y))
            x = nx
            last_ctrl = None
        elif up == 'V':
            ny = args[0] + (y if rel else 0.0)
            segs.append(('L', x, ny))
            y = ny
            last_ctrl = None
        elif up == 'C':
            x1, y1, x2, y2, nx, ny = args
            if rel:
                x1 += x; y1 += y; x2 += x; y2 += y; nx += x; ny += y
            segs.append(('C', x1, y1, x2, y2, nx, ny))
            last_ctrl = ('C', x2, y2)
            x, y = nx, ny
        elif up == 'S':
            x2, y2, nx, ny = args
            if rel:
                x2 += x; y2 += y; nx += x; ny += y
            if last_ctrl and last_ctrl[0] == 'C':
                x1, y1 = 2 * x - last_ctrl[1], 2 * y - last_ctrl[2]
            else:
                x1, y1 = x, y
            segs.append(('C', x1, y1, x2, y2, nx, ny))
            last_ctrl = ('C', x2, y2)
            x, y = nx, ny
        elif up == 'Q':
            x1, y1, nx, ny = args
            if rel:
                x1 += x; y1 += y; nx += x; ny += y
            segs.append(('Q', x1, y1, nx, ny))
            last_ctrl = ('Q', x1, y1)
            x, y = nx, ny
        elif up == 'T':
            nx, ny = args
            if rel:
                nx += x; ny += y
            if last_ctrl and last_ctrl[0] == 'Q':
                x1, y1 = 2 * x - last_ctrl[1], 2 * y - last_ctrl[2]
            else:
                x1, y1 = x, y
            segs.append(('Q', x1, y1, nx, ny))
            last_ctrl = ('Q', x1, y1)
            x, y = nx, ny
        elif up == 'A':
            rx, ry, rot, large, sweep, nx, ny = args
            if rel:
                nx += x; ny += y
            segs.extend(arc_to_cubics(x, y, rx, ry, rot, large != 0, sweep != 0, nx, ny))
            x, y = nx, ny
            last_ctrl = None
    return subs


def arc_to_cubics(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    """Convert an SVG elliptical arc to cubic segments (SVG spec F.6)."""
    if x1 == x2 and y1 == y2:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [('L', x2, y2)]
    phi = math.radians(phi_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    nseg = max(1, int(math.ceil(abs(dth) / (math.pi / 2) - 1e-9)))
    delta = dth / nseg
    t = 4.0 / 3.0 * math.tan(delta / 4)
    out = []
    a0 = th1
    for k in range(nseg):
        a1 = a0 + delta
        c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        p1 = (c0 - t * s0, s0 + t * c0)
        p2 = (c1 + t * s1, s1 - t * c1)
        p3 = (c1, s1)
        pts = []
        for (ux, uy) in (p1, p2, p3):
            ex, ey = ux * rx, uy * ry
            pts.append(cp * ex - sp * ey + cx)
            pts.append(sp * ex + cp * ey + cy)
        if k == nseg - 1:
            pts[4], pts[5] = x2, y2
        out.append(('C',) + tuple(pts))
        a0 = a1
    return out


def transform_path(subs, m):
    a, b, c, d, e, f = m
    out = []
    for (sx, sy), segs, closed in subs:
        ns = []
        for s in segs:
            if s[0] == 'L':
                x, y = s[1], s[2]
                ns.append(('L', a * x + c * y + e, b * x + d * y + f))
            elif s[0] == 'C':
                ns.append(('C', a * s[1] + c * s[2] + e, b * s[1] + d * s[2] + f,
                           a * s[3] + c * s[4] + e, b * s[3] + d * s[4] + f,
                           a * s[5] + c * s[6] + e, b * s[5] + d * s[6] + f))
            else:
                ns.append(('Q', a * s[1] + c * s[2] + e, b * s[1] + d * s[2] + f,
                           a * s[3] + c * s[4] + e, b * s[3] + d * s[4] + f))
        out.append([(a * sx + c * sy + e, b * sx + d * sy + f), ns, closed])
    return out


def _cubic_extrema(p0, p1, p2, p3):
    a = -p0 + 3 * p1 - 3 * p2 + p3
    b = 2 * (p0 - 2 * p1 + p2)
    c = p1 - p0
    ts = []
    if abs(a) < 1e-12:
        if abs(b) > 1e-12:
            ts.append(-c / b)
    else:
        disc = b * b - 4 * a * c
        if disc >= 0:
            r = math.sqrt(disc)
            ts.append((-b + r) / (2 * a))
            ts.append((-b - r) / (2 * a))
    vals = []
    for t in ts:
        if 0 < t < 1:
            u = 1 - t
            vals.append(u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3)
    return vals


def path_bbox(subs):
    """Tight bounding box (x0, y0, x1, y1) of a path, or None if empty."""
    xs = []
    ys = []
    for (sx, sy), segs, _ in subs:
        xs.append(sx)
        ys.append(sy)
        px, py = sx, sy
        for s in segs:
            if s[0] == 'L':
                px, py = s[1], s[2]
            elif s[0] == 'C':
                x1, y1, x2, y2, x3, y3 = s[1:]
                if not (min(px, x3) <= x1 <= max(px, x3) and min(px, x3) <= x2 <= max(px, x3)):
                    xs.extend(_cubic_extrema(px, x1, x2, x3))
                if not (min(py, y3) <= y1 <= max(py, y3) and min(py, y3) <= y2 <= max(py, y3)):
                    ys.extend(_cubic_extrema(py, y1, y2, y3))
                px, py = x3, y3
            else:
                x1, y1, x3, y3 = s[1:]
                for (p0, p1, p2, acc) in ((px, x1, x3, xs), (py, y1, y3, ys)):
                    den = p0 - 2 * p1 + p2
                    if abs(den) > 1e-12:
                        t = (p0 - p1) / den
                        if 0 < t < 1:
                            acc.append((1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t * t * p2)
                px, py = x3, y3
            xs.append(px)
            ys.append(py)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def flatten(subs, steps=12):
    """Return [(points, closed)] polylines approximating the path."""
    out = []
    for (sx, sy), segs, closed in subs:
        pts = [(sx, sy)]
        px, py = sx, sy
        for s in segs:
            if s[0] == 'L':
                px, py = s[1], s[2]
                pts.append((px, py))
            elif s[0] == 'C':
                x1, y1, x2, y2, x3, y3 = s[1:]
                for k in range(1, steps + 1):
                    t = k / steps
                    u = 1 - t
                    pts.append((u * u * u * px + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                                u * u * u * py + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3))
                px, py = x3, y3
            else:
                x1, y1, x3, y3 = s[1:]
                for k in range(1, steps + 1):
                    t = k / steps
                    u = 1 - t
                    pts.append((u * u * px + 2 * u * t * x1 + t * t * x3,
                                u * u * py + 2 * u * t * y1 + t * t * y3))
                px, py = x3, y3
        out.append((pts, closed))
    return out


def flatten_tol(subs, tol):
    """[points] per subpath, curves split so no point strays more than about
    ``tol`` from the curve; a closed subpath ends at its start point."""
    out = []
    tol = max(tol, 1e-9)
    for (sx, sy), segs, closed in subs:
        pts = [(sx, sy)]
        px, py = sx, sy
        for s in segs:
            if s[0] == 'L':
                px, py = s[1], s[2]
                pts.append((px, py))
                continue
            if s[0] == 'C':
                x1, y1, x2, y2, x3, y3 = s[1:]
                d = max(math.hypot(px - 2 * x1 + x2, py - 2 * y1 + y2),
                        math.hypot(x1 - 2 * x2 + x3, y1 - 2 * y2 + y3))
                n = max(1, min(1000, int(math.ceil(math.sqrt(0.75 * d / tol)))))
                for k in range(1, n + 1):
                    t = k / n
                    u = 1 - t
                    pts.append((u * u * u * px + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                                u * u * u * py + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3))
            else:
                x1, y1, x3, y3 = s[1:]
                d = math.hypot(px - 2 * x1 + x3, py - 2 * y1 + y3)
                n = max(1, min(1000, int(math.ceil(math.sqrt(0.25 * d / tol)))))
                for k in range(1, n + 1):
                    t = k / n
                    u = 1 - t
                    pts.append((u * u * px + 2 * u * t * x1 + t * t * x3,
                                u * u * py + 2 * u * t * y1 + t * t * y3))
            px, py = s[-2], s[-1]
        if closed and pts[-1] != pts[0]:
            pts.append(pts[0])
        out.append(pts)
    return out


def polys_to_path(polys):
    subs = []
    for pts, closed in polys:
        if len(pts) < 2:
            continue
        subs.append([pts[0], [('L', x, y) for x, y in pts[1:]], closed])
    return subs


def clip_polygon(pts, r):
    """Sutherland-Hodgman clip of a polygon to rectangle r=(x0,y0,x1,y1)."""
    x0, y0, x1, y1 = r
    for edge in range(4):
        if not pts:
            return pts
        out = []
        prev = pts[-1]
        for cur in pts:
            pin = _inside(prev, edge, x0, y0, x1, y1)
            cin = _inside(cur, edge, x0, y0, x1, y1)
            if cin:
                if not pin:
                    out.append(_intersect(prev, cur, edge, x0, y0, x1, y1))
                out.append(cur)
            elif pin:
                out.append(_intersect(prev, cur, edge, x0, y0, x1, y1))
            prev = cur
        pts = out
    return pts


def _inside(p, edge, x0, y0, x1, y1):
    if edge == 0:
        return p[0] >= x0
    if edge == 1:
        return p[0] <= x1
    if edge == 2:
        return p[1] >= y0
    return p[1] <= y1


def _intersect(p, q, edge, x0, y0, x1, y1):
    (px, py), (qx, qy) = p, q
    if edge in (0, 1):
        xe = x0 if edge == 0 else x1
        t = (xe - px) / (qx - px) if qx != px else 0.0
        return (xe, py + t * (qy - py))
    ye = y0 if edge == 2 else y1
    t = (ye - py) / (qy - py) if qy != py else 0.0
    return (px + t * (qx - px), ye)


def clip_polyline(pts, r):
    """Liang-Barsky clip of a polyline; returns a list of polylines."""
    x0, y0, x1, y1 = r
    out = []
    cur = None
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        ok = False
                        break
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        ok = False
                        break
                    t1 = min(t1, t)
        if not ok:
            cur = None
            continue
        sa = (ax + t0 * dx, ay + t0 * dy)
        sb = (ax + t1 * dx, ay + t1 * dy)
        if cur is None or t0 > 0:
            cur = [sa]
            out.append(cur)
        cur.append(sb)
        if t1 < 1:
            cur = None
    return out


def _on_edge(pt, r, tol):
    x, y = pt
    inside = r[0] - tol <= x <= r[2] + tol and r[1] - tol <= y <= r[3] + tol
    return inside and (abs(x - r[0]) <= tol or abs(x - r[2]) <= tol or abs(y - r[1]) <= tol or abs(y - r[3]) <= tol)


def _shorten(pts, d):
    """Remove length d from the start of the polyline; None when nothing is left."""
    pts = list(pts)
    while len(pts) >= 2:
        (ax, ay), (bx, by) = pts[0], pts[1]
        n = math.hypot(bx - ax, by - ay)
        if n > d:
            pts[0] = (ax + (bx - ax) * d / n, ay + (by - ay) * d / n)
            return pts
        d -= n
        pts.pop(0)
    return None


def pull_back_ends(part, d, r, tol=1.0):
    """For a stroked polyline clipped to rectangle r: move each end that lies
    on the rectangle's edge back along the line by d (half the line width),
    so that a square or round cap ends at the edge instead of crossing it.
    An end segment shorter than the remaining distance is dropped and the
    rest taken from the next one; returns None when the whole polyline is
    used up (a stub shorter than its caps)."""
    if len(part) < 2 or part[0] == part[-1]:
        return part
    start, end = _on_edge(part[0], r, tol), _on_edge(part[-1], r, tol)
    if not (start or end):
        return part
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(part, part[1:]))
    if total <= d * (start + end):
        return None
    pts = part
    if start:
        pts = _shorten(pts, d)
    if end:
        pts = _shorten(pts[::-1], d)[::-1]
    return pts


def bbox_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def rect_corners_bbox(m, x, y, w, h):
    pts = [apply(m, x, y), apply(m, x + w, y), apply(m, x + w, y + h), apply(m, x, y + h)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def signed_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2


def point_in_polygon(x, y, pts):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def winding_number(x, y, pts):
    wn = 0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if y1 <= y:
            if y2 > y and (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1) > 0:
                wn += 1
        elif y2 <= y and (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1) < 0:
            wn -= 1
    return wn
