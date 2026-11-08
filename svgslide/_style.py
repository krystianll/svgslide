"""CSS parsing and matching, SVG properties, colours and lengths."""

import colorsys
import re

# CSS named colours (CSS Color Module Level 4).
_NAMED = dict(zip(*[iter("""
aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff
beige f5f5dc bisque ffe4c4 black 000000 blanchedalmond ffebcd blue 0000ff
blueviolet 8a2be2 brown a52a2a burlywood deb887 cadetblue 5f9ea0 chartreuse 7fff00
chocolate d2691e coral ff7f50 cornflowerblue 6495ed cornsilk fff8dc crimson dc143c
cyan 00ffff darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9
darkgreen 006400 darkgrey a9a9a9 darkkhaki bdb76b darkmagenta 8b008b
darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000
darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f
darkslategrey 2f4f4f darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493
deepskyblue 00bfff dimgray 696969 dimgrey 696969 dodgerblue 1e90ff firebrick b22222
floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc
ghostwhite f8f8ff gold ffd700 goldenrod daa520 gray 808080 grey 808080 green 008000
greenyellow adff2f honeydew f0fff0 hotpink ff69b4 indianred cd5c5c indigo 4b0082
ivory fffff0 khaki f0e68c lavender e6e6fa lavenderblush fff0f5 lawngreen 7cfc00
lemonchiffon fffacd lightblue add8e6 lightcoral f08080 lightcyan e0ffff
lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3
lightpink ffb6c1 lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa
lightslategray 778899 lightslategrey 778899 lightsteelblue b0c4de lightyellow ffffe0
lime 00ff00 limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000
mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db
mediumseagreen 3cb371 mediumslateblue 7b68ee mediumspringgreen 00fa9a
mediumturquoise 48d1cc mediumvioletred c71585 midnightblue 191970 mintcream f5fffa
mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead navy 000080 oldlace fdf5e6
olive 808000 olivedrab 6b8e23 orange ffa500 orangered ff4500 orchid da70d6
palegoldenrod eee8aa palegreen 98fb98 paleturquoise afeeee palevioletred db7093
papayawhip ffefd5 peachpuff ffdab9 peru cd853f pink ffc0cb plum dda0dd
powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f
royalblue 4169e1 saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57
seashell fff5ee sienna a0522d silver c0c0c0 skyblue 87ceeb slateblue 6a5acd
slategray 708090 slategrey 708090 snow fffafa springgreen 00ff7f steelblue 4682b4
tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0 violet ee82ee
wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32
""".split())] * 2))

INHERITED = frozenset("""
fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-dasharray
stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit font-family
font-size font-weight font-style text-anchor visibility color marker-start
marker-mid marker-end letter-spacing text-decoration clip-rule dominant-baseline
white-space xml:space image-rendering direction writing-mode
""".split())

PRESENTATION = INHERITED | frozenset("""
opacity clip-path mask filter display stop-color stop-opacity overflow
alignment-baseline baseline-shift
""".split())

ROOT_STYLE = {
    'fill': 'black', 'fill-opacity': '1', 'fill-rule': 'nonzero',
    'stroke': 'none', 'stroke-width': '1', 'stroke-opacity': '1',
    'stroke-dasharray': 'none', 'stroke-linecap': 'butt', 'stroke-linejoin': 'miter',
    'stroke-miterlimit': '4', 'font-family': 'sans-serif', 'font-size': '16',
    'font-weight': 'normal', 'font-style': 'normal', 'text-anchor': 'start',
    'visibility': 'visible', 'color': 'black', 'marker-start': 'none',
    'marker-mid': 'none', 'marker-end': 'none',
}

_UNITS = {'px': 1.0, 'pt': 96 / 72, 'pc': 16.0, 'mm': 96 / 25.4, 'cm': 96 / 2.54,
          'in': 96.0, 'q': 96 / 25.4 / 4, '': 1.0}
_LEN_RE = re.compile(r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$')


def length(v, ref=0.0, font_size=16.0, default=0.0):
    """Parse a length to user units; '%' is relative to ``ref``."""
    if v is None:
        return default
    m = _LEN_RE.match(v)
    if not m:
        return default
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit == '%':
        return num * ref / 100.0
    if unit == 'em':
        return num * font_size
    if unit == 'ex':
        return num * font_size / 2
    return num * _UNITS.get(unit, 1.0)


def is_percent(v):
    return bool(v) and v.strip().endswith('%')


def number(v, default=0.0):
    try:
        s = v.strip()
        if s.endswith('%'):
            return float(s[:-1]) / 100
        return float(s)
    except (AttributeError, ValueError):
        return default


def parse_color(v, current='000000'):
    """Return (hex6, alpha) or None for none/unknown; 'current' = currentColor."""
    if v is None:
        return None
    s = v.strip().lower()
    if not s or s in ('none', 'transparent'):
        return None
    if s == 'currentcolor':
        return parse_color(current) if current else ('000000', 1.0)
    if s[0] == '#':
        h = s[1:]
        if len(h) in (3, 4):
            a = int(h[3] * 2, 16) / 255 if len(h) == 4 else 1.0
            return (h[0] * 2 + h[1] * 2 + h[2] * 2, a)
        if len(h) in (6, 8) and all(c in '0123456789abcdef' for c in h):
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return (h[:6], a)
        return None
    if s.startswith(('rgb(', 'rgba(')):
        parts = [p for p in re.split(r'[\s,/()]+', s[s.index('(') + 1:]) if p]
        if len(parts) < 3:
            return None
        rgb = []
        for p in parts[:3]:
            val = float(p[:-1]) * 2.55 if p.endswith('%') else float(p)
            rgb.append(max(0, min(255, int(round(val)))))
        a = number(parts[3], 1.0) if len(parts) > 3 else 1.0
        return ('%02x%02x%02x' % tuple(rgb), max(0.0, min(1.0, a)))
    if s.startswith(('hsl(', 'hsla(')):
        parts = [p for p in re.split(r'[\s,/()]+', s[s.index('(') + 1:]) if p]
        if len(parts) < 3:
            return None
        h = float(re.sub('[a-z]+', '', parts[0])) / 360.0
        sat = number(parts[1])
        lig = number(parts[2])
        r, g, b = colorsys.hls_to_rgb(h % 1.0, lig, sat)
        a = number(parts[3], 1.0) if len(parts) > 3 else 1.0
        return ('%02x%02x%02x' % (round(r * 255), round(g * 255), round(b * 255)), a)
    if s in _NAMED:
        return (_NAMED[s], 1.0)
    return None


# --- declarations ----------------------------------------------------------

def _split_outside(s, sep):
    if '(' not in s and '"' not in s and "'" not in s:
        return s.split(sep)
    out = []
    depth = 0
    quote = None
    start = 0
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            out.append(s[start:i])
            start = i + 1
    out.append(s[start:])
    return out


def parse_declarations(s):
    """Parse 'a: b; c: d !important' into [(name, value, important)]."""
    out = []
    for part in _split_outside(s, ';'):
        if ':' not in part:
            continue
        name, _, value = part.partition(':')
        name = name.strip().lower()
        value = value.strip()
        imp = False
        if value.lower().endswith('!important'):
            imp = True
            value = value[:-10].strip()
        if not name or not value:
            continue
        for n, v in _expand(name, value):
            out.append((n, v, imp))
    return out


_FONT_WEIGHTS = {'bold', 'bolder', 'lighter', 'normal', '100', '200', '300', '400', '500',
                 '600', '700', '800', '900'}


def _expand(name, value):
    if name == 'marker':
        return [('marker-start', value), ('marker-mid', value), ('marker-end', value)]
    if name == 'font':
        # [style] [variant] [weight] size[/line-height] family
        m = re.match(r'^\s*((?:[\w-]+\s+)*?)([\d.]+[a-z%]*)(?:\s*/\s*[\w.%]+)?\s+(.+)$', value)
        if not m:
            return []
        out = [('font-size', m.group(2)), ('font-family', m.group(3).strip())]
        for word in m.group(1).split():
            w = word.lower()
            if w in ('italic', 'oblique'):
                out.append(('font-style', w))
            elif w in _FONT_WEIGHTS and w != 'normal':
                out.append(('font-weight', w))
        return out
    return [(name, value)]


# --- CSS -------------------------------------------------------------------

_COMMENT = re.compile(r'/\*.*?\*/', re.S)
_SIMPLE = re.compile(r'(\*|[A-Za-z_][\w-]*)?((?:[#.][\w-]+|\[[^\]]*\])*)$')
_PART = re.compile(r'([#.])([\w-]+)|\[\s*([\w:-]+)\s*(?:([~|^$*]?=)\s*["\']?([^"\'\]]*)["\']?\s*)?\]')


class Rule:
    __slots__ = ('chain', 'spec', 'order', 'decls')

    def __init__(self, chain, spec, order, decls):
        self.chain = chain
        self.spec = spec
        self.order = order
        self.decls = decls


def _parse_compound(text):
    m = _SIMPLE.match(text)
    if not m:
        return None
    tag = m.group(1)
    if tag == '*':
        tag = None
    ids, classes, attrs = [], [], []
    for sym, name, attr, op, val in _PART.findall(m.group(2) or ''):
        if sym == '#':
            ids.append(name)
        elif sym == '.':
            classes.append(name)
        else:
            attrs.append((attr, op, val))
    return (tag.lower() if tag else None, ids, classes, attrs)


def _parse_selector(sel):
    sel = sel.strip()
    if not sel or any(ch in sel for ch in ':+~'):
        return None  # pseudo-classes and sibling combinators are not supported
    sel = re.sub(r'\s*>\s*', ' > ', sel)
    toks = sel.split()
    chain = []  # list of (compound, combinator-to-previous)
    comb = ' '
    for tok in toks:
        if tok == '>':
            comb = '>'
            continue
        c = _parse_compound(tok)
        if c is None:
            return None
        chain.append((c, comb))
        comb = ' '
    if not chain:
        return None
    spec = (sum(len(c[1]) for c, _ in chain),
            sum(len(c[2]) + len(c[3]) for c, _ in chain),
            sum(1 for c, _ in chain if c[0]))
    return chain, spec


def parse_stylesheet(text, start_order=0):
    text = _COMMENT.sub('', text)
    text = text.replace('<![CDATA[', '').replace(']]>', '')
    rules = []
    order = start_order
    i = 0
    n = len(text)
    while i < n:
        j = text.find('{', i)
        if j < 0:
            break
        prelude = text[i:j].strip()
        # find the matching close brace
        depth = 1
        k = j + 1
        while k < n and depth:
            if text[k] == '{':
                depth += 1
            elif text[k] == '}':
                depth -= 1
            k += 1
        body = text[j + 1:k - 1]
        i = k
        # drop leading statements like "@import url(x);" before the block
        if ';' in prelude:
            prelude = prelude.rsplit(';', 1)[1].strip()
        if prelude.startswith('@'):
            continue
        decls = parse_declarations(body)
        if not decls:
            continue
        for sel in prelude.split(','):
            parsed = _parse_selector(sel)
            if parsed is None:
                continue
            chain, spec = parsed
            rules.append(Rule(chain, spec, order, decls))
            order += 1
    return rules


def local_name(tag):
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1]


def _match_compound(el, comp):
    tag, ids, classes, attrs = comp
    if tag and local_name(el.tag).lower() != tag:
        return False
    if ids and el.get('id') not in ids:
        return False
    if classes:
        cls = (el.get('class') or '').split()
        for c in classes:
            if c not in cls:
                return False
    for name, op, val in attrs:
        have = el.get(name)
        if have is None:
            return False
        if op == '=' and have != val:
            return False
        if op == '~=' and val not in have.split():
            return False
        if op == '^=' and not have.startswith(val):
            return False
        if op == '$=' and not have.endswith(val):
            return False
        if op == '*=' and val not in have:
            return False
        if op == '|=' and not (have == val or have.startswith(val + '-')):
            return False
    return True


def _match_chain(el, chain, parents):
    idx = len(chain) - 1
    comp, comb = chain[idx]
    if not _match_compound(el, comp):
        return False
    cur = el
    while idx > 0:
        need_direct = comb == '>'
        idx -= 1
        comp, comb_next = chain[idx]
        cur = parents.get(cur)
        if need_direct:
            if cur is None or not _match_compound(cur, comp):
                return False
        else:
            while cur is not None and not _match_compound(cur, comp):
                cur = parents.get(cur)
            if cur is None:
                return False
        comb = comb_next
    return True


class StyleSheet:
    """Rules indexed by the key of their rightmost compound selector."""

    def __init__(self, rules, parents):
        self.parents = parents
        self.by_id = {}
        self.by_class = {}
        self.by_tag = {}
        self.universal = []
        for r in rules:
            tag, ids, classes, _ = r.chain[-1][0]
            if ids:
                self.by_id.setdefault(ids[0], []).append(r)
            elif classes:
                self.by_class.setdefault(classes[0], []).append(r)
            elif tag:
                self.by_tag.setdefault(tag, []).append(r)
            else:
                self.universal.append(r)
        self.empty = not rules

    def matching(self, el):
        if self.empty:
            return []
        cands = list(self.universal)
        i = el.get('id')
        if i and i in self.by_id:
            cands.extend(self.by_id[i])
        c = el.get('class')
        if c:
            for cl in c.split():
                cands.extend(self.by_class.get(cl, ()))
        t = local_name(el.tag).lower()
        cands.extend(self.by_tag.get(t, ()))
        out = [r for r in cands if _match_chain(el, r.chain, self.parents)]
        out.sort(key=lambda r: (r.spec, r.order))
        return out


def specified(el, sheet):
    """Specified properties of one element: attributes < CSS rules < style=""."""
    props = {}
    for k, v in el.attrib.items():
        k = local_name(k) if k.startswith('{') else k
        if k in PRESENTATION:
            props[k] = v.strip()
        elif k == 'space' or k == 'xml:space':
            props['xml:space'] = v.strip()
    important = {}
    for r in sheet.matching(el):
        for n, v, imp in r.decls:
            (important if imp else props)[n] = v
    st = el.get('style')
    if st:
        for n, v, imp in parse_declarations(st):
            (important if imp else props)[n] = v
    props.update(important)
    return props


def cascade(parent, spec):
    """Computed style from the parent's computed style and specified values."""
    out = {k: v for k, v in parent.items() if k in INHERITED}
    for k, v in spec.items():
        if v == 'inherit':
            if k in parent:
                out[k] = parent[k]
            continue
        out[k] = v
    fs = spec.get('font-size')
    if fs and fs != 'inherit':
        pfs = number(parent.get('font-size', '16'), 16.0)
        out['font-size'] = repr(font_size(fs, pfs))
    return out


_FS_KEYWORDS = {'xx-small': 9, 'x-small': 10, 'small': 13, 'medium': 16, 'large': 18,
                'x-large': 24, 'xx-large': 32}


def font_size(v, parent):
    v = v.strip().lower()
    if v in _FS_KEYWORDS:
        return float(_FS_KEYWORDS[v])
    if v == 'larger':
        return parent * 1.2
    if v == 'smaller':
        return parent / 1.2
    return length(v, ref=parent, font_size=parent, default=parent)
