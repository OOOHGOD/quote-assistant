"""本地端到端工作流编排。

这个模块把 CLI 的 `run-local` 命令串成完整链路：
本地 PDF -> PaddleOCR -> DeepSeek -> quote JSON -> validation -> job.json -> 可选批准/导出。

它不依赖 Google Drive 或其他云端文件存储，所有中间产物都写在 `data/jobs/<job_id>/` 下。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .deepseek_agent import QuoteExtractionAgent
from .models import utc_now
from .PaddleOCR import OcrDocument, PaddleOcrClient, extract_markdown, parse_jsonl
from .service import QuoteService
from .template_export import TemplateExportError
from .validation import validate_quote


class OcrEngine(Protocol):
    """OCR 引擎协议，便于测试时替换成假实现。"""

    def parse_local_file(self, file_path: Path) -> OcrDocument:
        ...


class ExtractionAgent(Protocol):
    """结构化抽取 agent 协议，便于测试时绕开真实 DeepSeek 调用。"""

    def extract_quote(self, markdown_text: str, *, source_name: str, ocr_job_id: str = "") -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LocalWorkflowResult:
    """本地工作流返回值。

    CLI 会把这个对象压缩成摘要 JSON，方便命令行和 n8n 等外部流程读取。
    """

    job: dict[str, Any]
    output_path: Path | None
    ocr_artifact_dir: Path

    def to_summary(self) -> dict[str, Any]:
        """生成适合命令行输出的简要结果。"""
        return {
            "job_id": self.job["id"],
            "status": self.job["status"],
            "source_file": self.job["source_file"],
            "blocking_issue_count": self.job["validation"]["blocking_issue_count"],
            "warning_count": self.job["validation"]["warning_count"],
            "output_path": str(self.output_path) if self.output_path else "",
            "ocr_artifact_dir": str(self.ocr_artifact_dir),
        }


def build_default_workflow(project_root: Path) -> "LocalQuoteWorkflow":
    """使用环境变量和项目配置创建默认工作流。"""
    from .deepseek_agent import DeepSeekClient, DeepSeekSettings
    from .PaddleOCR import PaddleOcrSettings

    return LocalQuoteWorkflow(
        service=QuoteService(project_root),
        ocr_engine=PaddleOcrClient(PaddleOcrSettings.from_env()),
        extraction_agent=QuoteExtractionAgent(DeepSeekClient(DeepSeekSettings.from_env())),
    )


def build_agent_only_workflow(project_root: Path) -> "LocalQuoteWorkflow":
    """Create a workflow that can consume OCR artifacts without PaddleOCR credentials."""
    from .deepseek_agent import DeepSeekClient, DeepSeekSettings

    return LocalQuoteWorkflow(
        service=QuoteService(project_root),
        ocr_engine=_UnavailableOcrEngine(),
        extraction_agent=QuoteExtractionAgent(DeepSeekClient(DeepSeekSettings.from_env())),
    )


class _UnavailableOcrEngine:
    """Placeholder OCR engine for commands that already have OCR artifacts."""

    def parse_local_file(self, file_path: Path) -> OcrDocument:
        raise RuntimeError("PADDLEOCR_TOKEN is required unless OCR JSONL/Markdown artifacts are provided.")


class LocalQuoteWorkflow:
    """本地报价单处理主流程。"""

    def __init__(self, service: QuoteService, ocr_engine: OcrEngine, extraction_agent: ExtractionAgent):
        self.service = service
        self.ocr_engine = ocr_engine
        self.extraction_agent = extraction_agent

    def run(
        self,
        pdf_path: Path,
        *,
        reviewer: str = "Local Workflow",
        approve: bool = False,
        export: bool = False,
    ) -> LocalWorkflowResult:
        """执行一次 PDF 报价单处理。

        `approve/export` 是显式开关：默认只生成待审核任务，避免未经人工确认就写出正式 Excel。
        """
        content = _read_valid_pdf(pdf_path, int(self.service.config.get("max_pdf_bytes", 50 * 1024 * 1024)))
        ocr_document = self.ocr_engine.parse_local_file(pdf_path)
        return self._create_result_from_ocr_document(
            pdf_path=pdf_path,
            content=content,
            ocr_document=ocr_document,
            reviewer=reviewer,
            approve=approve,
            export=export,
        )

    def run_from_ocr_artifacts(
        self,
        pdf_path: Path,
        *,
        ocr_jsonl_path: Path | None = None,
        ocr_markdown_path: Path | None = None,
        ocr_job_id: str = "",
        ocr_result_url: str = "",
        ocr_model: str = "PaddleOCR-VL",
        reviewer: str = "Local Workflow",
        approve: bool = False,
        export: bool = False,
    ) -> LocalWorkflowResult:
        """Continue the workflow from OCR artifacts downloaded by n8n or another runner."""
        content = _read_valid_pdf(pdf_path, int(self.service.config.get("max_pdf_bytes", 50 * 1024 * 1024)))
        ocr_document = build_ocr_document_from_artifacts(
            ocr_jsonl_path=ocr_jsonl_path,
            ocr_markdown_path=ocr_markdown_path,
            ocr_job_id=ocr_job_id,
            ocr_result_url=ocr_result_url,
            ocr_model=ocr_model,
        )
        return self._create_result_from_ocr_document(
            pdf_path=pdf_path,
            content=content,
            ocr_document=ocr_document,
            reviewer=reviewer,
            approve=approve,
            export=export,
        )

    def _create_result_from_ocr_document(
        self,
        *,
        pdf_path: Path,
        content: bytes,
        ocr_document: OcrDocument,
        reviewer: str,
        approve: bool,
        export: bool,
    ) -> LocalWorkflowResult:
        """Create a job from a normalized OCR document and run review/export switches."""
        job_id = uuid.uuid4().hex[:12]
        job_dir = self.service.store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        source_path = job_dir / "source.pdf"
        source_path.write_bytes(content)

        ocr_artifact_dir = job_dir / "ocr"
        ocr_document.write_artifacts(ocr_artifact_dir)
        # DeepSeek 只接收 OCR markdown，不直接读 PDF；这样 OCR 与结构化抽取边界清晰。
        quote = self.extraction_agent.extract_quote(
            ocr_document.markdown_text,
            source_name=pdf_path.name,
            ocr_job_id=ocr_document.job_id,
        )

        job = self._create_job_record(
            job_id=job_id,
            filename=pdf_path.name,
            content=content,
            source_path=source_path,
            quote=quote,
            ocr_document=ocr_document,
        )
        if job["validation"]["blocking_issue_count"]:
            # 阻断级问题必须留下告警记录，后续人工复核可以看到触发原因。
            self.service._record_alert(job, "quote_recognition_anomaly")
        self.service.store.save(job)

        output_path: Path | None = None
        if approve:
            job = self.service.review_job(job_id, {"action": "approve", "reviewer": reviewer})
        if export:
            if not approve and job["status"] != "approved":
                raise ValueError("Export requires --approve or an already approved job.")
            output_path = self.service.export_job(job_id)
            job = self.service.store.get(job_id) or job

        return LocalWorkflowResult(job=job, output_path=output_path, ocr_artifact_dir=ocr_artifact_dir)

    def export_existing_job(self, job_id: str) -> Path:
        """导出一个已经存在且已批准的任务。"""
        return self.service.export_job(job_id)

    def _create_job_record(
        self,
        *,
        job_id: str,
        filename: str,
        content: bytes,
        source_path: Path,
        quote: dict[str, Any],
        ocr_document: OcrDocument,
    ) -> dict[str, Any]:
        """构造与 HTTP 上传流程兼容的 job.json 结构。"""
        validation = validate_quote(quote, self.service.config)
        now = utc_now()
        return {
            "id": job_id,
            "revision": 1,
            "source_file": Path(filename).name,
            "source_path": str(source_path),
            "source": {
                "filename": Path(filename).name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_type": "application/pdf",
            },
            "created_at": now,
            "updated_at": now,
            "status": validation["decision"],
            "quote": quote,
            "validation": validation,
            "review": None,
            "review_history": [],
            "alert": None,
            "alerts": [],
            "export": None,
            "ocr": {
                "provider": "paddleocr",
                "job_id": ocr_document.job_id,
                "model": ocr_document.model,
                "result_url": ocr_document.result_url,
                "artifact_dir": str(self.service.store.job_dir(job_id) / "ocr"),
            },
            "agent": {
                "provider": "deepseek",
                "name": "quote_extraction_agent",
            },
        }


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件的小工具，主要给脚本/测试复用。"""
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """写入格式化 JSON，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_ocr_document_from_artifacts(
    *,
    ocr_jsonl_path: Path | None = None,
    ocr_markdown_path: Path | None = None,
    ocr_job_id: str = "",
    ocr_result_url: str = "",
    ocr_model: str = "PaddleOCR-VL",
) -> OcrDocument:
    """Build the standard OCR document from local PaddleOCR JSONL and/or Markdown files."""
    if ocr_jsonl_path is None and ocr_markdown_path is None:
        raise ValueError("Either --ocr-jsonl or --ocr-md is required.")

    jsonl_text = ""
    raw_lines: list[dict[str, Any]] = []
    markdown_text = ""

    if ocr_jsonl_path is not None:
        if not ocr_jsonl_path.is_file():
            raise ValueError(f"OCR JSONL does not exist: {ocr_jsonl_path}")
        jsonl_text = ocr_jsonl_path.read_text(encoding="utf-8")
        raw_lines = parse_jsonl(jsonl_text)
        markdown_text = extract_markdown(raw_lines)

    if ocr_markdown_path is not None:
        if not ocr_markdown_path.is_file():
            raise ValueError(f"OCR Markdown does not exist: {ocr_markdown_path}")
        markdown_text = ocr_markdown_path.read_text(encoding="utf-8")

    if not markdown_text.strip():
        raise ValueError("OCR artifacts produced empty markdown text.")

    return OcrDocument(
        job_id=ocr_job_id.strip() or "external-ocr-artifact",
        model=ocr_model.strip() or "PaddleOCR-VL",
        markdown_text=markdown_text,
        jsonl_text=jsonl_text,
        raw_lines=raw_lines,
        result_url=ocr_result_url.strip(),
        metadata={"source": "external_ocr_artifact"},
    )


def _read_valid_pdf(pdf_path: Path, max_bytes: int) -> bytes:
    """Read a local PDF after the shared workflow validation checks."""
    content = pdf_path.read_bytes()
    _validate_local_pdf(pdf_path, content, max_bytes)
    return content


def _validate_local_pdf(pdf_path: Path, content: bytes, max_bytes: int) -> None:
    """在调用 OCR 前做本地 PDF 基础校验。"""
    if not pdf_path.is_file():
        raise ValueError(f"PDF does not exist: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input document must be a local PDF file.")
    if not content.startswith(b"%PDF-"):
        raise ValueError("Input file is not a valid PDF.")
    if len(content) > max_bytes:
        raise ValueError(f"PDF exceeds size limit: {max_bytes} bytes.")


def format_workflow_error(exc: Exception) -> str:
    """把底层异常转换成 CLI 友好的错误信息。"""
    if isinstance(exc, TemplateExportError):
        return f"Template export blocked: {exc}"
    return str(exc)
