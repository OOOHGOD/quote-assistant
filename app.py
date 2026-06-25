"""本地 HTTP 工作台入口。

这个文件只负责把浏览器请求翻译成 `QuoteService` 的方法调用：
- `/api/jobs` 上传 PDF 并创建审核任务；
- `/api/jobs/<id>/review` 保存、批准或驳回人工审核；
- `/api/jobs/<id>/excel` 从已批准任务导出 Excel；
- `/api/template/*` 导入、体检并启用 Excel 模板映射。

注意：这里返回给前端的数据会主动隐藏本机路径和原始 OCR 页文本，避免把本地目录结构暴露给浏览器层。
"""

from __future__ import annotations

import argparse
import cgi
from copy import deepcopy
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from quote_assistant.acceptance import generate_acceptance_report
from quote_assistant.service import QuoteService


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
SERVICE = QuoteService(ROOT)


class Handler(BaseHTTPRequestHandler):
    """轻量级请求处理器。

    项目没有引入 Flask/FastAPI，是为了让 MVP 在标准库环境里也能运行。
    业务状态全部交给全局 `SERVICE`，本类只做协议解析、参数读取和响应格式化。
    """

    server_version = "QuoteAssistant/0.1"

    def do_GET(self) -> None:
        """处理只读接口和静态文件。"""
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.json_response({"status": "ok", "excel_template": self.public_template_status(SERVICE.template_status())})
        if path == "/api/acceptance":
            return self.json_response(generate_acceptance_report(SERVICE.project_root))
        if path == "/api/template/setup":
            return self.json_response(SERVICE.template_setup())
        if path == "/api/jobs":
            jobs = SERVICE.store.list()
            summary = [{key: job.get(key) for key in ("id", "source_file", "created_at", "updated_at", "status", "validation")} for job in jobs]
            return self.json_response(summary)
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                job = SERVICE.store.get(parts[2])
                return self.json_response(self.public_job(job) if job else None, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            if len(parts) == 4 and parts[3] == "excel":
                try:
                    output = SERVICE.export_job(parts[2])
                    return self.file_response(output, download_name=output.name)
                except (KeyError, ValueError, RuntimeError) as exc:
                    return self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
            if len(parts) == 4 and parts[3] == "source":
                try:
                    source = SERVICE.source_document(parts[2])
                    return self.file_response(source, inline_name="source.pdf", cache_control="no-store")
                except KeyError as exc:
                    return self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                except ValueError as exc:
                    return self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
        return self.serve_static(path)

    def do_POST(self) -> None:
        """处理会改变状态的接口。"""
        path = urlparse(self.path).path
        if path == "/api/jobs":
            return self.handle_upload()
        if path == "/api/template":
            return self.handle_template_upload()
        if path == "/api/template/activate":
            try:
                return self.json_response(SERVICE.activate_template_mapping(self.read_json()))
            except (ValueError, RuntimeError) as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
        if path.startswith("/api/jobs/") and path.endswith("/review"):
            job_id = path.strip("/").split("/")[2]
            payload = self.read_json()
            try:
                return self.json_response(self.public_job(SERVICE.review_job(job_id, payload)))
            except KeyError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
        if path.startswith("/api/jobs/") and path.endswith("/alerts/retry"):
            job_id = path.strip("/").split("/")[2]
            try:
                return self.json_response(self.public_job(SERVICE.retry_alerts(job_id, force=True)))
            except KeyError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        return self.json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def handle_upload(self) -> None:
        """接收 PDF 表单上传，并创建一个本地审核任务。"""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.json_response({"error": "请使用multipart/form-data上传PDF。"}, HTTPStatus.BAD_REQUEST)
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
        upload = form["file"] if "file" in form else None
        if upload is None or not getattr(upload, "filename", None):
            return self.json_response({"error": "未收到PDF文件。"}, HTTPStatus.BAD_REQUEST)
        try:
            job = SERVICE.create_job(upload.filename, upload.file.read())
            return self.json_response(self.public_job(job), HTTPStatus.CREATED)
        except Exception as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_template_upload(self) -> None:
        """接收 Excel 模板上传，只生成体检报告和映射草稿，不会立即启用。"""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.json_response({"error": "请使用multipart/form-data上传Excel模板。"}, HTTPStatus.BAD_REQUEST)
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
        upload = form["file"] if "file" in form else None
        if upload is None or not getattr(upload, "filename", None):
            return self.json_response({"error": "未收到Excel模板。"}, HTTPStatus.BAD_REQUEST)
        try:
            result = SERVICE.import_template(upload.filename, upload.file.read())
            return self.json_response(self.public_template_import_result(result), HTTPStatus.CREATED)
        except Exception as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def read_json(self) -> dict:
        """读取请求体中的 JSON；空请求体按空字典处理。"""
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def public_job(self, job: dict) -> dict:
        """生成面向前端的任务对象，并隐藏本机路径、告警路径和原始页文本。"""
        public = deepcopy(job)
        public.pop("source_path", None)
        for alert in public.get("alerts") or []:
            delivery = alert.get("delivery") or {}
            delivery.pop("local_path", None)
            delivery.pop("path", None)
        latest_alert = public.get("alert") or {}
        latest_delivery = latest_alert.get("delivery") or {}
        latest_delivery.pop("local_path", None)
        latest_delivery.pop("path", None)
        quote = public.get("quote")
        if isinstance(quote, dict):
            quote.pop("raw_pages", None)
        return public

    def public_template_status(self, status: dict) -> dict:
        """隐藏模板和映射文件的本机绝对路径。"""
        public = deepcopy(status)
        public.pop("mapping_path", None)
        public.pop("template_path", None)
        return public

    def public_template_import_result(self, result: dict) -> dict:
        """隐藏模板导入过程中的临时/本机路径。"""
        public = deepcopy(result)
        for key in ("stored_path", "report_path", "draft_mapping_path"):
            public.pop(key, None)
        return public

    def serve_static(self, path: str) -> None:
        """返回前端静态文件，并阻止路径穿越访问项目外文件。"""
        relative = "index.html" if path in {"", "/"} else unquote(path).lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT not in target.parents and target != STATIC_ROOT:
            return self.json_response({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
        if not target.is_file():
            target = STATIC_ROOT / "index.html"
        return self.file_response(target)

    def file_response(
        self,
        path: Path,
        download_name: str | None = None,
        inline_name: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """统一发送文件响应。

        Excel 走 attachment 下载；PDF 源文件预览走 inline；所有文件都加 nosniff 降低浏览器误判类型的风险。
        """
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        elif inline_name:
            self.send_header("Content-Disposition", f'inline; filename="{inline_name}"')
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def json_response(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        """统一发送 UTF-8 JSON 响应。"""
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF报价单审核工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    retry_thread = threading.Thread(target=retry_alert_worker, daemon=True, name="alert-retry-worker")
    retry_thread.start()
    print(f"Quote Assistant running at http://{args.host}:{args.port}")
    server.serve_forever()


def retry_alert_worker() -> None:
    interval = max(5, int(SERVICE.config.get("alert_retry_interval_seconds", 30)))
    while True:
        try:
            SERVICE.retry_due_alerts()
        except Exception as exc:
            print(f"Alert retry worker error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
