"""Gets plain text out of an uploaded contract file (.pdf, .txt or .md)."""
import io

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_CHARS = 100_000


class ContractReadError(ValueError):
    pass


def read_contract(filename, data):
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except PyPdfError as e:
            raise ContractReadError(f"could not read the PDF: {e}")
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ContractReadError("only .pdf, .txt and .md files are supported")

    text = text.strip()
    if not text:
        raise ContractReadError("no text found in the file (a scanned PDF needs OCR first)")
    if len(text) > MAX_CHARS:
        raise ContractReadError(f"contract is too long ({len(text)} characters, limit {MAX_CHARS})")
    return text
