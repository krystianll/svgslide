# svgslide

Convert an SVG drawing or a PDF into a PowerPoint file (`.pptx`) whose parts
are separate, editable PowerPoint objects: shapes you can recolour and
resize, real text boxes, pictures, and groups that follow the drawing's
groups. Made with scientific figures in mind (Matplotlib, seaborn, ggplot),
but works well for most simple vector images (see Limitations).

Pure Python (3.9+). SVG input needs nothing else; PDF input uses the
optional pypdfium2.

```sh
pip install svgslide          # SVG input
pip install "svgslide[pdf]"   # SVG and PDF input
svgslide figure.svg           # writes figure.pptx
```

## Python

```python
import svgslide

svgslide.convert("figure.svg", "figure.pptx")
svgslide.convert("figure.pdf", "figure.pptx", pages=[1, 3])
svgslide.convert_figure(fig, "figure.pptx")      # a Matplotlib figure
```

Options (both functions):

- `fonts`: replace fonts in the output file, e.g.
  `fonts={"sans-serif": "Arial"}`, or `fonts="Arial"` for every font; in a
  table, `"*"` stands for every font not named.
- `pages` (PDF): which pages to convert; each page becomes its own slide.
- `merge`, `merge_scope`: shapes that look the same become one shape when
  there are more than `merge` of them (default 2000; `0` merges every set,
  `None` never). `merge_scope` says what may merge: `"group"` (default,
  within one group of the drawing), `"touching"` (shapes that overlap or
  touch) or `"figure"` (anywhere). Useful for dense scatter plots and
  genome-browser tracks.
- `precision`: round outline coordinates to a grid in points (e.g. `0.01`)
  for smaller files.
- `format`: `"pptx"` or `"svg"`; a destination ending in `.svg` writes the
  converted drawing as SVG, for editors that do not open `.pptx`.

Each call returns a report with the counts of shapes, text boxes, pictures
and groups, the fonts found, and the SVG or PDF features it could not
convert.

## Command line

```sh
svgslide INPUT... [-o OUTPUT] [--font NAME|OLD=NEW]... [--pages LIST]
         [--merge N|none] [--merge-scope group|touching|figure] [--precision PT]
```

Each input (`.svg`, `.svgz` or `.pdf`) gives a `.pptx` next to it, or
`-o OUTPUT` (`.pptx` or `.svg`) for a single input. `--font sans-serif=Arial`
replaces one font, a bare `--font Arial` every font not named, so
`--font Arial --font "Symbol=Symbol"` keeps Symbol. The fonts found and
anything that could not be converted are printed for each file.

## What is converted

- Shapes and paths, with their colours, transparency, line widths, dashes,
  line ends and gradients; CSS styles; reused symbols and markers.
- Text as text boxes with font, size, weight, colour, alignment and rotation;
  text along a curve becomes one text box per letter.
- Text that a program saved only as letter outlines: Matplotlib and seaborn
  labels become real text again; Cairo labels (R `svg()`, `pdftocairo`)
  become one shape per label.
- Embedded PNG, JPEG and GIF images as pictures; rectangular clipping.
- PDF pages: shapes, text, images and clipping, read with pypdfium2.

## Limitations

- PowerPoint has no clipping for shapes: clipping by a rectangle is applied
  exactly, any other clip shape only approximately.
- Not converted (listed in the report): filters such as blur and drop
  shadows, masks, pattern fills, Inkscape flowed text, animation, external
  image files; in PDFs, smooth shadings and patterns (patterns become their
  average colour), soft masks and blend modes.
- The focal point of radial gradients is ignored.
- Text from Cairo-made files (R `svg()`, `pdftocairo`) cannot become editable
  text: those files hold letter shapes, not characters.
- Letter spacing of text along a curve is estimated, so it can differ a
  little from the original.
- Fonts are not embedded: PowerPoint uses the named font if it is installed,
  otherwise a substitute. Use `fonts=` / `--font` to pick fonts you have.
- Merged shapes keep each point's full outline, so a merged `.pptx` can be
  larger than an unmerged one, although it has far fewer objects.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[pdf]" pytest matplotlib
.venv/bin/python -m pytest
```

Tests needing pypdfium2 or Matplotlib are skipped without them.
`SVGSLIDE_CORPUS=/folder/of/svgs` adds a smoke test over a folder of SVG
files; picture comparison tests run when LibreOffice (`soffice`) and
Inkscape are on `PATH`.

## Licence

MIT. The DrawingML structure follows mpl-office (MIT); see `LICENSE`.
