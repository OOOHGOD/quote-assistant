from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def save(self, job: dict[str, Any]) -> None:
        with self._lock:
            directory = self.job_dir(job["id"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "job.json"
            temporary = directory / ".job.json.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self.job_dir(job_id) / "job.json"
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = []
            for path in self.root.glob("*/job.json"):
                try:
                    jobs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(jobs, key=lambda job: job.get("created_at", ""), reverse=True)
