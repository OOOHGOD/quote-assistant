from __future__ import annotations

import json
import hashlib
import hmac
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from quote_assistant.service import QuoteService
from quote_assistant.template_export import TemplateExportError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_activation_template(path: Path) -> None:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="报价单" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"""
    cells = []
    for address in ["B1", "B2", "B3", "B4", "B5", "B6", "A8", "B8", "C8", "D8", "E8", "A9", "B9", "C9", "D9", "E9", "B11", "B12", "B13"]:
        cells.append(f'<c r="{address}" s="1" t="inlineStr"><is><t>预留</t></is></c>')
    sheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">{''.join(cells)}</row></sheetData><pageSetup orientation="landscape"/></worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/styles.xml", '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')


def activation_mapping(template_file: str, digest: str) -> dict:
    return {
        "configured": False,
        "review_required": True,
        "template_file": template_file,
        "template_sha256": digest,
        "sheet_name": "报价单",
        "header_cells": {
            "quote.headers.quote_no.value": "B1", "quote.headers.supplier.value": "B2",
            "quote.headers.customer.value": "B3", "quote.headers.project.value": "B4",
            "quote.headers.quote_date.value": "B5", "quote.headers.currency.value": "B6",
        },
        "items": {
            "path": "quote.items", "start_row": 8, "max_rows": 2,
            "columns": {"line_no": "A", "product_name.value": "B", "quantity.value": "C", "unit_price.value": "D", "amount.value": "E"},
        },
        "total_cells": {
            "quote.totals.subtotal.value": "B11", "quote.totals.tax.value": "B12", "quote.totals.grand_total.value": "B13",
        },
        "clear_unused_item_rows": True,
    }


class ServiceAlertTests(unittest.TestCase):
    def create_service(self, root: Path) -> QuoteService:
        (root / "templates").mkdir(parents=True)
        (root / "config.json").write_text(json.dumps({
            "confidence_threshold": 0.8,
            "critical_confidence_threshold": 0.9,
            "amount_tolerance": 0.02,
            "require_manual_approval": True,
            "alert_on_severities": ["error", "critical"],
            "critical_fields": ["supplier", "quote_no", "product_name", "quantity", "unit_price", "amount"],
            "excel_template_mapping": "templates/template_mapping.json",
        }), encoding="utf-8")
        (root / "templates" / "template_mapping.json").write_text(json.dumps({"configured": False}), encoding="utf-8")
        return QuoteService(root)

    def test_export_block_creates_alert_history(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            service.review_job(job["id"], {"action": "approve", "reviewer": "Tester"})
            with self.assertRaises(TemplateExportError):
                service.export_job(job["id"])
            saved = service.store.get(job["id"])
            self.assertEqual("quote_export_blocked", saved["alerts"][-1]["payload"]["event"])
            self.assertTrue((service.store.job_dir(job["id"]) / "alert.json").is_file())

    def test_pdf_upload_signature_and_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            with self.assertRaisesRegex(ValueError, "有效的PDF"):
                service.create_job("fake.pdf", b"not a pdf")
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            content = source.read_bytes()
            job = service.create_job(source.name, content)
            self.assertEqual(hashlib.sha256(content).hexdigest(), job["source"]["sha256"])
            self.assertEqual(len(content), job["source"]["size_bytes"])
            self.assertEqual(service.store.job_dir(job["id"]) / "source.pdf", service.source_document(job["id"]))

    def test_failed_approval_creates_second_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            source = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"
            job = service.create_job(source.name, source.read_bytes())
            self.assertEqual(1, len(job["alerts"]))
            with self.assertRaises(ValueError):
                service.review_job(job["id"], {"action": "approve", "reviewer": "Tester"})
            saved = service.store.get(job["id"])
            self.assertEqual(2, len(saved["alerts"]))
            self.assertEqual("quote_approval_blocked", saved["alerts"][-1]["payload"]["event"])

    def test_webhook_alert_is_signed_and_delivery_is_persisted(self):
        class WebhookHandler(BaseHTTPRequestHandler):
            bodies = []
            signatures = []

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.__class__.bodies.append(body)
                self.__class__.signatures.append(self.headers.get("X-Quote-Alert-Signature"))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), WebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "ALERT_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}/alerts",
                "ALERT_WEBHOOK_SECRET": "test-secret",
                "ALERT_WEBHOOK_TIMEOUT_SECONDS": "1",
            }):
                service = self.create_service(Path(directory))
                source = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"
                job = service.create_job(source.name, source.read_bytes())
                delivery = job["alert"]["delivery"]
                self.assertEqual("webhook", delivery["channel"])
                self.assertTrue(delivery["success"])
                self.assertEqual(1, delivery["attempts"])
                expected = hmac.new(b"test-secret", WebhookHandler.bodies[0], hashlib.sha256).hexdigest()
                self.assertEqual(f"sha256={expected}", WebhookHandler.signatures[0])
                record = json.loads((service.store.job_dir(job["id"]) / "alert.json").read_text(encoding="utf-8"))
                self.assertTrue(record["delivery"]["success"])
                self.assertEqual("quote_recognition_anomaly", record["payload"]["event"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_failed_webhook_can_be_retried_without_changing_job_revision(self):
        class WebhookHandler(BaseHTTPRequestHandler):
            status = 503
            request_count = 0

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.__class__.request_count += 1
                self.send_response(self.__class__.status)
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), WebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "ALERT_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}/alerts",
                "ALERT_WEBHOOK_TIMEOUT_SECONDS": "1",
                "ALERT_RETRY_BASE_SECONDS": "1",
            }):
                service = self.create_service(Path(directory))
                source = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"
                job = service.create_job(source.name, source.read_bytes())
                first_delivery = job["alert"]["delivery"]
                self.assertFalse(first_delivery["success"])
                self.assertEqual(503, first_delivery["status"])
                self.assertEqual(1, first_delivery["attempts"])
                self.assertIn("next_retry_at", first_delivery)
                revision = job["revision"]

                WebhookHandler.status = 204
                retried = service.retry_alerts(job["id"], force=True)
                delivery = retried["alert"]["delivery"]
                self.assertTrue(delivery["success"])
                self.assertEqual(2, delivery["attempts"])
                self.assertNotIn("next_retry_at", delivery)
                self.assertEqual(revision, retried["revision"])
                self.assertEqual(2, WebhookHandler.request_count)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_template_import_preserves_bytes_and_keeps_export_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            template = root / "source.xlsx"
            with zipfile.ZipFile(template, "w") as archive:
                archive.writestr("xl/workbook.xml", """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="报价单" sheetId="1" r:id="rId1"/></sheets></workbook>""")
                archive.writestr("xl/_rels/workbook.xml.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>""")
                archive.writestr("xl/worksheets/sheet1.xml", """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>报价编号</t></is></c><c r="B1" t="inlineStr"><is><t>待填</t></is></c></row></sheetData></worksheet>""")
            original = template.read_bytes()
            result = service.import_template("source.xlsx", original)
            stored = Path(result["stored_path"])
            self.assertEqual(original, stored.read_bytes())
            self.assertFalse(result["configured"])
            self.assertTrue(result["review_required"])
            draft = json.loads(Path(result["draft_mapping_path"]).read_text(encoding="utf-8"))
            self.assertFalse(draft["configured"])
            self.assertEqual("B1", draft["header_cells"]["quote.headers.quote_no.value"])
            self.assertFalse(service.template_status()["configured"])

    def test_review_history_records_changes_and_source_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            source = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"
            job = service.create_job(source.name, source.read_bytes())
            item_rows = []
            for index, item in enumerate(job["quote"]["items"]):
                values = {name: candidate.get("value") for name, candidate in item.items() if isinstance(candidate, dict)}
                if index == 0:
                    values["amount"] = 7680.0
                if index == 1:
                    values["quantity"] = 3
                    values["amount"] = 4680.0
                item_rows.append({"original_index": index, "values": values})
            saved = service.review_job(job["id"], {
                "action": "save",
                "reviewer": "Reviewer A",
                "note": "对照PDF修正",
                "corrections": {"totals.subtotal": 12360.0, "totals.grand_total": 13966.8},
                "item_rows": item_rows,
                "human_verified_source": True,
            })
            self.assertEqual("ready_for_review", saved["status"])
            self.assertEqual("Reviewer A", saved["review_history"][-1]["reviewer"])
            self.assertTrue(saved["review_history"][-1]["changed_paths"])
            self.assertTrue(saved["review_history"][-1]["human_verified_source"])

    def test_scanned_pdf_manual_takeover_requires_verification_then_approves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            scan_path = root / "scan.pdf"
            pdf = canvas.Canvas(str(scan_path), pagesize=A4)
            pdf.rect(50, 650, 400, 100)
            pdf.save()
            job = service.create_job(scan_path.name, scan_path.read_bytes())
            self.assertEqual("needs_review", job["status"])
            payload = {
                "action": "save",
                "reviewer": "Manual Reviewer",
                "corrections": {
                    "headers.quote_no": "Q-SCAN-01", "headers.supplier": "Manual Supplier", "headers.currency": "CNY",
                    "totals.subtotal": 100.0, "totals.tax": 0.0, "totals.grand_total": 100.0,
                },
                "item_rows": [{"original_index": None, "values": {
                    "product_name": "Manual Chair", "quantity": 1, "unit_price": 100, "amount": 100,
                    "product_code": "", "specification": "", "material": "", "color": "", "unit": "pcs",
                    "location": "", "remarks": "",
                }}],
                "human_verified_source": False,
            }
            saved = service.review_job(job["id"], payload)
            self.assertEqual("needs_review", saved["status"])
            payload["human_verified_source"] = True
            verified = service.review_job(job["id"], payload)
            self.assertEqual("ready_for_review", verified["status"])
            approved = service.review_job(job["id"], {"action": "approve", "reviewer": "Manual Reviewer"})
            self.assertEqual("approved", approved["status"])
            self.assertTrue(approved["quote"]["human_source_verification"]["verified"])
            self.assertEqual(approved["source"]["sha256"], approved["quote"]["human_source_verification"]["source_sha256"])

    def test_edit_after_approval_invalidates_old_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            job = service.review_job(job["id"], {"action": "approve", "reviewer": "Tester"})
            old_export = root / "output" / "old.xlsx"
            old_export.parent.mkdir(parents=True)
            old_export.write_bytes(b"old")
            job["export"] = {"path": str(old_export), "generated_at": "old"}
            service.store.save(job)
            edited = service.review_job(job["id"], {
                "action": "save", "reviewer": "Tester", "corrections": {"headers.project": "Changed Project"}
            })
            self.assertIsNone(edited["export"])
            self.assertFalse(old_export.exists())
            self.assertEqual("ready_for_review", edited["status"])

    def test_template_mapping_activation_requires_confirmation_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            source = root / "activation.xlsx"
            create_activation_template(source)
            imported = service.import_template(source.name, source.read_bytes())
            mapping = activation_mapping(imported["template_file"], imported["template_sha256"])

            with self.assertRaisesRegex(ValueError, "必须确认"):
                service.activate_template_mapping({"reviewer": "Template Reviewer", "confirm_format_immutable": False, "mapping": mapping})
            bad_hash_mapping = dict(mapping)
            bad_hash_mapping["template_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "哈希"):
                service.activate_template_mapping({"reviewer": "Template Reviewer", "confirm_format_immutable": True, "mapping": bad_hash_mapping})
            self.assertFalse(service.template_status()["configured"])

            result = service.activate_template_mapping({
                "reviewer": "Template Reviewer",
                "confirm_format_immutable": True,
                "template_sha256": imported["template_sha256"],
                "mapping": mapping,
            })
            self.assertTrue(result["configured"])
            self.assertEqual("Template Reviewer", result["reviewer"])
            self.assertTrue(service.template_status()["configured"])
            formal = json.loads((root / "templates" / "template_mapping.json").read_text(encoding="utf-8"))
            self.assertTrue(formal["configured"])
            history = json.loads((root / "templates" / "template_activation_history.json").read_text(encoding="utf-8"))
            self.assertEqual("Template Reviewer", history[-1]["reviewer"])

    def test_activated_mapping_exports_approved_quote_without_structure_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            template = root / "activation.xlsx"
            create_activation_template(template)
            imported = service.import_template(template.name, template.read_bytes())
            mapping = activation_mapping(imported["template_file"], imported["template_sha256"])
            service.activate_template_mapping({
                "reviewer": "Template Reviewer", "confirm_format_immutable": True, "mapping": mapping,
            })
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            approved = service.review_job(job["id"], {"action": "approve", "reviewer": "Quote Reviewer"})
            output = service.export_job(approved["id"])
            self.assertTrue(output.is_file())
            saved = service.store.get(approved["id"])
            self.assertTrue(saved["export"]["template_audit"]["structure_unchanged"])
            self.assertEqual(imported["template_sha256"], saved["export"]["template_audit"]["template_sha256"])
            with zipfile.ZipFile(template) as original, zipfile.ZipFile(output) as generated:
                self.assertEqual(set(original.namelist()), set(generated.namelist()))

    def test_source_tampering_invalidates_review_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            job = service.review_job(job["id"], {"action": "approve", "reviewer": "Reviewer"})
            old_export = root / "output" / "old.xlsx"
            old_export.parent.mkdir(parents=True)
            old_export.write_bytes(b"old")
            job["export"] = {"path": str(old_export)}
            service.store.save(job)
            (service.store.job_dir(job["id"]) / "source.pdf").write_bytes(b"%PDF-tampered")

            with self.assertRaisesRegex(ValueError, "哈希不一致"):
                service.source_document(job["id"])
            saved = service.store.get(job["id"])
            self.assertEqual("needs_review", saved["status"])
            self.assertIsNone(saved["export"])
            self.assertFalse(old_export.exists())
            self.assertEqual("quote_source_integrity_failure", saved["alert"]["payload"]["event"])
            alert_count = len(saved["alerts"])
            with self.assertRaises(ValueError):
                service.source_document(job["id"])
            self.assertEqual(alert_count, len(service.store.get(job["id"])["alerts"]))

    def test_stale_reviewer_cannot_overwrite_newer_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            self.assertEqual(1, job["revision"])
            first = service.review_job(job["id"], {
                "action": "save", "reviewer": "Reviewer A", "note": "first", "expected_revision": 1,
            })
            self.assertEqual(2, first["revision"])
            with self.assertRaisesRegex(ValueError, "其他审核员更新"):
                service.review_job(job["id"], {
                    "action": "save", "reviewer": "Reviewer B", "note": "stale", "expected_revision": 1,
                })
            saved = service.store.get(job["id"])
            self.assertEqual(2, saved["revision"])
            self.assertEqual(1, len(saved["review_history"]))
            self.assertEqual("Reviewer A", saved["review_history"][0]["reviewer"])
            self.assertFalse((service.store.job_dir(job["id"]) / ".job.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
