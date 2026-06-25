from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .acceptance import generate_acceptance_report
from .local_workflow import build_default_workflow, format_workflow_error
from .service import QuoteService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.handler(args)
    except Exception as exc:  # CLI boundary: return a clear error instead of a traceback by default.
        print(json.dumps({"ok": False, "error": format_workflow_error(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local quote workflow CLI")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_local = subparsers.add_parser("run-local", help="Run local PDF -> PaddleOCR -> DeepSeek -> job workflow")
    run_local.add_argument("--pdf", type=Path, required=True)
    run_local.add_argument("--reviewer", default="Local Workflow")
    run_local.add_argument("--approve", action="store_true")
    run_local.add_argument("--export", action="store_true")
    run_local.set_defaults(handler=handle_run_local)

    import_template = subparsers.add_parser("import-template", help="Import a local Excel quotation template")
    import_template.add_argument("--template", type=Path, required=True)
    import_template.set_defaults(handler=handle_import_template)

    activate_template = subparsers.add_parser("activate-template", help="Activate a reviewed local template mapping")
    activate_template.add_argument("--mapping-json", type=Path, required=True)
    activate_template.add_argument("--reviewer", required=True)
    activate_template.add_argument("--confirm-format-immutable", action="store_true")
    activate_template.set_defaults(handler=handle_activate_template)

    export_job = subparsers.add_parser("export-job", help="Export an approved job into the active local Excel template")
    export_job.add_argument("--job-id", required=True)
    export_job.set_defaults(handler=handle_export_job)

    acceptance = subparsers.add_parser("acceptance", help="Generate the local acceptance report")
    acceptance.add_argument("--template", type=Path)
    acceptance.add_argument("--mapping-json", type=Path)
    acceptance.add_argument("--normal-pdf", type=Path)
    acceptance.add_argument("--anomaly-pdf", type=Path)
    acceptance.set_defaults(handler=handle_acceptance)
    return parser


def handle_run_local(args: argparse.Namespace) -> dict[str, Any]:
    workflow = build_default_workflow(args.project_root)
    result = workflow.run(args.pdf, reviewer=args.reviewer, approve=args.approve, export=args.export)
    return {"ok": True, **result.to_summary()}


def handle_import_template(args: argparse.Namespace) -> dict[str, Any]:
    service = QuoteService(args.project_root)
    result = service.import_template(args.template.name, args.template.read_bytes())
    return {"ok": True, **_redact_paths(result)}


def handle_activate_template(args: argparse.Namespace) -> dict[str, Any]:
    service = QuoteService(args.project_root)
    mapping = json.loads(args.mapping_json.read_text(encoding="utf-8"))
    result = service.activate_template_mapping(
        {
            "reviewer": args.reviewer,
            "confirm_format_immutable": args.confirm_format_immutable,
            "mapping": mapping,
        }
    )
    return {"ok": True, **_redact_paths(result)}


def handle_export_job(args: argparse.Namespace) -> dict[str, Any]:
    service = QuoteService(args.project_root)
    output = service.export_job(args.job_id)
    return {"ok": True, "job_id": args.job_id, "output_path": str(output)}


def handle_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "report": generate_acceptance_report(
            args.project_root,
            template_path=args.template,
            mapping_path=args.mapping_json,
            normal_pdf=args.normal_pdf,
            anomaly_pdf=args.anomaly_pdf,
        ),
    }


def _redact_paths(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for key in ("stored_path", "report_path", "draft_mapping_path", "template_path", "mapping_path"):
        result.pop(key, None)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
