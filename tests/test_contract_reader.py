import pytest
from reportlab.pdfgen import canvas

from clausepay.contract_reader import ContractReadError, read_contract


def test_reads_text_file():
    assert read_contract("deal.txt", b"  60/40 split \n") == "60/40 split"


def test_reads_pdf(tmp_path):
    path = tmp_path / "deal.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 750, "Alice gets 60 percent")
    c.save()
    assert "Alice gets 60 percent" in read_contract("deal.pdf", path.read_bytes())


@pytest.mark.parametrize("name,data", [("deal.docx", b"x"), ("deal.txt", b"   "), ("deal.pdf", b"not a pdf")])
def test_rejects_bad_files(name, data):
    with pytest.raises(ContractReadError):
        read_contract(name, data)
