# Changes

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
