from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

import app as app_module
from quote_assistant.service import QuoteService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_activation_template(path: Path) -> None:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="报价单" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"""
    cells = []
    for address in ["B1", "B2", "B3", "B4", "B5", "B6", "A8", "B8", "C8", "D8", "E8", "A9", "B9", "C9", "D9", "E9", "B11", "B12", "B13"]:
        cells.append(f'<c r="{address}" s="1" t="inlineStr"><is><t>预留</t></is></c>')
    sheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">{''.join(cells)}</row></sheetData><pageSetup orientation="landscape"/></worksheet>"""
    import zipfile

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
            "quote.headers.quote_no.value": "B1",
            "quote.headers.supplier.value": "B2",
            "quote.headers.customer.value": "B3",
            "quote.headers.project.value": "B4",
            "quote.headers.quote_date.value": "B5",
            "quote.headers.currency.value": "B6",
        },
        "items": {
            "path": "quote.items",
            "start_row": 8,
            "max_rows": 2,
            "columns": {
                "line_no": "A",
                "product_name.value": "B",
                "quantity.value": "C",
                "unit_price.value": "D",
                "amount.value": "E",
            },
        },
        "total_cells": {
            "quote.totals.subtotal.value": "B11",
            "quote.totals.tax.value": "B12",
            "quote.totals.grand_total.value": "B13",
        },
        "clear_unused_item_rows": True,
    }


class AppEndpointTests(unittest.TestCase):
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
            "alert_retry_interval_seconds": 30,
        }), encoding="utf-8")
        (root / "templates" / "template_mapping.json").write_text(json.dumps({"configured": False}), encoding="utf-8")
        return QuoteService(root)

    def seed_sample_inputs(self, root: Path) -> None:
        samples_dir = root / "samples"
        samples_dir.mkdir(parents=True, exist_ok=True)
        for name in ("quote-normal.pdf", "quote-anomaly.pdf"):
            (samples_dir / name).write_bytes((PROJECT_ROOT / "samples" / name).read_bytes())

    @contextlib.contextmanager
    def running_server(self, service: QuoteService):
        original_service = app_module.SERVICE
        original_static_root = app_module.STATIC_ROOT
        app_module.SERVICE = service
        app_module.STATIC_ROOT = PROJECT_ROOT / "static"
        server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            app_module.SERVICE = original_service
            app_module.STATIC_ROOT = original_static_root

    def json_request(self, url: str, payload: dict, *, method: str = "POST"):
        req = request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def multipart_request(self, url: str, *, field_name: str, filename: str, content: bytes, content_type: str):
        boundary = "----CodexQuoteAssistantBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_public_job_and_source_endpoint_do_not_leak_internal_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            source = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"
            job = service.create_job(source.name, source.read_bytes())

            with self.running_server(service) as base_url:
                with request.urlopen(f"{base_url}/api/jobs/{job['id']}") as response:
                    self.assertEqual(200, response.status)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertNotIn("source_path", payload)
                self.assertNotIn("raw_pages", payload["quote"])
                self.assertNotIn("local_path", json.dumps(payload, ensure_ascii=False))
                self.assertNotIn("data/jobs", json.dumps(payload, ensure_ascii=False))

                with request.urlopen(f"{base_url}/api/jobs/{job['id']}/source") as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("application/pdf", response.headers.get_content_type())
                    self.assertEqual('inline; filename="source.pdf"', response.headers["Content-Disposition"])
                    self.assertEqual("no-store", response.headers["Cache-Control"])
                    self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
                    self.assertTrue(response.read().startswith(b"%PDF-"))

    def test_upload_prefers_ocr_workflow_when_credentials_are_available(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(Path(directory))
            original_service = app_module.SERVICE
            original_builder = app_module.build_default_workflow
            original_token = os.environ.get("PADDLEOCR_TOKEN")
            original_deepseek = os.environ.get("DEEPSEEK_API_KEY")
            calls: list[dict] = []

            class FakeResult:
                def __init__(self, job: dict):
                    self.job = job

            class FakeWorkflow:
                def run(self, pdf_path: Path, *, reviewer: str = "Local Workflow", approve: bool = False, export: bool = False):
                    calls.append({"pdf_path": pdf_path, "reviewer": reviewer, "approve": approve, "export": export})
                    return FakeResult(
                        {
                            "id": "web-ocr-job",
                            "source_file": pdf_path.name,
                            "status": "ready_for_review",
                            "validation": {"blocking_issue_count": 0, "warning_count": 0},
                            "quote": {},
                            "alerts": [],
                            "ocr": {"provider": "paddleocr", "job_id": "web-ocr-1"},
                            "agent": {"provider": "deepseek"},
                        }
                    )

            try:
                app_module.SERVICE = service
                app_module.build_default_workflow = lambda project_root: FakeWorkflow()
                os.environ["PADDLEOCR_TOKEN"] = "test-token"
                os.environ["DEEPSEEK_API_KEY"] = "test-key"

                job = app_module.create_uploaded_job("supplier-quote.pdf", (PROJECT_ROOT / "samples" / "quote-normal.pdf").read_bytes())

                self.assertEqual("web-ocr-job", job["id"])
                self.assertEqual("supplier-quote.pdf", job["source_file"])
                self.assertEqual("paddleocr", job["ocr"]["provider"])
                self.assertEqual(1, len(calls))
                self.assertEqual("HTTP Upload", calls[0]["reviewer"])
            finally:
                app_module.SERVICE = original_service
                app_module.build_default_workflow = original_builder
                if original_token is None:
                    os.environ.pop("PADDLEOCR_TOKEN", None)
                else:
                    os.environ["PADDLEOCR_TOKEN"] = original_token
                if original_deepseek is None:
                    os.environ.pop("DEEPSEEK_API_KEY", None)
                else:
                    os.environ["DEEPSEEK_API_KEY"] = original_deepseek

    def test_acceptance_endpoint_returns_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed_sample_inputs(root)
            service = self.create_service(root)
            with self.running_server(service) as base_url:
                with request.urlopen(f"{base_url}/api/acceptance") as response:
                    self.assertEqual(200, response.status)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["sample_inputs_present"])
                self.assertEqual(3, len(payload["required_inputs"]))
                required = {entry["id"]: entry for entry in payload["required_inputs"]}
                self.assertTrue(any(entry["id"] == "real_template" for entry in payload["required_inputs"]))
                self.assertTrue(any(entry["id"] == "configured_mapping" for entry in payload["required_inputs"]))
                self.assertTrue(any(entry["id"] == "real_pdf" for entry in payload["required_inputs"]))
                self.assertEqual("EXCEL报价单格式不允许修改", payload["immutable_excel_policy"]["title"])
                self.assertTrue(any(entry["id"] == "place_real_template" for entry in payload["next_actions"]))
                self.assertTrue(any(entry["id"] == "add_real_pdf" for entry in payload["next_actions"]))
                self.assertFalse(required["real_pdf"]["present"])
                self.assertIn("checks", payload)
                self.assertTrue(any(entry["name"] == "fixed_template_export" for entry in payload["checks"]))
                self.assertTrue(any(entry["name"] == "real_template_still_required_for_final_acceptance" for entry in payload["checks"]))

    def test_acceptance_endpoint_auto_uses_configured_real_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed_sample_inputs(root)
            service = self.create_service(root)
            template = root / "templates" / "activation.xlsx"
            create_activation_template(template)
            imported = service.import_template(template.name, template.read_bytes())
            service.activate_template_mapping({
                "reviewer": "Template Reviewer",
                "confirm_format_immutable": True,
                "mapping": activation_mapping(imported["template_file"], imported["template_sha256"]),
            })
            with self.running_server(service) as base_url:
                with request.urlopen(f"{base_url}/api/acceptance") as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual("auto_real_template", payload["template_mode"])
                required = {entry["id"]: entry for entry in payload["required_inputs"]}
                self.assertTrue(required["real_template"]["present"])
                self.assertTrue(required["configured_mapping"]["present"])
                self.assertFalse(required["real_pdf"]["present"])
                self.assertTrue(any(entry["id"] == "add_real_pdf" for entry in payload["next_actions"]))
                final_check = next(entry for entry in payload["checks"] if entry["name"] == "real_template_still_required_for_final_acceptance")
                self.assertTrue(final_check["passed"])
                self.assertIn("正式模板映射", final_check["evidence"]["note"])

    def test_excel_download_endpoint_returns_attachment_with_original_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            template = root / "activation.xlsx"
            create_activation_template(template)
            imported = service.import_template(template.name, template.read_bytes())
            service.activate_template_mapping({
                "reviewer": "Template Reviewer",
                "confirm_format_immutable": True,
                "mapping": activation_mapping(imported["template_file"], imported["template_sha256"]),
            })
            source = PROJECT_ROOT / "samples" / "quote-normal.pdf"
            job = service.create_job(source.name, source.read_bytes())
            approved = service.review_job(job["id"], {"action": "approve", "reviewer": "Quote Reviewer"})

            with self.running_server(service) as base_url:
                with request.urlopen(f"{base_url}/api/jobs/{approved['id']}/excel") as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(
                        f'attachment; filename="quote-{approved["id"]}.xlsx"',
                        response.headers["Content-Disposition"],
                    )
                    self.assertEqual(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        response.headers.get_content_type(),
                    )
                    self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
                    self.assertGreater(len(response.read()), 0)

    def test_template_and_job_http_flow_hides_internal_paths_and_exports_excel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            template = root / "activation.xlsx"
            create_activation_template(template)
            pdf = PROJECT_ROOT / "samples" / "quote-normal.pdf"

            with self.running_server(service) as base_url:
                with request.urlopen(f"{base_url}/api/health") as response:
                    health = json.loads(response.read().decode("utf-8"))
                self.assertNotIn("mapping_path", json.dumps(health, ensure_ascii=False))

                status, imported = self.multipart_request(
                    f"{base_url}/api/template",
                    field_name="file",
                    filename=template.name,
                    content=template.read_bytes(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                self.assertEqual(201, status)
                self.assertEqual(template.name, imported["template_file"])
                self.assertNotIn("stored_path", imported)
                self.assertNotIn("report_path", imported)
                self.assertNotIn("draft_mapping_path", imported)

                with self.assertRaises(error.HTTPError) as activation_error:
                    self.json_request(
                        f"{base_url}/api/template/activate",
                        {
                            "reviewer": "Template Reviewer",
                            "confirm_format_immutable": False,
                            "mapping": activation_mapping(imported["template_file"], imported["template_sha256"]),
                        },
                    )
                self.assertEqual(409, activation_error.exception.code)

                status, activation = self.json_request(
                    f"{base_url}/api/template/activate",
                    {
                        "reviewer": "Template Reviewer",
                        "confirm_format_immutable": True,
                        "mapping": activation_mapping(imported["template_file"], imported["template_sha256"]),
                    },
                )
                self.assertEqual(200, status)
                self.assertTrue(activation["configured"])

                status, created_job = self.multipart_request(
                    f"{base_url}/api/jobs",
                    field_name="file",
                    filename=pdf.name,
                    content=pdf.read_bytes(),
                    content_type="application/pdf",
                )
                self.assertEqual(201, status)
                self.assertEqual("ready_for_review", created_job["status"])
                self.assertNotIn("source_path", created_job)
                self.assertNotIn("raw_pages", created_job["quote"])

                status, approved = self.json_request(
                    f"{base_url}/api/jobs/{created_job['id']}/review",
                    {"action": "approve", "reviewer": "HTTP Reviewer"},
                )
                self.assertEqual(200, status)
                self.assertEqual("approved", approved["status"])

                with request.urlopen(f"{base_url}/api/jobs/{created_job['id']}/excel") as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(
                        f'attachment; filename="quote-{created_job["id"]}.xlsx"',
                        response.headers["Content-Disposition"],
                    )
                    self.assertGreater(len(response.read()), 0)

    def test_http_review_rejects_stale_revision_with_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            pdf = PROJECT_ROOT / "samples" / "quote-normal.pdf"

            with self.running_server(service) as base_url:
                status, created_job = self.multipart_request(
                    f"{base_url}/api/jobs",
                    field_name="file",
                    filename=pdf.name,
                    content=pdf.read_bytes(),
                    content_type="application/pdf",
                )
                self.assertEqual(201, status)

                status, saved = self.json_request(
                    f"{base_url}/api/jobs/{created_job['id']}/review",
                    {
                        "action": "save",
                        "reviewer": "Reviewer A",
                        "note": "first-pass",
                        "expected_revision": created_job["revision"],
                    },
                )
                self.assertEqual(200, status)
                self.assertEqual(2, saved["revision"])

                with self.assertRaises(error.HTTPError) as stale_error:
                    self.json_request(
                        f"{base_url}/api/jobs/{created_job['id']}/review",
                        {
                            "action": "save",
                            "reviewer": "Reviewer B",
                            "note": "stale-pass",
                            "expected_revision": created_job["revision"],
                        },
                    )
                self.assertEqual(409, stale_error.exception.code)
                payload = json.loads(stale_error.exception.read().decode("utf-8"))
                self.assertIn("当前版本", payload["error"])

    def test_http_anomaly_job_blocks_approval_and_records_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.create_service(root)
            pdf = PROJECT_ROOT / "samples" / "quote-anomaly.pdf"

            with self.running_server(service) as base_url:
                status, created_job = self.multipart_request(
                    f"{base_url}/api/jobs",
                    field_name="file",
                    filename=pdf.name,
                    content=pdf.read_bytes(),
                    content_type="application/pdf",
                )
                self.assertEqual(201, status)
                self.assertEqual("needs_review", created_job["status"])
                self.assertEqual("quote_recognition_anomaly", created_job["alert"]["payload"]["event"])

                with self.assertRaises(error.HTTPError) as blocked_error:
                    self.json_request(
                        f"{base_url}/api/jobs/{created_job['id']}/review",
                        {"action": "approve", "reviewer": "HTTP Reviewer"},
                    )
                self.assertEqual(409, blocked_error.exception.code)
                payload = json.loads(blocked_error.exception.read().decode("utf-8"))
                self.assertIn("阻断", payload["error"])

                with request.urlopen(f"{base_url}/api/jobs/{created_job['id']}") as response:
                    latest = json.loads(response.read().decode("utf-8"))
                self.assertEqual("needs_review", latest["status"])
                self.assertEqual("quote_approval_blocked", latest["alert"]["payload"]["event"])
                self.assertEqual(2, len(latest["alerts"]))


if __name__ == "__main__":
    unittest.main()
