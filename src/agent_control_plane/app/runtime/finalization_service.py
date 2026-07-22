from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
    checked_out_checkpoint_worktree,
    checkpoint_changed_files,
    checkpoint_temporary_patch_artifacts,
    clean_checkpointed_workspace,
    create_slot_checkpoint,
    parse_result_report,
    verify_clean_workspace_tree,
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

    def requalify(self, item_id: str) -> ReviewInboxItem:
        """Re-run controller quality gates against a durable checkpoint and rebuild the bundle.

        Never moves branches, never mutates the slot workspace, and never
        touches the record until the rebuilt bundle is ready to persist:
        any failure below leaves the existing inbox record unchanged.
        """
        item = self.review_inbox.get(item_id)
        if item.review_status != "pending":
            raise ValueError(
                f"Review item {item_id} is already {item.review_status} and cannot be requalified"
            )
        if item.source_kind != "agent_job":
            raise ValueError(f"Review item {item_id} has no requalifiable job checkpoint")
        has_checkpoint = all(
            (item.checkpoint_ref, item.checkpoint_sha, item.checkpoint_tree_sha, item.base_sha)
        )
        clean_tree_sha = None if has_checkpoint else _existing_clean_tree_sha(item)
        if not has_checkpoint and clean_tree_sha is None:
            raise ValueError(
                f"Review item {item_id} has no durable checkpoint or clean-tree evidence "
                "to requalify against"
            )
        job = self.store.get_job(item.source_id)
        if has_checkpoint:
            verification_bundle = self._requalify_checkpoint(item_id, item, job)
        else:
            verification_bundle = self._requalify_clean_tree(item_id, job, str(clean_tree_sha))
        updated = self.review_inbox.requalify(item_id, verification_bundle)
        self.store.add_event(
            job.job_id,
            "info",
            f"Review inbox item {item_id} requalified "
            f"(review_ready={verification_bundle.get('review_ready')})",
        )
        return updated

    def _requalify_checkpoint(
        self,
        item_id: str,
        item: ReviewInboxItem,
        job: JobRecord,
    ) -> dict[str, Any]:
        checkpoint = SlotCheckpoint(
            job_id=job.job_id,
            task_id=job.task_id,
            terminal_status=job.status,
            workspace_path=job.workspace_path.resolve(strict=False),
            ref_name=str(item.checkpoint_ref),
            commit_sha=str(item.checkpoint_sha),
            tree_sha=str(item.checkpoint_tree_sha),
            base_sha=str(item.base_sha),
        )
        try:
            verify_slot_checkpoint(job.workspace_path, checkpoint)
        except (GitError, OSError, SlotCheckpointError) as exc:
            raise ValueError(
                f"Checkpoint ref for {item_id} is no longer resolvable: {exc}"
            ) from exc
        contract, contract_error = self._native_quality_contract(job)
        if contract_error is not None:
            raise ValueError(f"Native quality contract for {item_id} is invalid: {contract_error}")
        checkpoint_changes = checkpoint_changed_files(job.workspace_path, checkpoint)
        changed_files = tuple(change["path"] for change in checkpoint_changes)
        command_files = tuple(
            change["path"] for change in checkpoint_changes if not change["status"].startswith("D")
        )
        if contract.policy == "controller" and job.controller_gate_mode != "none" and changed_files:
            with checked_out_checkpoint_worktree(
                job.workspace_path,
                checkpoint,
                scratch_root=job.run_dir / "requalify",
            ) as worktree:
                self.native_quality_runner.run(
                    workspace_path=worktree,
                    run_dir=job.run_dir,
                    checkpoint_tree_sha=checkpoint.tree_sha,
                    changed_files=changed_files,
                    command_files=command_files,
                    contract=contract,
                    controller_gate_mode=job.controller_gate_mode,
                )
                if workspace_state(worktree).dirty:
                    raise SlotCheckpointError(
                        f"Controller quality gate mutated the checkpoint checkout for {item_id}; "
                        "requalify aborted"
                    )
        return build_verification_bundle(
            job.result_path,
            workspace_path=job.workspace_path,
            checkpoint=checkpoint,
            run_dir=job.run_dir,
            source_status=job.status,
            workspace_changed=True,
            quality_contract=contract,
            quality_contract_error=None,
            expected_result_status=job.expected_result_status,
            controller_gate_mode=job.controller_gate_mode,
        )

    def _requalify_clean_tree(
        self,
        item_id: str,
        job: JobRecord,
        clean_tree_sha: str,
    ) -> dict[str, Any]:
        try:
            current_tree = verify_clean_workspace_tree(job.workspace_path)
        except (GitError, SlotCheckpointError) as exc:
            raise ValueError(
                f"Clean-tree workspace for {item_id} could not be verified: {exc}"
            ) from exc
        if current_tree != clean_tree_sha:
            raise ValueError(
                f"HEAD moved for {item_id} since the recorded clean-tree evidence; "
                "nothing to materialize for requalify"
            )
        contract, contract_error = self._native_quality_contract(job)
        if contract_error is not None:
            raise ValueError(f"Native quality contract for {item_id} is invalid: {contract_error}")
        if contract.policy == "controller" and job.controller_gate_mode != "none":
            self.native_quality_runner.run(
                workspace_path=job.workspace_path,
                run_dir=job.run_dir,
                checkpoint_tree_sha=clean_tree_sha,
                changed_files=(),
                command_files=(),
                contract=contract,
                controller_gate_mode=job.controller_gate_mode,
                full_suite=True,
            )
            after_tree = verify_clean_workspace_tree(job.workspace_path)
            if after_tree != clean_tree_sha:
                raise SlotCheckpointError(
                    f"Controller quality gates mutated the clean tree for {item_id}; "
                    "requalify aborted"
                )
        return build_verification_bundle(
            job.result_path,
            workspace_path=job.workspace_path,
            checkpoint=None,
            run_dir=job.run_dir,
            source_status=job.status,
            workspace_changed=False,
            clean_tree_sha=clean_tree_sha,
            quality_contract=contract,
            quality_contract_error=None,
            expected_result_status=job.expected_result_status,
            controller_gate_mode=job.controller_gate_mode,
        )

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

        workspace_disposition = "clean"
        if state.dirty:
            workspace_disposition = (
                "dirty_after_failure" if job.runner_failure else "dirty_after_job"
            )
        job = self.store.set_workspace_disposition(job.job_id, workspace_disposition)
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
        if not state.dirty and job_status == "completed":
            clean_tree_item = self._release_clean_tree_quality(job, allow_inactive=allow_inactive)
            if clean_tree_item is not None:
                return clean_tree_item

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

        self._preserve_checkpoint_salvage(job)
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
            self._preserve_checkpoint_salvage(job)
            delivery_status = (
                "checkpointed" if job_status == "completed" else "salvage_checkpointed"
            )
            temporary_patch_artifacts = checkpoint_temporary_patch_artifacts(
                job.workspace_path,
                checkpoint,
            )
            if temporary_patch_artifacts:
                paths = ", ".join(temporary_patch_artifacts)
                self.store.set_checkpoint_disposition(job.job_id, "contaminated")
                self._upsert_job_review(
                    job,
                    delivery_status="checkpoint_contaminated",
                    checkpoint=checkpoint,
                    slot_released=False,
                )
                self._release_slot_status(
                    job,
                    status="contaminated",
                    note=(
                        f"job {job.job_id} checkpoint contains temporary patch artifacts: {paths}"
                    ),
                    allow_inactive=allow_inactive,
                )
                self.store.add_event(
                    job.job_id,
                    "warning",
                    f"Terminal checkpoint preserved but quarantined due to temporary artifacts: {paths}",
                )
                return self.review_inbox.get(f"agent_job:{job.job_id}")
            contract, contract_error = self._native_quality_contract(job)
            result_text = _read_result_text(job.result_path, fallback=None)
            result_report = parse_result_report(result_text) if result_text is not None else None
            if (
                job_status == "completed"
                and job.expected_result_status == "completed"
                and result_report is not None
                and result_report["status"] == "completed"
                and job.workspace_access == "native"
                and not job.read_only
                and contract.policy == "controller"
                and contract_error is None
                and job.controller_gate_mode != "none"
            ):
                checkpoint_changes = checkpoint_changed_files(job.workspace_path, checkpoint)
                changed_files = tuple(change["path"] for change in checkpoint_changes)
                command_files = tuple(
                    change["path"]
                    for change in checkpoint_changes
                    if not change["status"].startswith("D")
                )
                if changed_files:
                    self.native_quality_runner.run(
                        workspace_path=job.workspace_path,
                        run_dir=job.run_dir,
                        checkpoint_tree_sha=checkpoint.tree_sha,
                        changed_files=changed_files,
                        command_files=command_files,
                        contract=contract,
                        controller_gate_mode=job.controller_gate_mode,
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

    def _release_clean_tree_quality(
        self,
        job: JobRecord,
        *,
        allow_inactive: bool,
    ) -> ReviewInboxItem | None:
        """Produce controller-quality evidence for a genuinely clean, zero-change job.

        Returns None (leaving the caller's plain "ready" path in place) when the
        job is not eligible for controller quality evidence at all, so this is a
        pure addition: it never makes an otherwise-uncovered job worse.
        """
        contract, contract_error = self._native_quality_contract(job)
        result_text = _read_result_text(job.result_path, fallback=None)
        result_report = parse_result_report(result_text) if result_text is not None else None
        eligible = (
            job.expected_result_status == "completed"
            and result_report is not None
            and result_report["status"] == "completed"
            and job.workspace_access == "native"
            and not job.read_only
            and contract.policy == "controller"
            and contract_error is None
            and job.controller_gate_mode != "none"
        )
        if not eligible:
            return None
        try:
            clean_tree_sha = verify_clean_workspace_tree(job.workspace_path)
        except (GitError, SlotCheckpointError):
            return None

        checkpoint_error: str | None = None
        try:
            self.native_quality_runner.run(
                workspace_path=job.workspace_path,
                run_dir=job.run_dir,
                checkpoint_tree_sha=clean_tree_sha,
                changed_files=(),
                command_files=(),
                contract=contract,
                controller_gate_mode=job.controller_gate_mode,
                full_suite=True,
            )
            after_state = workspace_state(job.workspace_path)
            if after_state.dirty:
                checkpoint_error = (
                    "controller quality gates left the clean workspace dirty; "
                    "clean-tree evidence rejected"
                )
            else:
                after_tree = verify_clean_workspace_tree(job.workspace_path)
                if after_tree != clean_tree_sha:
                    checkpoint_error = (
                        "HEAD tree changed while controller quality gates ran against "
                        "the clean tree; evidence rejected"
                    )
        except (GitError, OSError, SlotCheckpointError) as exc:
            checkpoint_error = f"controller quality gates against the clean tree failed: {exc}"

        if checkpoint_error is not None:
            # The workspace was verified clean before the gates ran, so anything wrong
            # now was introduced by the gate run itself. That is still an environment
            # integrity failure, not merely a failed check: never hand the slot to
            # another job in this state, same fail-closed posture as a checkpoint whose
            # workspace changed underneath it.
            item = self._upsert_job_review(
                job,
                delivery_status="checkpoint_failed",
                checkpoint_error=checkpoint_error,
                slot_released=False,
            )
            self._release_slot_status(
                job,
                status="checkpoint_failed",
                note=f"job {job.job_id} clean-tree controller quality evidence failed: {checkpoint_error}",
                allow_inactive=True,
            )
            self.store.add_event(
                job.job_id,
                "error",
                f"Clean-tree controller quality evidence failed: {checkpoint_error}",
            )
            return item

        delivery_status = "ready"
        item = self._upsert_job_review(
            job,
            delivery_status=delivery_status,
            clean_tree_sha=clean_tree_sha,
            slot_released=False,
        )
        slot_status, slot_note = self._slot_release_status(job, job.status)
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
                clean_tree_sha=clean_tree_sha,
                slot_released=True,
            )
        return item

    def _preserve_checkpoint_salvage(self, job: JobRecord) -> None:
        existing = self.store.get_job(job.job_id).checkpoint_disposition
        if existing in {"contaminated", "continuation_verified", "final_accepted"}:
            return
        self.store.set_checkpoint_disposition(job.job_id, "salvage")

    def _upsert_job_review(
        self,
        job: JobRecord,
        *,
        delivery_status: str,
        checkpoint: SlotCheckpoint | None = None,
        checkpoint_error: str | None = None,
        clean_tree_sha: str | None = None,
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
                False
                if clean_tree_sha is not None
                else (True if checkpoint is not None or "dirty" in delivery_status else None)
            ),
            clean_tree_sha=clean_tree_sha,
            quality_contract=quality_contract,
            quality_contract_error=quality_contract_error,
            expected_result_status=job.expected_result_status,
            controller_gate_mode=job.controller_gate_mode,
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


def _existing_clean_tree_sha(item: ReviewInboxItem) -> str | None:
    bundle = item.verification_bundle
    if not isinstance(bundle, dict):
        return None
    artifact = bundle.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != "clean_tree":
        return None
    clean_tree_sha = artifact.get("clean_tree_sha")
    return clean_tree_sha if isinstance(clean_tree_sha, str) and clean_tree_sha else None
