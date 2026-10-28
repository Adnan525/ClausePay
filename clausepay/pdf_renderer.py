"""Turns a simple document description into a PDF.

A document is a title plus a list of blocks:
    {"type": "heading", "text": "..."}
    {"type": "paragraph", "text": "..."}
    {"type": "bullets", "items": ["...", "..."]}
    {"type": "table", "header": [...], "rows": [[...], ...]}
The report builder produces this shape, so it never needs to know about reportlab.
"""
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


class PdfRenderer:
    def __init__(self):
        self.styles = getSampleStyleSheet()

    def render(self, title, blocks, path):
        """Write the PDF to `path` and return the path."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        doc = SimpleDocTemplate(path, pagesize=A4, title=title,
                                leftMargin=20 * mm, rightMargin=20 * mm,
                                topMargin=20 * mm, bottomMargin=20 * mm)
        story = [Paragraph(escape(title), self.styles["Title"])]
        for block in blocks:
            story.extend(self._block(block))
        doc.build(story)
        return path

    def _block(self, block):
        kind = block["type"]
        if kind == "heading":
            return [Paragraph(escape(block["text"]), self.styles["Heading2"])]
        if kind == "paragraph":
            return [Paragraph(escape(block["text"]), self.styles["BodyText"]), Spacer(1, 3 * mm)]
        if kind == "bullets":
            items = [Paragraph(escape(i), self.styles["BodyText"]) for i in block["items"]]
            return [ListFlowable(items, bulletType="bullet"), Spacer(1, 3 * mm)]
        if kind == "table":
            return [self._table(block), Spacer(1, 4 * mm)]
        raise ValueError(f"unknown block type {kind!r}")

    def _table(self, block):
        cell = self.styles["BodyText"]
        header = [Paragraph(f"<b>{escape(str(h))}</b>", cell) for h in block["header"]]
        rows = [[Paragraph(escape(str(c)), cell) for c in row] for row in block["rows"]]
        table = Table([header] + rows, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return table
