from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def write_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, height - 54, "QUOTATION")
    pdf.setFont("Helvetica", 10)
    y = height - 86
    for line in lines:
        if stringWidth(line, "Helvetica", 10) > width - 96:
            pdf.setFont("Helvetica", 8)
        else:
            pdf.setFont("Helvetica", 10)
        pdf.drawString(48, y, line)
        y -= 20
    pdf.save()


def main() -> None:
    common = [
        "Quote No: Q-2026-0612-01",
        "Supplier: Shanghai Sample Furniture Co., Ltd.",
        "Customer: Demo Hotel Group",
        "Project: Guest Room Renovation",
        "Date: 2026-06-12",
        "Currency: CNY",
        "No | Code | Product | Specification | Material | Color | Unit | Qty | Unit Price | Amount | Location | Remarks",
    ]
    write_pdf(SAMPLES / "quote-normal.pdf", common + [
        "1 | CH-101 | Dining Chair | 450x520x820 mm | Oak veneer | Natural | pcs | 6 | 1280.00 | 7680.00 | Guest Room | Fire-retardant fabric",
        "2 | TB-201 | Side Table | 500x500x550 mm | Ash wood | Walnut | pcs | 3 | 1560.00 | 4680.00 | Guest Room | Matte finish",
        "Subtotal: 12360.00",
        "Tax: 1606.80",
        "Grand Total: 13966.80",
    ])
    write_pdf(SAMPLES / "quote-anomaly.pdf", common + [
        "1 | CH-101 | Dining Chair | 450x520x820 mm | Oak veneer | Natural | pcs | 6 | 1280.00 | 7000.00 | Guest Room | Amount intentionally wrong",
        "2 | TB-201 | Side Table | 500x500x550 mm | Ash wood | Walnut | pcs | 0 | 1560.00 | 0.00 | Guest Room | Quantity intentionally wrong",
        "Subtotal: 11000.00",
        "Tax: 1606.80",
        "Grand Total: 13000.00",
    ])
    print(SAMPLES)


if __name__ == "__main__":
    main()

