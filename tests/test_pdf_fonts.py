"""Document PDFs render with fonts that ship inside the celerp package, so they
work on every platform the app runs on, not only where a system font happens to
sit at a Linux path."""

from pathlib import Path

from reportlab.pdfbase import pdfmetrics

import celerp
from celerp.output import pdf


def test_document_pdf_fonts_ship_with_celerp():
    package = Path(celerp.__file__).resolve().parent
    for name in (pdf._FONT, pdf._FONT_BOLD):
        path = Path(pdfmetrics.getFont(name).face.filename).resolve()
        assert path.is_file() and path.is_relative_to(package), f"{name} is read from {path}"
