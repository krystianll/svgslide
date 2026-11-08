"""Text width estimates and font-name choice."""

# Helvetica/Arial advance widths (1/1000 em) for ASCII 32..126.
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
      1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
      667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
      333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
      556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]

GENERIC = {
    'sans-serif': 'Arial', 'serif': 'Times New Roman', 'monospace': 'Courier New',
    'cursive': 'Comic Sans MS', 'fantasy': 'Impact', 'system-ui': 'Arial',
    'ui-sans-serif': 'Arial', 'ui-serif': 'Times New Roman', 'ui-monospace': 'Courier New',
}

# Baseline position below the top of a text line, and the line height, as
# fractions of the font size (Arial-like metrics; PowerPoint's single spacing).
ASCENT = 0.905
LINE_HEIGHT = 1.2


def char_width(ch):
    o = ord(ch)
    if 32 <= o <= 126:
        return _W[o - 32] / 1000.0
    if o >= 0x1100 and (0x1100 <= o <= 0x11FF or 0x2E80 <= o <= 0xA4CF or
                        0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
                        0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60 or
                        0x20000 <= o <= 0x3FFFD):
        return 1.0
    return 0.6


def text_width(s, size, bold=False):
    w = sum(char_width(ch) for ch in s) * size
    return w * 1.05 if bold else w


def first_family(value):
    """First family of a CSS font-family list, without quotes."""
    if not value:
        return 'sans-serif'
    for part in value.split(','):
        name = part.strip().strip('"\'').strip()
        if name:
            return name
    return 'sans-serif'


def typeface(value, fonts):
    """Office font name for a CSS font-family value: a name in the fonts
    table (matched without regard to case), else the table's "*" entry,
    else the generic family's Office font, else the name as written."""
    return replace(first_family(value), fonts)


def replace(name, fonts):
    if fonts:
        if name in fonts:
            return fonts[name]
        low = name.lower()
        for k, v in fonts.items():
            if k.lower() == low:
                return v
        if '*' in fonts:
            return fonts['*']
    return GENERIC.get(name.lower(), name)
