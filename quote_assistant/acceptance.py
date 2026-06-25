from __future__ import annotations

import json
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from .service import QuoteService


def create_acceptance_template(path: Path) -> None:
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


def acceptance_mapping(template_file: str, digest: str) -> dict[str, Any]:
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


def load_mapping(mapping_path: Path, template_file: str, digest: str) -> dict[str, Any]:
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["template_file"] = template_file
    mapping["template_sha256"] = digest
    return mapping


def discover_real_acceptance_inputs(project_root: Path) -> tuple[Path | None, Path | None]:
    config_path = project_root / "config.json"
    if not config_path.is_file():
        return None, None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    mapping_path = project_root / str(config.get("excel_template_mapping") or "")
    if not mapping_path.is_file():
        return None, None
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not mapping.get("configured"):
        return None, None
    template_file = str(mapping.get("template_file") or "")
    if not template_file:
        return None, None
    template_path = mapping_path.parent / template_file
    if not template_path.is_file():
        return None, None
    return template_path, mapping_path


def discover_real_pdf_inputs(project_root: Path) -> list[Path]:
    sample_hashes = set()
    for sample_name in ("quote-normal.pdf", "quote-anomaly.pdf"):
        sample_path = project_root / "samples" / sample_name
        if sample_path.is_file():
            sample_hashes.add(sha256(sample_path.read_bytes()).hexdigest())
    excluded = {
        (project_root / "samples" / "quote-normal.pdf").resolve(),
        (project_root / "samples" / "quote-anomaly.pdf").resolve(),
    }
    found: list[Path] = []
    for path in project_root.rglob("*.pdf"):
        resolved = path.resolve()
        if resolved in excluded:
            continue
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in sample_hashes:
            continue
        found.append(resolved)
    return sorted(found)


def build_next_actions(required_inputs: list[dict[str, Any]]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    missing = {entry["id"]: entry for entry in required_inputs if not entry.get("present")}
    if "real_template" in missing:
        actions.append({
            "id": "place_real_template",
            "title": "放入真实Excel模板",
            "detail": f"将正式报价模板放到 {missing['real_template']['expected_location']}。",
        })
    if "configured_mapping" in missing:
        actions.append({
            "id": "activate_mapping",
            "title": "启用正式模板映射",
            "detail": "在工作台点击“导入原Excel模板”，完成映射审核并启用正式映射。",
        })
    if "real_pdf" in missing:
        actions.append({
            "id": "add_real_pdf",
            "title": "补充真实PDF样本",
            "detail": f"将至少一份真实供应商PDF放到 {missing['real_pdf']['expected_location']}。",
        })
    if not actions:
        actions.append({
            "id": "run_final_acceptance",
            "title": "执行正式验收",
            "detail": "使用当前正式模板和真实PDF运行最终导出验收，确认Excel格式与审核告警流程都满足要求。",
        })
    return actions


def immutable_excel_policy() -> dict[str, str]:
    return {
        "title": "EXCEL报价单格式不允许修改",
        "detail": "系统只允许向正式模板中已登记且已存在的固定单元格写值，不允许新增、删除、重排、改样式、改公式、改合并区或改工作表结构。",
    }


def generate_acceptance_report(
    project_root: Path,
    *,
    template_path: Path | None = None,
    mapping_path: Path | None = None,
    normal_pdf: Path | None = None,
    anomaly_pdf: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    samples_dir = project_root / "samples"
    normal_pdf = normal_pdf.resolve() if normal_pdf else samples_dir / "quote-normal.pdf"
    anomaly_pdf = anomaly_pdf.resolve() if anomaly_pdf else samples_dir / "quote-anomaly.pdf"
    template_path = template_path.resolve() if template_path else None
    mapping_path = mapping_path.resolve() if mapping_path else None
    auto_template_path = None
    auto_mapping_path = None
    if template_path is None and mapping_path is None:
        auto_template_path, auto_mapping_path = discover_real_acceptance_inputs(project_root)
        template_path = auto_template_path
        mapping_path = auto_mapping_path
    real_templates = sorted((project_root / "templates").glob("*.xls*"))
    real_pdfs = discover_real_pdf_inputs(project_root)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "templates").mkdir(parents=True, exist_ok=True)
        config = json.loads((project_root / "config.json").read_text(encoding="utf-8"))
        (root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "templates" / "template_mapping.json").write_text(json.dumps({"configured": False}, ensure_ascii=False), encoding="utf-8")

        service = QuoteService(root)
        if template_path:
            imported = service.import_template(template_path.name, template_path.read_bytes())
            template_mode = "auto_real_template" if auto_template_path else "real_template"
        else:
            generated_template = root / "acceptance-template.xlsx"
            create_acceptance_template(generated_template)
            imported = service.import_template(generated_template.name, generated_template.read_bytes())
            template_mode = "sample_template"

        mapping = load_mapping(mapping_path, imported["template_file"], imported["template_sha256"]) if mapping_path else acceptance_mapping(imported["template_file"], imported["template_sha256"])

        activation_error = ""
        activated: dict[str, Any] | None = None
        try:
            activated = service.activate_template_mapping({
                "reviewer": "Acceptance Runner",
                "confirm_format_immutable": True,
                "mapping": mapping,
            })
        except ValueError as exc:
            activation_error = str(exc)

        approved: dict[str, Any] | None = None
        exported: dict[str, Any] | None = None
        output_path: Path | None = None
        export_error = ""
        if normal_pdf.is_file():
            normal_job = service.create_job(normal_pdf.name, normal_pdf.read_bytes())
            approved = service.review_job(normal_job["id"], {"action": "approve", "reviewer": "Acceptance Runner"})
            if activated:
                try:
                    output_path = service.export_job(approved["id"])
                    exported = service.store.get(approved["id"])["export"]
                except Exception as exc:
                    export_error = str(exc)

        anomaly_approval_error = ""
        anomaly_saved: dict[str, Any] | None = None
        if anomaly_pdf.is_file():
            anomaly_job = service.create_job(anomaly_pdf.name, anomaly_pdf.read_bytes())
            try:
                service.review_job(anomaly_job["id"], {"action": "approve", "reviewer": "Acceptance Runner"})
            except ValueError as exc:
                anomaly_approval_error = str(exc)
            anomaly_saved = service.store.get(anomaly_job["id"])

        required_inputs = [
            {
                "id": "real_template",
                "label": "真实Excel报价模板",
                "present": bool(real_templates),
                "expected_location": "quote_assistant/templates/<正式模板>.xlsx 或 .xlsm",
                "detail": real_templates[0].name if real_templates else "尚未发现正式模板文件。",
            },
            {
                "id": "configured_mapping",
                "label": "正式模板映射已启用",
                "present": bool(template_path and mapping_path),
                "expected_location": "quote_assistant/templates/template_mapping.json",
                "detail": str(mapping_path.relative_to(project_root)) if mapping_path else "尚未发现已启用的正式映射。",
            },
            {
                "id": "real_pdf",
                "label": "真实供应商PDF样本",
                "present": bool(real_pdfs),
                "expected_location": "quote_assistant/samples/ 或项目内其他业务目录中的真实PDF",
                "detail": str(real_pdfs[0].relative_to(project_root)) if real_pdfs else "当前只有系统样例PDF。",
            },
        ]

        return {
            "project_root": str(project_root),
            "generated_at": (approved or anomaly_saved or {"updated_at": ""})["updated_at"],
            "template_mode": template_mode,
            "immutable_excel_policy": immutable_excel_policy(),
            "requested_template": str(template_path) if template_path else "",
            "requested_mapping_json": str(mapping_path) if mapping_path else "",
            "real_template_detected": bool(real_templates),
            "real_template_files": [path.name for path in real_templates],
            "real_pdf_detected": bool(real_pdfs),
            "real_pdf_files": [str(path.relative_to(project_root)) for path in real_pdfs],
            "sample_inputs_present": normal_pdf.is_file() and anomaly_pdf.is_file(),
            "required_inputs": required_inputs,
            "next_actions": build_next_actions(required_inputs),
            "checks": [
                {
                    "name": "fixed_template_export",
                    "passed": bool(exported and exported["template_audit"]["structure_unchanged"]),
                    "evidence": {
                        "output_file": output_path.name if output_path else "",
                        "template_sha256": exported["template_audit"]["template_sha256"] if exported else "",
                        "mapped_cell_count": exported["template_audit"]["mapped_cell_count"] if exported else 0,
                        "written_cell_count": exported["template_audit"]["written_cell_count"] if exported else 0,
                        "export_error": export_error,
                    },
                },
                {
                    "name": "template_activation_requires_confirmation",
                    "passed": bool(activated and activated["configured"]),
                    "evidence": {
                        "sheet_name": activated["sheet_name"] if activated else "",
                        "template_file": imported["template_file"],
                        "structure_fingerprint": activated["structure_fingerprint"] if activated else "",
                        "activation_error": activation_error,
                    },
                },
                {
                    "name": "normal_pdf_can_be_reviewed_and_exported",
                    "passed": bool(approved and approved["status"] == "approved" and output_path and output_path.is_file()),
                    "evidence": {
                        "job_id": approved["id"] if approved else "",
                        "status": approved["status"] if approved else "missing_input",
                        "revision": approved["revision"] if approved else 0,
                        "input_file": normal_pdf.name if normal_pdf.is_file() else "",
                    },
                },
                {
                    "name": "anomaly_pdf_triggers_alert_and_blocks_approval",
                    "passed": bool(anomaly_saved and anomaly_saved["status"] == "needs_review" and len(anomaly_saved["alerts"]) >= 2),
                    "evidence": {
                        "job_id": anomaly_saved["id"] if anomaly_saved else "",
                        "status": anomaly_saved["status"] if anomaly_saved else "missing_input",
                        "latest_alert": anomaly_saved["alert"]["payload"]["event"] if anomaly_saved else "",
                        "alert_count": len(anomaly_saved["alerts"]) if anomaly_saved else 0,
                        "approval_error": anomaly_approval_error,
                        "input_file": anomaly_pdf.name if anomaly_pdf.is_file() else "",
                    },
                },
                {
                    "name": "real_template_still_required_for_final_acceptance",
                    "passed": bool(template_path and mapping_path and activated and exported),
                    "evidence": {
                        "detected_files": [path.name for path in real_templates],
                        "note": (
                            "已使用当前工作区启用的正式模板映射执行验收。"
                            if template_mode == "auto_real_template" and activated and exported
                            else "已提供真实模板和映射，可据此执行最终验收。"
                            if template_path and mapping_path
                            else "当前样例验收使用的是脚本生成的临时固定模板，不等同于用户正式报价模板。"
                        ),
                    },
                },
            ],
        }
