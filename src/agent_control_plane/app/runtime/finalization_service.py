from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.review_inbox import (
    ReviewInboxDraft,
    ReviewInboxItem,
    ReviewInboxStore,
)
from agent_control_plane.entities.slot import SlotStore, SlotStoreError
from agent_control_plane.features.agent_runner import (
    FinalizationLease,
    GlobalQuotaBroker,
    WorkerLeaseError,
)
from agent_control_plane.features.result_handoff import (
    NativeQualityGateRunner,
    SlotCheckpoint,
    SlotCheckpointError,
    build_verification_bundle,
    checkpoint_changed_files,
    clean_checkpointed_workspace,
    create_slot_checkpoint,
    verify_slot_checkpoint,
)
from agent_control_plane.features.slot_lifecycle import SlotManager
from agent_control_plane.shared.config import ControlConfig
from agent_control_plane.shared.git_tools import (
    GitError,
    compact_status_preview,
    workspace_state,
)
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    inspect_native_quality_contract,
    resolve_native_quality_contract,
)


class FinalizationService:
    """Replay terminal delivery, checkpoint, quota, inbox, and slot-release work."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        store: JobStore,
        slot_store: SlotStore,
        slots: SlotManager,
        review_inbox: ReviewInboxStore,
        quota_broker: GlobalQuotaBroker | None,
        native_quality_runner: NativeQualityGateRunner,
        is_terminal: Callable[[JobRecord], bool],
    ) -> None:
        self.config = config
        self.store = store
        self.slot_store = slot_store
        self.slots = slots
        self.review_inbox = review_inbox
        self.quota_broker = quota_broker
        self.native_quality_runner = native_quality_runner
        self.is_terminal = is_terminal

    def finish(
        self,
        job_id: str,
        status: str,
        last_error: str | None = None,
        *,
        worker_instance_id: str | None = None,
    ) -> JobRecord:
        if worker_instance_id is None:
            self.store.mark_finished(job_id, status, last_error)
        else:
            finished = self.store.mark_finished_by_worker(
                job_id,
                worker_instance_id,
                status,
                last_error,
            )
            if finished is None:
                self.store.add_event(
                    job_id,
                    "warning",
                    f"Stale worker {worker_instance_id} was fenced from terminal transition",
                )
                return self.store.get_job(job_id)
        return self.replay(job_id, allow_inactive=False)

    def replay(self, job_id: str, *, allow_inactive: bool) -> JobRecord:
        job = self.store.get_job(job_id)
        if not self.is_terminal(job):
            raise ValueError(f"Cannot finalize non-terminal job: {job_id}")
        if job.finalization_status == "completed":
            return job
        lease = FinalizationLease(job.run_dir, uuid.uuid4().hex)
        try:
            lease.acquire()
        except WorkerLeaseError:
            self.store.add_event(
                job_id,
                "warning",
                "Terminal finalization is already running in another process",
            )
            return self.store.get_job(job_id)
        try:
            return self._replay_claimed(job_id, allow_inactive=allow_inactive)
        finally:
            lease.release()

    def _replay_claimed(self, job_id: str, *, allow_inactive: bool) -> JobRecord:
        job = self.store.get_job(job_id)
        if not self.is_terminal(job):
            raise ValueError(f"Cannot finalize non-terminal job: {job_id}")
        if job.finalization_status == "completed":
            return job
        if job.finalization_status != "pending":
            job = self.store.prepare_finalization_replay(job_id)
        failure: str | None = None
        if self.quota_broker is not None:
            try:
                self.quota_broker.release(job_id)
            except (OSError, sqlite3.Error) as exc:
                failure = f"Could not release quota lease: {exc}"
        item: ReviewInboxItem | None = None
        try:
            if failure is not None:
                raise sqlite3.OperationalError(failure)
            if job.slot_name:
                item = self.finish_slot_lifecycle(
                    job,
                    job.status,
                    allow_inactive=allow_inactive,
                )
            else:
                item = self._upsert_job_review(
                    job,
                    delivery_status=("ready" if job.status == "completed" else "salvage_ready"),
                    slot_released=False,
                )
            if item is None:
                raise ValueError("Terminal handoff did not create a review inbox item")
            if item.delivery_status in {"inspection_failed", "checkpoint_failed"}:
                raise SlotCheckpointError(
                    item.checkpoint_error or f"Terminal handoff is {item.delivery_status}"
                )
            if job.slot_name:
                slot = self.slot_store.get_slot(job.slot_name)
                if slot is not None and slot.active_job_id == job.job_id:
                    raise ValueError(
                        f"Slot {job.slot_name} is still owned by terminal job {job_id}"
                    )
        except (
            GitError,
            OSError,
            sqlite3.Error,
            SlotCheckpointError,
            SlotStoreError,
            ValueError,
        ) as exc:
            failed = self.store.mark_finalization_failed(job_id, str(exc))
            self.store.add_event(
                job_id,
                "error",
                f"Terminal finalization remains retryable: {exc}",
            )
            return failed
        completed = self.store.mark_finalization_completed(job_id)
        self.store.add_event(job_id, "info", "Terminal finalization completed")
        return completed

    def finish_slot_lifecycle(
        self,
        job: JobRecord,
        job_status: str,
        *,
        force_checkpoint: bool = False,
        allow_inactive: bool = False,
    ) -> ReviewInboxItem | None:
        if job.slot_name is None:
            raise ValueError(f"Job {job.job_id} has no slot to finalize")
        slot = self.slot_store.require_slot(job.slot_name)
        if slot.path.resolve(strict=False) != job.workspace_path.resolve(strict=False):
            raise ValueError(f"Slot {job.slot_name} path changed before finalization: {slot.path}")
        self.slot_store.claim_for_finalization(job.slot_name, job.job_id)
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            note = f"job {job.job_id} finished {job_status}; could not inspect slot: {exc}"
            item = self._upsert_job_review(
                job,
                delivery_status="inspection_failed",
                checkpoint_error=str(exc),
                slot_released=False,
            )
            self._release_slot_status(
                job,
                status="inspection_failed",
                note=note,
                allow_inactive=allow_inactive,
            )
            return item

        should_checkpoint = force_checkpoint or (
            self.config.defaults.terminal_slot_policy == "checkpoint"
        )
        existing_checkpoint = self._existing_job_checkpoint(job)
        if should_checkpoint and not state.dirty and existing_checkpoint is not None:
            return self._release_verified_checkpoint(
                job,
                job_status,
                existing_checkpoint,
                allow_inactive=allow_inactive,
            )
        if state.dirty and should_checkpoint:
            return self._checkpoint_and_release_slot(
                job,
                job_status,
                allow_inactive=allow_inactive,
            )

        slot_status, slot_note = self._slot_release_status(job, job_status)
        if state.dirty:
            delivery_status = (
                "dirty_preserved" if job_status == "completed" else "salvage_dirty_preserved"
            )
        else:
            delivery_status = "ready" if job_status == "completed" else "salvage_ready"
        item = self._upsert_job_review(
            job,
            delivery_status=delivery_status,
            slot_released=False,
        )
        released = self._release_slot_status(
            job,
            status=slot_status,
            note=slot_note,
            allow_inactive=allow_inactive,
        )
        if slot_status == "available" and released:
            item = self._upsert_job_review(
                job,
                delivery_status=delivery_status,
                slot_released=True,
            )
        return item

    def _existing_job_checkpoint(self, job: JobRecord) -> SlotCheckpoint | None:
        try:
            item = self.review_inbox.get(f"agent_job:{job.job_id}")
        except KeyError:
            return None
        if not all(
            (
                item.checkpoint_ref,
                item.checkpoint_sha,
                item.checkpoint_tree_sha,
                item.base_sha,
            )
        ):
            return None
        return SlotCheckpoint(
            job_id=job.job_id,
            task_id=job.task_id,
            terminal_status=job.status,
            workspace_path=job.workspace_path.resolve(strict=False),
            ref_name=str(item.checkpoint_ref),
            commit_sha=str(item.checkpoint_sha),
            tree_sha=str(item.checkpoint_tree_sha),
            base_sha=str(item.base_sha),
        )

    def _release_verified_checkpoint(
        self,
        job: JobRecord,
        job_status: str,
        checkpoint: SlotCheckpoint,
        *,
        allow_inactive: bool,
    ) -> ReviewInboxItem:
        delivery_status = "checkpointed" if job_status == "completed" else "salvage_checkpointed"
        existing_item = self.review_inbox.get(f"agent_job:{job.job_id}")
        try:
            verify_slot_checkpoint(job.workspace_path, checkpoint)
            if workspace_state(job.workspace_path).dirty:
                raise SlotCheckpointError(
                    "Workspace changed while the existing checkpoint was being verified"
                )
        except (GitError, OSError, SlotCheckpointError) as exc:
            self._release_slot_status(
                job,
                status="checkpoint_failed",
                note=f"job {job.job_id} checkpoint verification failed: {exc}",
                allow_inactive=True,
            )
            item = self._upsert_job_review(
                job,
                delivery_status="checkpoint_failed",
                checkpoint=checkpoint,
                checkpoint_error=str(exc),
                slot_released=False,
            )
            self.store.add_event(
                job.job_id,
                "error",
                f"Existing terminal checkpoint failed verification: {exc}",
            )
            return item

        released = self._release_slot_status(
            job,
            status="available",
            note=f"job {job.job_id} checkpoint verified at {checkpoint.ref_name}",
            allow_inactive=allow_inactive,
        )
        return self._upsert_job_review(
            job,
            delivery_status=delivery_status,
            checkpoint=checkpoint,
            slot_released=existing_item.slot_released or released,
        )

    def _checkpoint_and_release_slot(
        self,
        job: JobRecord,
        job_status: str,
        *,
        allow_inactive: bool,
    ) -> ReviewInboxItem | None:
        checkpoint: SlotCheckpoint | None = None
        try:
            checkpoint = create_slot_checkpoint(
                job.workspace_path,
                job_id=job.job_id,
                task_id=job.task_id,
                terminal_status=job_status,
                scratch_root=job.run_dir / "checkpoint",
            )
            delivery_status = (
                "checkpointed" if job_status == "completed" else "salvage_checkpointed"
            )
            contract, contract_error = self._native_quality_contract(job)
            if (
                job_status == "completed"
                and job.workspace_access == "native"
                and not job.read_only
                and contract.policy == "controller"
                and contract_error is None
            ):
                changed_files = tuple(
                    change["path"]
                    for change in checkpoint_changed_files(job.workspace_path, checkpoint)
                )
                if changed_files:
                    self.native_quality_runner.run(
                        workspace_path=job.workspace_path,
                        run_dir=job.run_dir,
                        checkpoint_tree_sha=checkpoint.tree_sha,
                        changed_files=changed_files,
                        contract=contract,
                    )
            self._upsert_job_review(
                job,
                delivery_status=delivery_status,
                checkpoint=checkpoint,
                slot_released=False,
            )
            clean_checkpointed_workspace(
                job.workspace_path,
                checkpoint,
                scratch_root=job.run_dir / "checkpoint",
            )
        except (GitError, OSError, sqlite3.Error, SlotCheckpointError) as exc:
            slot_status, slot_note = self._slot_release_status(job, job_status)
            self._release_slot_status(
                job,
                status=slot_status,
                note=slot_note,
                allow_inactive=allow_inactive,
            )
            try:
                item = self._upsert_job_review(
                    job,
                    delivery_status="checkpoint_failed",
                    checkpoint=checkpoint,
                    checkpoint_error=str(exc),
                    slot_released=False,
                )
            except (OSError, sqlite3.Error):
                item = None
            self.store.add_event(
                job.job_id,
                "error",
                f"Terminal checkpoint cleanup failed safely; slot remains dirty: {exc}",
            )
            return item

        released = self._release_slot_status(
            job,
            status="available",
            note=f"job {job.job_id} checkpointed at {checkpoint.ref_name}",
            allow_inactive=allow_inactive,
        )
        item = self._upsert_job_review(
            job,
            delivery_status=delivery_status,
            checkpoint=checkpoint,
            slot_released=released,
        )
        if released:
            self.store.add_event(
                job.job_id,
                "info",
                (
                    f"Terminal changes checkpointed at {checkpoint.ref_name} "
                    f"({checkpoint.commit_sha}); slot is available"
                ),
            )
        else:
            self.store.add_event(
                job.job_id,
                "warning",
                "Checkpoint is durable and workspace is clean, but the slot registry lock was not released",
            )
        return item

    def _upsert_job_review(
        self,
        job: JobRecord,
        *,
        delivery_status: str,
        checkpoint: SlotCheckpoint | None = None,
        checkpoint_error: str | None = None,
        slot_released: bool,
    ) -> ReviewInboxItem:
        result_text = _read_result_text(job.result_path, fallback=job.last_error)
        quality_contract, quality_contract_error = self._native_quality_contract(job)
        verification_bundle = build_verification_bundle(
            job.result_path,
            workspace_path=job.workspace_path,
            checkpoint=checkpoint,
            checkpoint_error=checkpoint_error,
            run_dir=job.run_dir,
            source_status=job.status,
            workspace_changed=(
                True if checkpoint is not None or "dirty" in delivery_status else None
            ),
            quality_contract=quality_contract,
            quality_contract_error=quality_contract_error,
        )
        return self.review_inbox.upsert(
            ReviewInboxDraft(
                source_kind="agent_job",
                source_id=job.job_id,
                source_status=job.status,
                source_completed_at=job.finished_at,
                delivery_status=delivery_status,
                task_id=job.task_id,
                route=job.route,
                workspace_path=job.workspace_path,
                slot_name=job.slot_name,
                result_path=job.result_path,
                checkpoint_ref=checkpoint.ref_name if checkpoint else None,
                checkpoint_sha=checkpoint.commit_sha if checkpoint else None,
                checkpoint_tree_sha=checkpoint.tree_sha if checkpoint else None,
                base_sha=checkpoint.base_sha if checkpoint else None,
                result_excerpt=result_text,
                result_text=result_text,
                verification_bundle=verification_bundle,
                checkpoint_error=checkpoint_error,
                slot_released=slot_released,
            )
        )

    def _native_quality_contract(
        self,
        job: JobRecord,
    ) -> tuple[NativeQualityContract, str | None]:
        expected = resolve_native_quality_contract(
            self.config,
            job.route,
            workspace_access=job.workspace_access,
            read_only=job.read_only,
        )
        inspection = inspect_native_quality_contract(job.run_dir, expected)
        return expected, inspection.error

    def _release_slot_status(
        self,
        job: JobRecord,
        *,
        status: str,
        note: str | None,
        allow_inactive: bool,
    ) -> bool:
        if job.slot_name is None:
            return False
        record = self.slot_store.get_slot(job.slot_name)
        if record is None:
            return False
        if record.active_job_id == job.job_id:
            return (
                self.slots.release_for_job(
                    job.slot_name,
                    job_id=job.job_id,
                    status=status,
                    note=note,
                )
                is not None
            )
        if record.active_job_id is None and allow_inactive:
            self.slot_store.mark_status(job.slot_name, status, note=note)
            return status == "available"
        return False

    @staticmethod
    def _slot_release_status(job: JobRecord, job_status: str) -> tuple[str, str | None]:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return (
                "inspection_failed",
                f"job {job.job_id} finished {job_status}; could not inspect slot: {exc}",
            )
        if not state.porcelain:
            return "available", None

        dirty_preview = compact_status_preview(state.porcelain)
        if job_status == "stopped_dirty_after_failure":
            return (
                "dirty_after_failure",
                f"job {job.job_id} stopped with dirty workspace: {dirty_preview}",
            )
        return (
            "dirty_after_job",
            f"job {job.job_id} finished {job_status} with dirty workspace: {dirty_preview}",
        )


def _read_result_text(path: Path, *, fallback: str | None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback
    return text or fallback
