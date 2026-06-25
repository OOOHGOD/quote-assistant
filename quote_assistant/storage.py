"""本地 JSON 任务存储。

每个任务一个目录：`data/jobs/<job_id>/job.json`，源 PDF、OCR 产物和告警文件也放在同一目录下。
这里使用原子替换写入，减少程序中断时产生半截 JSON 的概率。
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any


class JobStore:
    """线程安全的本地 job.json 读写器。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def job_dir(self, job_id: str) -> Path:
        """返回某个任务的目录路径。"""
        return self.root / job_id

    def save(self, job: dict[str, Any]) -> None:
        """保存任务；先写临时文件，再 replace 成正式 job.json。"""
        with self._lock:
            directory = self.job_dir(job["id"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "job.json"
            temporary = directory / ".job.json.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """读取单个任务，不存在时返回 None。"""
        with self._lock:
            path = self.job_dir(job_id) / "job.json"
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        """读取全部任务，并按创建时间倒序返回。"""
        with self._lock:
            jobs = []
            for path in self.root.glob("*/job.json"):
                try:
                    jobs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(jobs, key=lambda job: job.get("created_at", ""), reverse=True)
