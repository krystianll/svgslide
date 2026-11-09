# Changes

## 0.2.0

- `scale=` / `--scale`: make the whole drawing larger or smaller, text,
  lines and pictures included.
- `width=`, `height=` / `--width`, `--height`: set the figure's final size,
  e.g. `20cm`; the other side follows the figure's shape.
- `canvas_size=` / `--canvas-size`: put the figure in the middle of a slide
  of a given size (`widescreen`, `4:3`, `A4`, `A4-portrait` or e.g.
  `25cm x 14cm`); a larger figure is shrunk to fit.
- Several drawings or PDFs into one deck: `convert(["a.svg", "b.pdf"], ...)`,
  `svgslide a.svg b.pdf -o talk.pptx`.
- The report's new `notes` say when a figure was shrunk to fit, also for
  PowerPoint's 56-inch slide limit; the command line prints them.

## 0.1.0

First release.

- Converts SVG drawings and PDF pages into PowerPoint (`.pptx`) files whose
  shapes, text and pictures are separate, editable objects; can also write the
  converted drawing as SVG for other editors.
- Python API (`convert`, and `convert_figure` for Matplotlib figures) and a
  `svgslide` command-line tool.
- Optional merging of identical shapes and coordinate rounding for large
  plots (`merge`, `merge_scope`, `precision`).
- Replace fonts in the output file (e.g. `fonts={"sans-serif": "Arial"}`,
  `--font sans-serif=Arial` or `fonts="Arial"`, `--font Arial`).
- PDF input needs `pip install "svgslide[pdf]"` (pypdfium2).
