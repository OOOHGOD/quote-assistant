from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from .models import utc_now


def emit_alert(
    job: dict[str, Any],
    job_dir: Path,
    *,
    event: str = "quote_recognition_anomaly",
    issues: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
    alert_issues = issues if issues is not None else job["validation"]["issues"]
    payload = {
        "event": event,
        "created_at": utc_now(),
        "job_id": job["id"],
        "source_file": job["source_file"],
        "status": job["status"],
        "blocking_issue_count": sum(1 for entry in alert_issues if entry.get("severity") in {"error", "critical"}),
        "issues": alert_issues,
        "details": details or {},
    }
    alert = {
        "sequence": sequence,
        "payload": payload,
        "delivery": {
            "channel": "local",
            "success": True,
            "attempts": 0,
            "local_path": str(_alert_path(job_dir, sequence, event)),
        },
    }
    alert, _ = retry_alert_delivery(alert, job_dir, force=True)
    persist_alert(alert, job_dir)
    return alert


def retry_alert_delivery(
    alert: dict[str, Any],
    job_dir: Path,
    *,
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    delivery = dict(alert.get("delivery") or {})
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        delivery.update({"channel": "local", "success": True, "attempts": int(delivery.get("attempts") or 0)})
        delivery.pop("next_retry_at", None)
        alert["delivery"] = delivery
        return alert, False
    if delivery.get("success") is True and delivery.get("channel") == "webhook":
        return alert, False
    if not force and not alert_delivery_due(alert):
        return alert, False

    payload = alert["payload"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Quote-Alert-Event": str(payload.get("event") or "")}
    secret = os.environ.get("ALERT_WEBHOOK_SECRET", "")
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Quote-Alert-Signature"] = f"sha256={signature}"

    attempts = int(delivery.get("attempts") or 0) + 1
    attempted_at = utc_now()
    delivery.update({
        "channel": "webhook",
        "success": False,
        "attempts": attempts,
        "last_attempt_at": attempted_at,
        "local_path": delivery.get("local_path") or str(_alert_path(job_dir, int(alert.get("sequence") or 1), str(payload.get("event") or "alert"))),
    })
    delivery.pop("status", None)
    delivery.pop("error", None)
    try:
        timeout = max(0.1, float(os.environ.get("ALERT_WEBHOOK_TIMEOUT_SECONDS", "8")))
        req = request.Request(webhook_url, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=timeout) as response:
            delivery["status"] = response.status
            delivery["success"] = 200 <= response.status < 300
    except error.HTTPError as exc:
        delivery["status"] = exc.code
        delivery["error"] = str(exc)
    except Exception as exc:  # Local persistence remains authoritative when the network is unavailable.
        delivery["error"] = str(exc)

    if delivery["success"]:
        delivery.pop("next_retry_at", None)
    else:
        delivery["next_retry_at"] = _next_retry_at(attempted_at, attempts)
    alert["delivery"] = delivery
    return alert, True


def alert_delivery_due(alert: dict[str, Any], now: datetime | None = None) -> bool:
    delivery = alert.get("delivery") or {}
    if delivery.get("channel") != "webhook" or delivery.get("success") is True:
        return False
    next_retry_at = delivery.get("next_retry_at")
    if not next_retry_at:
        return True
    try:
        due_at = datetime.fromisoformat(str(next_retry_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    return due_at <= (now or datetime.now(timezone.utc))


def persist_alert(alert: dict[str, Any], job_dir: Path, *, latest: bool = True) -> None:
    payload = alert.get("payload") or {}
    sequence = int(alert.get("sequence") or 1)
    path = _alert_path(job_dir, sequence, str(payload.get("event") or "alert"))
    alert.setdefault("delivery", {})["local_path"] = str(path)
    content = json.dumps(alert, ensure_ascii=False, indent=2)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    if latest:
        latest_path = job_dir / "alert.json"
        latest_temporary = job_dir / ".alert.json.tmp"
        latest_temporary.write_text(content, encoding="utf-8")
        latest_temporary.replace(latest_path)


def _alert_path(job_dir: Path, sequence: int, event: str) -> Path:
    safe_event = "".join(character if character.isalnum() or character in "-_" else "-" for character in event)
    return job_dir / f"alert-{sequence:03d}-{safe_event}.json"


def _next_retry_at(attempted_at: str, attempts: int) -> str:
    base_seconds = max(1, int(os.environ.get("ALERT_RETRY_BASE_SECONDS", "30")))
    max_seconds = max(base_seconds, int(os.environ.get("ALERT_RETRY_MAX_SECONDS", "3600")))
    delay_seconds = min(max_seconds, base_seconds * (2 ** max(0, attempts - 1)))
    attempted = datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
    return (attempted + timedelta(seconds=delay_seconds)).isoformat().replace("+00:00", "Z")
