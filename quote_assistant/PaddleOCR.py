from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


DEFAULT_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL"
PENDING_STATES = {"pending", "running"}


class PaddleOcrError(RuntimeError):
    """Raised when PaddleOCR cannot produce a usable document result."""


@dataclass(frozen=True)
class PaddleOcrSettings:
    token: str
    job_url: str = DEFAULT_JOB_URL
    model: str = DEFAULT_MODEL
    poll_interval_seconds: float = 5.0
    timeout_seconds: float = 600.0
    request_timeout_seconds: float = 60.0
    optional_payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "PaddleOcrSettings":
        token = os.environ.get("PADDLEOCR_TOKEN", "").strip()
        if not token:
            raise PaddleOcrError("PADDLEOCR_TOKEN is required for PaddleOCR extraction.")
        optional_payload = {
            "useDocOrientationClassify": _env_bool("PADDLEOCR_USE_DOC_ORIENTATION", False),
            "useDocUnwarping": _env_bool("PADDLEOCR_USE_DOC_UNWARPING", False),
            "useChartRecognition": _env_bool("PADDLEOCR_USE_CHART_RECOGNITION", False),
        }
        return cls(
            token=token,
            job_url=os.environ.get("PADDLEOCR_JOB_URL", DEFAULT_JOB_URL).strip() or DEFAULT_JOB_URL,
            model=os.environ.get("PADDLEOCR_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            poll_interval_seconds=float(os.environ.get("PADDLEOCR_POLL_INTERVAL_SECONDS", "5")),
            timeout_seconds=float(os.environ.get("PADDLEOCR_TIMEOUT_SECONDS", "600")),
            request_timeout_seconds=float(os.environ.get("PADDLEOCR_REQUEST_TIMEOUT_SECONDS", "60")),
            optional_payload=optional_payload,
        )


@dataclass(frozen=True)
class OcrDocument:
    job_id: str
    model: str
    markdown_text: str
    jsonl_text: str
    raw_lines: list[dict[str, Any]]
    result_url: str
    metadata: dict[str, Any]

    def write_artifacts(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "ocr.md").write_text(self.markdown_text, encoding="utf-8")
        (output_dir / "ocr.jsonl").write_text(self.jsonl_text, encoding="utf-8")
        (output_dir / "ocr_meta.json").write_text(
            json.dumps(
                {
                    "job_id": self.job_id,
                    "model": self.model,
                    "result_url": self.result_url,
                    "metadata": self.metadata,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class PaddleOcrClient:
    def __init__(self, settings: PaddleOcrSettings):
        self.settings = settings

    def parse_local_file(self, file_path: Path) -> OcrDocument:
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise PaddleOcrError(f"Local document does not exist: {resolved}")
        job_id = self._submit_file(resolved)
        result = self._wait_for_result(job_id)
        json_url = str((result.get("resultUrl") or {}).get("jsonUrl") or "")
        if not json_url:
            raise PaddleOcrError(f"PaddleOCR job {job_id} finished without resultUrl.jsonUrl.")
        jsonl_text = self._download_jsonl(json_url)
        raw_lines = parse_jsonl(jsonl_text)
        markdown_text = extract_markdown(raw_lines)
        if not markdown_text.strip():
            raise PaddleOcrError(f"PaddleOCR job {job_id} produced empty markdown text.")
        return OcrDocument(
            job_id=job_id,
            model=self.settings.model,
            markdown_text=markdown_text,
            jsonl_text=jsonl_text,
            raw_lines=raw_lines,
            result_url=json_url,
            metadata={"extract_progress": result.get("extractProgress") or {}},
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token}"}

    def _submit_file(self, file_path: Path) -> str:
        data = {
            "model": self.settings.model,
            "optionalPayload": json.dumps(self.settings.optional_payload, ensure_ascii=False),
        }
        with file_path.open("rb") as document:
            response = requests.post(
                self.settings.job_url,
                headers=self._headers(),
                data=data,
                files={"file": document},
                timeout=self.settings.request_timeout_seconds,
            )
        payload = _json_response(response)
        job_id = str((payload.get("data") or {}).get("jobId") or "")
        if not job_id:
            raise PaddleOcrError(f"PaddleOCR submit response did not include data.jobId: {payload}")
        return job_id

    def _wait_for_result(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.timeout_seconds
        last_payload: dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            response = requests.get(
                f"{self.settings.job_url.rstrip('/')}/{job_id}",
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=self.settings.request_timeout_seconds,
            )
            payload = _json_response(response)
            last_payload = payload
            data = payload.get("data") or {}
            state = str(data.get("state") or "").lower()
            if state == "done":
                return data
            if state == "failed":
                raise PaddleOcrError(str(data.get("errorMsg") or f"PaddleOCR job {job_id} failed."))
            if state not in PENDING_STATES:
                raise PaddleOcrError(f"Unexpected PaddleOCR job state for {job_id}: {state!r}")
            time.sleep(self.settings.poll_interval_seconds)
        raise PaddleOcrError(f"PaddleOCR job {job_id} timed out. Last response: {last_payload}")

    def _download_jsonl(self, json_url: str) -> str:
        response = requests.get(json_url, timeout=self.settings.request_timeout_seconds)
        if response.status_code >= 400:
            raise PaddleOcrError(f"Could not download PaddleOCR JSONL result: HTTP {response.status_code}")
        return response.text


def parse_jsonl(jsonl_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(jsonl_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise PaddleOcrError(f"Invalid JSONL at line {line_number}: {exc}") from exc
    return rows


def extract_markdown(rows: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for row in rows:
        result = row.get("result") if isinstance(row, dict) else None
        if not isinstance(result, dict):
            continue
        for entry in result.get("layoutParsingResults") or []:
            markdown = entry.get("markdown") or {}
            text = markdown.get("text")
            if text:
                chunks.append(str(text))
        for entry in result.get("ocrResults") or []:
            text = entry.get("text") or entry.get("recText")
            if text:
                chunks.append(str(text))
    return "\n\n".join(chunks)


def _json_response(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaddleOcrError(f"PaddleOCR returned non-JSON response: HTTP {response.status_code}") from exc
    if response.status_code >= 400 or int(payload.get("code") or 0) != 0:
        raise PaddleOcrError(f"PaddleOCR request failed: HTTP {response.status_code}, payload={payload}")
    return payload


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
