"""Generate only the frozen fictional Phase 13 smoke document; no AWS I/O."""
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "fictional-storage-note.pdf"
LINES = (
    "Synthetic portfolio exercise.",
    "Northbridge Archive Ltd provides fictional storage services to",
    "Acme Orchard Ltd. Acme Orchard Ltd must pay Northbridge Archive Ltd",
    "within 23 calendar days after receipt of an invoice.",
)


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, invariant=1)
    pdf.setTitle("Fictional storage note")
    pdf.setAuthor("LegalDesk synthetic portfolio fixture")
    pdf.setFillColor(HexColor("#233947"))
    pdf.setFont("Times-Bold", 24)
    pdf.drawString(54, 770, "Fictional storage note")
    pdf.setStrokeColor(HexColor("#d8d0c3"))
    pdf.line(54, 747, 540, 747)
    pdf.setFont("Times-Roman", 12)
    for index, line in enumerate(LINES):
        pdf.drawString(54, 704 - index * 22, line)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(54, 54, "LEGALDESK / SYNTHETIC DATA ONLY")
    pdf.save()
    reader = PdfReader(OUTPUT)
    extracted = " ".join(reader.pages[0].extract_text().split())
    assert len(reader.pages) == 1 and len(extracted) <= 2000
    assert "within 23 calendar days after receipt of an invoice." in extracted
    assert OUTPUT.stat().st_size <= 100 * 1024
    print(json.dumps({"pages": 1, "bytes": OUTPUT.stat().st_size, "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
