from __future__ import annotations

import json
import unittest
from pathlib import Path

from quote_assistant.parser import parse_pdf
from quote_assistant.models import field, issue
from quote_assistant.validation import apply_corrections, apply_item_rows, resolve_extraction_issues, validate_quote


ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_normal_quote_is_ready_for_review(self):
        quote = parse_pdf(ROOT / "samples" / "quote-normal.pdf")
        result = validate_quote(quote, self.config)
        self.assertEqual(2, len(quote["items"]))
        self.assertEqual(13966.8, quote["totals"]["grand_total"]["value"])
        self.assertEqual("ready_for_review", result["decision"])
        self.assertEqual(0, result["blocking_issue_count"])

    def test_anomalies_block_approval(self):
        quote = parse_pdf(ROOT / "samples" / "quote-anomaly.pdf")
        result = validate_quote(quote, self.config)
        codes = {entry["code"] for entry in result["issues"]}
        self.assertIn("AMOUNT_MISMATCH", codes)
        self.assertIn("INVALID_QUANTITY", codes)
        self.assertIn("GRAND_TOTAL_MISMATCH", codes)
        self.assertEqual("needs_review", result["decision"])

    def test_human_correction_sets_full_confidence(self):
        quote = parse_pdf(ROOT / "samples" / "quote-anomaly.pdf")
        corrected = apply_corrections(quote, {"items.0.amount": 7680.0, "items.1.quantity": 3, "items.1.amount": 4680.0, "totals.subtotal": 12360.0, "totals.grand_total": 13966.8})
        result = validate_quote(corrected, self.config)
        self.assertEqual(1.0, corrected["items"][0]["amount"]["confidence"])
        self.assertEqual(0, result["blocking_issue_count"])

    def test_manual_rows_preserve_source_and_mark_new_values(self):
        quote = parse_pdf(ROOT / "samples" / "quote-normal.pdf")
        original_source = quote["items"][0]["product_name"]["source"]
        rows = [
            {
                "original_index": 0,
                "values": {name: candidate.get("value") for name, candidate in quote["items"][0].items() if isinstance(candidate, dict)},
            },
            {
                "original_index": None,
                "values": {
                    "product_code": "LP-01", "product_name": "Floor Lamp", "specification": "H1600",
                    "material": "Metal", "color": "Black", "unit": "pcs", "quantity": 2,
                    "unit_price": 500, "amount": 1000, "location": "Lobby", "remarks": "Manual",
                },
            },
        ]
        corrected = apply_item_rows(quote, rows)
        self.assertEqual(original_source, corrected["items"][0]["product_name"]["source"])
        self.assertEqual(1.0, corrected["items"][1]["product_name"]["confidence"])
        self.assertEqual("manual_review", corrected["items"][1]["product_name"]["source"]["type"])
        self.assertEqual([1, 2], [item["line_no"] for item in corrected["items"]])

    def test_scanned_document_requires_explicit_source_verification(self):
        quote = {
            "headers": {name: field() for name in ["quote_no", "supplier", "customer", "project", "quote_date", "currency"]},
            "items": [],
            "totals": {name: field() for name in ["subtotal", "tax", "grand_total"]},
            "extraction_issues": [issue("SCANNED_OR_EMPTY_PDF", "critical", "需要人工核验", "document")],
        }
        quote = apply_corrections(quote, {
            "headers.quote_no": "Q-SCAN", "headers.supplier": "Manual Supplier", "headers.currency": "CNY",
            "totals.subtotal": 100, "totals.tax": 0, "totals.grand_total": 100,
        })
        quote = apply_item_rows(quote, [{"original_index": None, "values": {
            "product_name": "Manual Chair", "quantity": 1, "unit_price": 100, "amount": 100,
        }}])
        self.assertEqual("needs_review", validate_quote(quote, self.config)["decision"])
        verified = resolve_extraction_issues(quote, "Tester", "2026-06-12T10:00:00+00:00", "abc123")
        result = validate_quote(verified, self.config)
        self.assertEqual("ready_for_review", result["decision"])
        self.assertEqual(0, result["blocking_issue_count"])
        self.assertEqual("abc123", verified["human_source_verification"]["source_sha256"])


if __name__ == "__main__":
    unittest.main()
