from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.shared.clock import utc_now


class ArchiveService:
    """Move eligible terminal run directories while preserving durable job paths."""

    def __init__(
        self,
        *,
        store: JobStore,
        runs_root: Path,
        is_terminal: Callable[[JobRecord], bool],
        clock: Callable[[], float],
    ) -> None:
        self.store = store
        self.runs_root = runs_root
        self.is_terminal = is_terminal
        self.clock = clock

    def archive(
        self,
        *,
        older_than_days: int = 14,
        limit: int = 50,
        apply: bool = False,
    ) -> list[dict[str, Any]]:
        if older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff = self.clock() - older_than_days * 24 * 60 * 60
        decisions: list[dict[str, Any]] = []
        for job in self.store.list_jobs(limit):
            decision = self._decision(job, cutoff, apply=apply)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _decision(
        self,
        job: JobRecord,
        cutoff: float,
        *,
        apply: bool,
    ) -> dict[str, Any] | None:
        if not self.is_terminal(job) or job.archived_at is not None:
            return None
        archived_from_timestamp = _job_archive_timestamp(job, fallback=self.clock())
        if archived_from_timestamp > cutoff:
            return None
        archive_dir = (
            self.runs_root
            / "_archive"
            / _date_bucket_from_timestamp(archived_from_timestamp)
            / job.job_id
        )
        decision: dict[str, Any] = {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "status": job.status,
            "backend": job.backend,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
            "run_dir": str(job.run_dir),
            "archive_dir": str(archive_dir),
            "apply": apply,
            "action": "would_archive",
        }
        runs_root = self.runs_root.resolve(strict=False)
        run_dir = job.run_dir.resolve(strict=False)
        if run_dir == runs_root or not run_dir.is_relative_to(runs_root):
            decision["action"] = "blocked"
            decision["reason"] = (
                f"Run directory is outside configured runs root {runs_root}: {run_dir}"
            )
            return decision
        if not apply:
            return decision
        if archive_dir.exists():
            decision["action"] = "blocked"
            decision["reason"] = f"Archive path already exists: {archive_dir}"
            return decision

        updates: dict[str, Any] = {"archived_at": utc_now()}
        if job.run_dir.exists():
            archive_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(job.run_dir), str(archive_dir))
            except OSError as exc:
                decision["action"] = "failed"
                decision["reason"] = str(exc)
                return decision
            updates["run_dir"] = archive_dir
            prompt_relative = _path_relative_to(job.prompt_path, job.run_dir)
            if prompt_relative is not None:
                updates["prompt_path"] = archive_dir / prompt_relative
            if job.log_path is not None:
                log_relative = _path_relative_to(job.log_path, job.run_dir)
                if log_relative is not None:
                    updates["log_path"] = archive_dir / log_relative
        else:
            decision["warning"] = f"Run directory does not exist: {job.run_dir}"

        archived = self.store.update_job(job.job_id, **updates)
        decision["action"] = "archived"
        decision["run_dir"] = str(archived.run_dir)
        decision["archived_at"] = archived.archived_at
        return decision


def _date_bucket_from_timestamp(timestamp: float) -> Path:
    moment = datetime.fromtimestamp(timestamp, UTC)
    return Path(f"{moment:%Y}") / f"{moment:%m}" / f"{moment:%d}"


def _job_archive_timestamp(job: JobRecord, *, fallback: float) -> float:
    raw = job.finished_at or job.updated_at or job.created_at
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def _path_relative_to(path: Path, parent: Path) -> Path | None:
    try:
        return path.relative_to(parent)
    except ValueError:
        return None
