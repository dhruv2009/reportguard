"""PDF helpers: text/table extraction, hidden text detection, page rendering.

White or tiny (<3pt) text is split out into hidden_text instead of being mixed into
the visible text.
"""

from __future__ import annotations

import io
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium


def _normalize_color(color) -> tuple[float, ...] | None:
    if color is None:
        return None
    if isinstance(color, (int, float)):
        color = (color,)
    return tuple(float(c) for c in color if isinstance(c, (int, float)))


def _is_light(color) -> bool:
    c = _normalize_color(color)
    if not c:
        return False
    if len(c) in (1, 3):
        return all(v >= 0.95 for v in c)
    if len(c) == 4:  # CMYK white is (0, 0, 0, 0)
        return all(v <= 0.05 for v in c)
    return False


def _hidden_checker(page):
    # white text on a dark filled rect (table headers) is visible
    dark_fills = [r for r in page.rects if r.get("fill") and not _is_light(r.get("non_stroking_color"))]

    def on_dark_fill(ch: dict) -> bool:
        cx, cy = (ch["x0"] + ch["x1"]) / 2, (ch["top"] + ch["bottom"]) / 2
        return any(r["x0"] <= cx <= r["x1"] and r["top"] <= cy <= r["bottom"] for r in dark_fills)

    def is_hidden(obj: dict) -> bool:
        if obj.get("object_type") != "char":
            return False
        if (obj.get("size") or 12) < 3:
            return True
        return _is_light(obj.get("non_stroking_color")) and not on_dark_fill(obj)

    return is_hidden


def extract_pdf(path: str | Path) -> dict:
    pages, hidden = [], []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            is_hidden = _hidden_checker(page)
            hidden_chars = [c for c in page.chars if is_hidden(c)]
            if hidden_chars:
                text = "".join(c["text"] for c in hidden_chars)
                hidden.append({"page": i, "text": text[:500], "char_count": len(hidden_chars),
                               "reason": "white or sub-3pt text that a human reader cannot see"})
            visible = page.filter(lambda o: not is_hidden(o))
            tables = [[[cell if cell is not None else "" for cell in row] for row in table]
                      for table in visible.extract_tables()]
            pages.append({"page": i, "text": visible.extract_text() or "", "tables": tables})
    return {"page_count": len(pages), "pages": pages, "hidden_text": hidden}


def pdf_page_count(path: str | Path) -> int:
    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


def render_pdf_page(path: str | Path, page: int = 1, scale: float = 1.6) -> bytes:
    doc = pdfium.PdfDocument(str(path))
    try:
        if not 1 <= page <= len(doc):
            raise ValueError(f"page must be between 1 and {len(doc)}")
        image = doc[page - 1].render(scale=scale).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()
