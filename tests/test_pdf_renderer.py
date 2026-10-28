import pytest

from clausepay.pdf_renderer import PdfRenderer

BLOCKS = [
    {"type": "heading", "text": "Summary"},
    {"type": "paragraph", "text": "Fees & holdbacks <10%> were applied."},
    {"type": "bullets", "items": ["Alice 60%", "Bob 40%"]},
    {"type": "table", "header": ["Payee", "Amount"], "rows": [["Alice", "$156.00"], ["Bob", "$384.00"]]},
]


def test_render_writes_a_pdf(tmp_path):
    path = PdfRenderer().render("Week 2 report", BLOCKS, str(tmp_path / "out" / "report.pdf"))
    with open(path, "rb") as f:
        data = f.read()
    assert data.startswith(b"%PDF")
    assert len(data) > 1000


def test_unknown_block_rejected(tmp_path):
    with pytest.raises(ValueError):
        PdfRenderer().render("x", [{"type": "chart"}], str(tmp_path / "x.pdf"))
