"""报价单任务服务层。

`QuoteService` 是后端业务入口，负责把解析、校验、审核、模板导入、模板启用、Excel 导出和告警串起来。
HTTP 接口和 CLI 都应该通过这里操作任务，避免多个入口各自改写 job.json。
"""

from __future__ import annotations

import json
import hashlib
import uuid
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from .alerts import alert_delivery_due, emit_alert, persist_alert, retry_alert_delivery
from .models import utc_now
from .parser import parse_pdf
from .storage import JobStore
from .template_export import TemplateExportError, export_from_immutable_template, inspect_template_configuration
from .template_inspect import inspect_xlsx_template, mapping_draft_from_report
from .validation import apply_corrections, apply_item_rows, resolve_extraction_issues, validate_quote


class QuoteService:
    """报价单任务的应用服务。

    所有会修改任务状态的方法都通过 `_mutation_lock` 串行化，避免前端重复点击或后台重试导致并发写入。
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.config = json.loads((project_root / "config.json").read_text(encoding="utf-8"))
        self.store = JobStore(project_root / "data" / "jobs")
        self._mutation_lock = RLock()

    def create_job(self, filename: str, content: bytes) -> dict[str, Any]:
        """从上传的 PDF 创建任务。

        这是文本型 PDF 的快速路径；扫描件或更复杂的版面会在本地工作流中走 PaddleOCR + DeepSeek。
        """
        if not filename.lower().endswith(".pdf"):
            raise ValueError("当前版本只接受PDF报价单。")
        if not content.startswith(b"%PDF-"):
            raise ValueError("上传文件不是有效的PDF。")
        max_bytes = int(self.config.get("max_pdf_bytes", 50 * 1024 * 1024))
        if len(content) > max_bytes:
            raise ValueError(f"PDF文件超过大小限制：{max_bytes // (1024 * 1024)} MB。")
        job_id = uuid.uuid4().hex[:12]
        directory = self.store.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "source.pdf"
        source_path.write_bytes(content)
        source_sha256 = hashlib.sha256(content).hexdigest()
        quote = parse_pdf(source_path)
        validation = validate_quote(quote, self.config)
        job = {
            "id": job_id,
            "revision": 1,
            "source_file": Path(filename).name,
            "source_path": str(source_path),
            "source": {
                "filename": Path(filename).name,
                "size_bytes": len(content),
                "sha256": source_sha256,
                "content_type": "application/pdf",
            },
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": validation["decision"],
            "quote": quote,
            "validation": validation,
            "review": None,
            "review_history": [],
            "alert": None,
            "alerts": [],
            "export": None,
        }
        if validation["blocking_issue_count"]:
            self._record_alert(job, "quote_recognition_anomaly")
        self.store.save(job)
        return job

    def import_template(self, filename: str, content: bytes) -> dict[str, Any]:
        """导入原始 Excel 模板并生成体检报告/映射草稿。

        这里不会启用模板，必须由人工确认映射后再调用 `activate_template_mapping`。
        """
        safe_name = Path(filename).name
        extension = Path(safe_name).suffix.lower()
        if extension not in {".xlsx", ".xlsm"}:
            raise ValueError("原始Excel模板必须是.xlsx或.xlsm文件。")
        if not content:
            raise ValueError("Excel模板文件为空。")

        templates_dir = self.project_root / "templates"
        templates_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        target = templates_dir / safe_name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            target = templates_dir / f"{target.stem}-{digest[:8]}{extension}"
        target.write_bytes(content)

        report = inspect_xlsx_template(target)
        draft = mapping_draft_from_report(report)
        report_path = templates_dir / "template_report.json"
        draft_path = templates_dir / "template_mapping.draft.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "stored_path": str(target),
            "template_file": target.name,
            "template_sha256": digest,
            "sheet_names": [sheet["name"] for sheet in report["sheets"]],
            "report_path": str(report_path),
            "draft_mapping_path": str(draft_path),
            "configured": False,
            "review_required": True,
            "message": "原模板已原样保存；体检报告与映射草稿已生成，人工确认前正式导出仍保持锁定。",
        }

    def review_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """保存、批准或驳回人工审核结果。"""
        with self._mutation_lock:
            return self._review_job_locked(job_id, payload)

    def _review_job_locked(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带锁执行审核变更，包含版本校验、字段修正、重新校验和告警记录。"""
        job = self._required_job(job_id)
        self._verify_source_integrity(job)
        current_revision = int(job.get("revision") or 0)
        expected_revision = payload.get("expected_revision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ValueError("任务版本号无效。") from exc
            if expected_revision != current_revision:
                raise ValueError(f"任务已被其他审核员更新（当前版本 {current_revision}），请刷新后重新提交。")
        action = payload.get("action", "save")
        if action not in {"save", "approve", "reject"}:
            raise ValueError("无效的审核操作。")
        reviewer = str(payload.get("reviewer") or "人工审核员").strip()
        note = str(payload.get("note") or "").strip()
        reviewed_at = utc_now()
        was_approved = job.get("status") == "approved"
        before_quote = deepcopy(job["quote"])
        corrections = payload.get("corrections", {})
        if corrections:
            job["quote"] = apply_corrections(job["quote"], corrections)
        if "item_rows" in payload:
            item_rows = payload.get("item_rows")
            if not isinstance(item_rows, list):
                raise ValueError("报价明细格式无效。")
            job["quote"] = apply_item_rows(job["quote"], item_rows, int(self.config.get("max_manual_items", 500)))
        if payload.get("human_verified_source"):
            job["quote"] = resolve_extraction_issues(
                job["quote"], reviewer, reviewed_at, str(job.get("source", {}).get("sha256") or "")
            )
        job["validation"] = validate_quote(job["quote"], self.config)
        changed_paths = self._changed_paths(before_quote, job["quote"])
        if changed_paths or (was_approved and action in {"save", "reject"}):
            self._invalidate_export(job)

        if action == "approve":
            if job["validation"]["blocking_issue_count"]:
                job["status"] = "needs_review"
                self._record_alert(job, "quote_approval_blocked", details={"reviewer": reviewer, "note": note})
                self._record_review(job, action, "blocked", reviewer, note, reviewed_at, changed_paths, bool(payload.get("human_verified_source")))
                job["updated_at"] = utc_now()
                job["revision"] = current_revision + 1
                self.store.save(job)
                raise ValueError("仍有阻断异常，请修正后再批准。")
            job["status"] = "approved"
            job["review"] = {"decision": "approved", "reviewer": reviewer, "note": note, "reviewed_at": reviewed_at}
            outcome = "approved"
        elif action == "reject":
            job["status"] = "rejected"
            job["review"] = {"decision": "rejected", "reviewer": reviewer, "note": note, "reviewed_at": reviewed_at}
            outcome = "rejected"
        else:
            job["status"] = job["validation"]["decision"]
            job["review"] = {"decision": "draft", "reviewer": reviewer, "note": note, "reviewed_at": reviewed_at}
            outcome = "saved"
            if job["validation"]["blocking_issue_count"]:
                self._record_alert(job, "quote_review_still_blocked", details={"reviewer": reviewer, "note": note})

        self._record_review(job, action, outcome, reviewer, note, reviewed_at, changed_paths, bool(payload.get("human_verified_source")))
        job["updated_at"] = utc_now()
        job["revision"] = current_revision + 1
        self.store.save(job)
        return job

    def export_job(self, job_id: str) -> Path:
        """导出已批准任务到当前启用的 Excel 模板。"""
        with self._mutation_lock:
            return self._export_job_locked(job_id)

    def _export_job_locked(self, job_id: str) -> Path:
        """带锁执行 Excel 导出，并在失败时记录阻断告警。"""
        job = self._required_job(job_id)
        self._verify_source_integrity(job)
        if job["status"] != "approved":
            raise ValueError("报价单必须先通过人工审核才能导出Excel。")
        mapping_path = self.project_root / self.config["excel_template_mapping"]
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            extension = Path(str(mapping.get("template_file") or "")).suffix.lower() or ".xlsx"
            output_path = self.project_root / "output" / f"quote-{job_id}{extension}"
            template_audit = export_from_immutable_template(job, self.project_root, mapping_path, output_path)
        except (OSError, json.JSONDecodeError, TemplateExportError) as exc:
            self._record_alert(job, "quote_export_blocked", issues=[{
                "code": "TEMPLATE_EXPORT_BLOCKED",
                "severity": "critical",
                "message": str(exc),
                "path": "excel_template",
                "actual": None,
            }])
            job["updated_at"] = utc_now()
            job["revision"] = int(job.get("revision") or 0) + 1
            self.store.save(job)
            raise TemplateExportError(str(exc)) from exc
        job["export"] = {"path": str(output_path), "generated_at": utc_now(), "template_audit": template_audit}
        job["updated_at"] = utc_now()
        job["revision"] = int(job.get("revision") or 0) + 1
        self.store.save(job)
        return output_path

    def source_document(self, job_id: str) -> Path:
        """返回任务源 PDF，返回前会先校验源文件哈希。"""
        job = self._required_job(job_id)
        return self._verify_source_integrity(job)

    def retry_alerts(self, job_id: str, *, force: bool = False) -> dict[str, Any]:
        """重试某个任务的告警投递。"""
        with self._mutation_lock:
            job = self._required_job(job_id)
            changed = False
            job_dir = self.store.job_dir(job_id)
            for index, alert in enumerate(job.get("alerts") or []):
                alert.setdefault("sequence", index + 1)
                updated, attempted = retry_alert_delivery(alert, job_dir, force=force)
                if attempted:
                    job["alerts"][index] = updated
                    persist_alert(updated, job_dir, latest=False)
                    changed = True
            if changed:
                job["alert"] = job["alerts"][-1]
                persist_alert(job["alert"], job_dir)
                job["updated_at"] = utc_now()
                self.store.save(job)
            return job

    def retry_due_alerts(self) -> int:
        """扫描全部任务，重试已经到期的 webhook 告警。"""
        retried = 0
        for stored_job in self.store.list():
            if any(alert_delivery_due(alert) for alert in stored_job.get("alerts") or []):
                attempts_before = sum(int((alert.get("delivery") or {}).get("attempts") or 0) for alert in stored_job.get("alerts") or [])
                job = self.retry_alerts(stored_job["id"])
                attempts_after = sum(int((alert.get("delivery") or {}).get("attempts") or 0) for alert in job.get("alerts") or [])
                retried += max(0, attempts_after - attempts_before)
        return retried

    def template_status(self) -> dict[str, Any]:
        """返回当前正式模板映射是否可用于导出。"""
        mapping_path = self.project_root / self.config["excel_template_mapping"]
        try:
            return {**inspect_template_configuration(mapping_path), "reason": ""}
        except TemplateExportError as exc:
            return {"configured": False, "reason": str(exc), "mapping_path": str(mapping_path)}

    def template_setup(self) -> dict[str, Any]:
        """返回模板导入后等待人工确认的映射草稿信息。"""
        templates_dir = self.project_root / "templates"
        draft_path = templates_dir / "template_mapping.draft.json"
        report_path = templates_dir / "template_report.json"
        if not draft_path.exists() or not report_path.exists():
            return {"available": False, "reason": "尚未导入原始Excel模板。"}
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"available": False, "reason": f"模板体检资料无法读取：{exc}"}
        sheets = [{
            "name": sheet.get("name"),
            "dimension": sheet.get("dimension"),
            "formula_count": sheet.get("formula_count", 0),
            "merged_range_count": len(sheet.get("merged_ranges", [])),
            "field_candidates": sheet.get("field_candidates", {}),
        } for sheet in report.get("sheets", [])]
        return {
            "available": True,
            "draft": draft,
            "template_sha256": report.get("template_sha256"),
            "template_file": report.get("template_file"),
            "sheets": sheets,
        }

    def activate_template_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启用经过人工确认的模板映射。

        启用前会重新校验模板哈希和映射安全性；只有通过检查的映射才会替换正式 `template_mapping.json`。
        """
        reviewer = str(payload.get("reviewer") or "").strip()
        mapping = payload.get("mapping")
        if not reviewer:
            raise ValueError("必须填写模板映射审核人。")
        if payload.get("confirm_format_immutable") is not True:
            raise ValueError("必须确认仅向固定单元格写值且不修改Excel格式。")
        if not isinstance(mapping, dict):
            raise ValueError("模板映射格式无效。")

        setup = self.template_setup()
        if not setup.get("available"):
            raise ValueError(setup.get("reason") or "模板草稿不可用。")
        template_file = str(mapping.get("template_file") or "")
        if not template_file or Path(template_file).name != template_file:
            raise ValueError("模板文件名无效。")
        template_path = self.project_root / "templates" / template_file
        if not template_path.is_file():
            raise ValueError("模板文件不存在。")
        actual_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
        expected_hash = str(mapping.get("template_sha256") or payload.get("template_sha256") or "")
        if not expected_hash or actual_hash != expected_hash or actual_hash != setup.get("template_sha256"):
            raise ValueError("模板文件哈希与体检草稿不一致，必须重新导入并审核。")

        candidate = deepcopy(mapping)
        candidate["configured"] = True
        candidate["review_required"] = False
        candidate["template_sha256"] = actual_hash
        candidate.pop("header_row_suggestion", None)
        items = candidate.get("items")
        if isinstance(items, dict):
            items.pop("header_row_suggestion", None)

        templates_dir = self.project_root / "templates"
        candidate_path = templates_dir / ".template_mapping.candidate.json"
        mapping_path = self.project_root / self.config["excel_template_mapping"]
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            inspection = inspect_template_configuration(candidate_path)
        except TemplateExportError:
            candidate_path.unlink(missing_ok=True)
            raise

        candidate_path.replace(mapping_path)
        audit_path = templates_dir / "template_activation_history.json"
        try:
            history = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else []
        except (OSError, json.JSONDecodeError):
            history = []
        history.append({
            "activated_at": utc_now(),
            "reviewer": reviewer,
            "template_file": template_file,
            "template_sha256": actual_hash,
            "sheet_name": inspection["sheet_name"],
            "mapped_cell_count": inspection["mapped_cell_count"],
            "structure_fingerprint": inspection["structure_fingerprint"],
        })
        audit_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            **inspection,
            "activated_at": history[-1]["activated_at"],
            "reviewer": reviewer,
            "message": "模板映射已通过完整校验并启用。",
        }

    def _record_alert(
        self,
        job: dict[str, Any],
        event: str,
        *,
        issues: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录任务告警，并立即尝试本地/远端投递。"""
        history = job.setdefault("alerts", [])
        alert = emit_alert(
            job,
            self.store.job_dir(job["id"]),
            event=event,
            issues=issues,
            details=details,
            sequence=len(history) + 1,
        )
        history.append(alert)
        job["alert"] = alert
        return alert

    def _record_review(
        self,
        job: dict[str, Any],
        action: str,
        outcome: str,
        reviewer: str,
        note: str,
        reviewed_at: str,
        changed_paths: list[str],
        human_verified_source: bool,
    ) -> None:
        """把每次审核动作追加到审计历史中。"""
        job.setdefault("review_history", []).append({
            "action": action,
            "outcome": outcome,
            "reviewer": reviewer,
            "note": note,
            "reviewed_at": reviewed_at,
            "changed_paths": changed_paths,
            "human_verified_source": human_verified_source,
            "source_sha256": job.get("source", {}).get("sha256"),
            "blocking_issue_count": job["validation"]["blocking_issue_count"],
            "resulting_revision": int(job.get("revision") or 0) + 1,
        })

    def _changed_paths(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        """比较审核前后的报价数据，返回发生变化的字段路径。"""
        def flatten(payload: Any, prefix: str = "") -> dict[str, Any]:
            """把嵌套 quote 数据压平成 `{路径: 值}`，便于审计差异。"""
            result: dict[str, Any] = {}
            if isinstance(payload, dict):
                if "value" in payload and "confidence" in payload:
                    result[prefix] = payload.get("value")
                else:
                    for key, value in payload.items():
                        if key in {"raw_pages", "evidence"}:
                            continue
                        child = f"{prefix}.{key}" if prefix else key
                        result.update(flatten(value, child))
            elif isinstance(payload, list):
                for index, value in enumerate(payload):
                    child = f"{prefix}.{index}" if prefix else str(index)
                    result.update(flatten(value, child))
            elif prefix.endswith("line_no") or prefix.endswith("verified"):
                result[prefix] = payload
            return result

        before_values = flatten(before)
        after_values = flatten(after)
        return sorted(path for path in set(before_values) | set(after_values) if before_values.get(path) != after_values.get(path))

    def _invalidate_export(self, job: dict[str, Any]) -> None:
        """当审核数据发生变化时删除旧导出文件，防止过期 Excel 被继续使用。"""
        export = job.get("export")
        if not export:
            return
        path = Path(str(export.get("path") or ""))
        output_root = (self.project_root / "output").resolve()
        try:
            resolved = path.resolve()
            if output_root in resolved.parents:
                resolved.unlink(missing_ok=True)
        except OSError:
            pass
        job["export"] = None

    def _verify_source_integrity(self, job: dict[str, Any]) -> Path:
        """校验源 PDF 是否仍与创建任务时的 SHA-256 一致。"""
        source_path = self.store.job_dir(job["id"]) / "source.pdf"
        expected_hash = str(job.get("source", {}).get("sha256") or "")
        if not source_path.is_file():
            message = "原PDF文件不存在，审核与导出已阻断。"
            self._record_source_integrity_failure(job, message, None)
            raise ValueError(message)
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if not expected_hash:
            job.setdefault("source", {})["sha256"] = actual_hash
            job["source"]["size_bytes"] = source_path.stat().st_size
            job["source"]["filename"] = job.get("source_file", "source.pdf")
            job["source"]["content_type"] = "application/pdf"
            self.store.save(job)
            return source_path
        if actual_hash != expected_hash:
            message = "原PDF文件哈希不一致，可能已被替换或损坏；审核与导出已阻断。"
            self._record_source_integrity_failure(job, message, actual_hash)
            raise ValueError(message)
        return source_path

    def _record_source_integrity_failure(self, job: dict[str, Any], message: str, actual_hash: str | None) -> None:
        """源文件丢失或被替换时，记录 critical 告警并阻断审核/导出。"""
        existing = (job.get("alert") or {}).get("payload", {})
        if existing.get("event") != "quote_source_integrity_failure":
            self._record_alert(job, "quote_source_integrity_failure", issues=[{
                "code": "SOURCE_INTEGRITY_FAILURE",
                "severity": "critical",
                "message": message,
                "path": "source.pdf",
                "actual": actual_hash,
            }], details={"expected_sha256": job.get("source", {}).get("sha256"), "actual_sha256": actual_hash})
        job["status"] = "needs_review"
        job["quote"].pop("human_source_verification", None)
        for entry in job["quote"].get("extraction_issues", []):
            entry.pop("resolved_by_human", None)
            entry.pop("resolution", None)
        self._invalidate_export(job)
        job["updated_at"] = utc_now()
        self.store.save(job)

    def _required_job(self, job_id: str) -> dict[str, Any]:
        """读取任务；不存在则抛出统一的业务错误。"""
        job = self.store.get(job_id)
        if not job:
            raise KeyError("任务不存在。")
        return job
