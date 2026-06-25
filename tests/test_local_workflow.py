from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quote_assistant.deepseek_agent import quote_from_agent_payload
from quote_assistant.local_workflow import LocalQuoteWorkflow
from quote_assistant.ocr import OcrDocument, extract_markdown, parse_jsonl
from quote_assistant.service import QuoteService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeOcrEngine:
    def parse_local_file(self, file_path: Path) -> OcrDocument:
        jsonl_text = json.dumps(
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": (
                                    "Quote No: Q-LOCAL-001\n"
                                    "Supplier: Local Supplier\n"
                                    "Currency: CNY\n"
                                    "1 | SKU-1 | Chair | Wood | Oak | Natural | pcs | 2 | 100 | 200"
                                )
                            }
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
        rows = parse_jsonl(jsonl_text)
        return OcrDocument(
            job_id="ocrjob-local-test",
            model="PaddleOCR-VL",
            markdown_text=extract_markdown(rows),
            jsonl_text=jsonl_text,
            raw_lines=rows,
            result_url="https://example.invalid/result.jsonl",
            metadata={"test": True},
        )


class FakeExtractionAgent:
    def extract_quote(self, markdown_text: str, *, source_name: str, ocr_job_id: str = "") -> dict:
        payload = {
            "headers": {
                "quote_no": "Q-LOCAL-001",
                "supplier": "Local Supplier",
                "customer": "Local Customer",
                "project": "Local Project",
                "quote_date": "2026-06-25",
                "currency": "CNY",
            },
            "items": [
                {
                    "product_code": "SKU-1",
                    "product_name": "Chair",
                    "specification": "Wood",
                    "material": "Oak",
                    "color": "Natural",
                    "unit": "pcs",
                    "quantity": 2,
                    "unit_price": 100,
                    "amount": 200,
                    "location": None,
                    "remarks": None,
                }
            ],
            "totals": {"subtotal": 200, "tax": 0, "grand_total": 200},
            "notes": [],
        }
        return quote_from_agent_payload(payload, source_name=source_name, ocr_job_id=ocr_job_id)


def create_service(root: Path) -> QuoteService:
    (root / "templates").mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "confidence_threshold": 0.8,
                "critical_confidence_threshold": 0.9,
                "amount_tolerance": 0.02,
                "require_manual_approval": True,
                "alert_on_severities": ["error", "critical"],
                "critical_fields": ["supplier", "quote_no", "product_name", "quantity", "unit_price", "amount"],
                "excel_template_mapping": "templates/template_mapping.json",
            }
        ),
        encoding="utf-8",
    )
    (root / "templates" / "template_mapping.json").write_text(json.dumps({"configured": False}), encoding="utf-8")
    return QuoteService(root)


class LocalWorkflowTests(unittest.TestCase):
    def test_jsonl_markdown_extraction_supports_layout_results(self):
        jsonl_text = '{"result":{"layoutParsingResults":[{"markdown":{"text":"A"}}]}}\n'
        rows = parse_jsonl(jsonl_text)
        self.assertEqual("A", extract_markdown(rows))

    def test_agent_payload_normalizes_to_existing_quote_schema(self):
        quote = quote_from_agent_payload(
            {
                "headers": {"quote_no": "Q1", "supplier": "S", "currency": "CNY"},
                "items": [{"product_name": "Chair", "quantity": "2", "unit_price": "10.5", "amount": "21"}],
                "totals": {"subtotal": "21", "tax": "0", "grand_total": "21"},
            },
            source_name="source.pdf",
            ocr_job_id="ocrjob-1",
        )
        self.assertEqual("paddleocr+deepseek-agent-v1", quote["document"]["parser"])
        self.assertEqual(2.0, quote["items"][0]["quantity"]["value"])
        self.assertEqual("deepseek_agent", quote["headers"]["quote_no"]["source"]["type"])

    def test_local_workflow_creates_job_and_ocr_artifacts_without_cloud_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = create_service(root)
            workflow = LocalQuoteWorkflow(service, FakeOcrEngine(), FakeExtractionAgent())
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"

            result = workflow.run(source, reviewer="Tester")
            self.assertEqual("ready_for_review", result.job["status"])
            self.assertEqual("ocrjob-local-test", result.job["ocr"]["job_id"])
            self.assertTrue((result.ocr_artifact_dir / "ocr.md").is_file())
            self.assertTrue((result.ocr_artifact_dir / "ocr.jsonl").is_file())
            self.assertNotIn("google", json.dumps(result.job, ensure_ascii=False).lower())
            self.assertFalse(result.output_path)

    def test_cli_acceptance_command_returns_json(self):
        completed = subprocess.run(
            [sys.executable, "-m", "quote_assistant.cli", "--project-root", str(PROJECT_ROOT), "acceptance"],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("report", payload)


if __name__ == "__main__":
    unittest.main()
