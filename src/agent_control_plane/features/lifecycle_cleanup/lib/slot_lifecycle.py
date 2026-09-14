from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.review_inbox import ReviewInboxItem, ReviewInboxStore
from agent_control_plane.entities.slot import SlotRecord, SlotStore, SlotStoreError
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.config import ControlConfig
from agent_control_plane.shared.git_tools import GitError, run_git, workspace_snapshot
from agent_control_plane.shared.path_rules import is_same_or_child
from agent_control_plane.shared.process_liveness import (
    process_is_alive as shared_process_is_alive,
)
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database


class LifecycleClass(StrEnum):
    ACTIVE = "active"
    DIRTY = "dirty"
    UNIQUE_UNPUSHED = "unique-unpushed"
    ACCEPTED_INTEGRATED = "accepted-integrated"
    SAFE_TO_DELETE = "safe-to-delete"
    QUARANTINED = "quarantined"
    STALE = "stale"
    RETAINED_UNOWNED = "retained-unowned"
    AVAILABLE = "available"


@dataclass(frozen=True)
class FrozenSlot:
    name: str
    route: str
    path: Path
    generation: int
    device: int
    inode: int
    branch: str
    head: str
    canonical_ref: str
    canonical_tip: str
    canonical_url: str
    common_git_dir: str
    checkpoint_ref: str
    checkpoint_sha: str
    default_branch: str
    default_ref: str
    default_sha: str | None
    job_id: str
    base_sha: str | None = None

    @property
    def operation_id(self) -> str:
        payload = json.dumps(
            [
                self.name,
                self.route,
                str(self.path),
                self.generation,
                self.head,
                self.canonical_ref,
                self.canonical_tip,
                self.canonical_url,
                self.common_git_dir,
                self.checkpoint_ref,
                self.default_branch,
                self.default_ref,
                self.default_sha,
                self.job_id,
                self.base_sha,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class FrozenBranch:
    route: str
    branch: str
    sha: str
    canonical_ref: str
    canonical_tip: str
    canonical_url: str
    common_git_dir: str
    receipt_source: str
    receipt_id: str
    kind: str = "branch"

    @property
    def operation_id(self) -> str:
        payload = json.dumps(
            [
                self.kind,
                self.route,
                self.branch,
                self.sha,
                self.canonical_ref,
                self.canonical_tip,
                self.canonical_url,
                self.common_git_dir,
                self.receipt_source,
                self.receipt_id,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class SlotLifecycleService:
    """Audit all ACP slots and execute only exact, revalidated cleanup intents."""

    def __init__(
        self,
        config: ControlConfig,
        *,
        slots: SlotStore,
        jobs: JobStore,
        inbox: ReviewInboxStore,
        live_cwds: Callable[[Path], list[int]] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        process_is_alive: Callable[[int | None], bool] | None = None,
        legacy_database: Path | Sequence[Path] | None = None,
        legacy_databases: Sequence[Path] | None = None,
    ) -> None:
        self.config = config
        self.slots = slots
        self.jobs = jobs
        self.inbox = inbox
        self.live_cwds = live_cwds or _live_process_cwds
        self.clock = clock
        self.sleep = sleep
        self.process_is_alive = process_is_alive or shared_process_is_alive

        self.legacy_databases: list[Path] = []
        if legacy_databases is not None:
            raw_dbs = list(legacy_databases)
        elif legacy_database is not None:
            if isinstance(legacy_database, (str, Path)):
                raw_dbs = [Path(legacy_database)]
            else:
                raw_dbs = [Path(p) for p in legacy_database]
        else:
            raw_dbs = [
                self.config.coordination_root / "legacy" / "control-plane" / "shared-jobs.sqlite3",
                self.config.database_path.parent.parent
                / "legacy"
                / "control-plane"
                / "shared-jobs.sqlite3",
                self.config.worktree_base / "runs" / "jobs.sqlite3",
                self.config.coordination_root / "runs" / "jobs.sqlite3",
            ]

        seen_dbs: set[Path] = set()
        curr_db_resolved = self.config.database_path.resolve(strict=False)
        for cand in raw_dbs:
            p = Path(cand)
            if p.exists():
                res = p.resolve(strict=False)
                if res != curr_db_resolved and res not in seen_dbs:
                    seen_dbs.add(res)
                    self.legacy_databases.append(res)
        self.legacy_databases.sort(key=lambda p: str(p))
        self._initialize()

    @property
    def legacy_database(self) -> Path | None:
        return self.legacy_databases[0] if self.legacy_databases else None

    def audit(self, *, refresh: bool = True) -> dict[str, Any]:
        self.slots.initialize()
        self.jobs.initialize()
        self.inbox.initialize()
        registered = self.slots.list_slots()
        route_cache = self._fetch_canonical_routes(refresh=refresh)
        rows = [self._audit_slot(slot, route_cache=route_cache) for slot in registered]
        rows.extend(self._unmanaged_resources(registered, route_cache=route_cache))
        counts = {value.value: 0 for value in LifecycleClass}
        for row in rows:
            counts[row["classification"]] += 1
        payload = {
            "observed_at": utc_now(),
            "refresh": refresh,
            "counts": counts,
            "resources": rows,
        }
        self._event("audit", None, payload)
        return payload

    def _fetch_canonical_routes(self, *, refresh: bool) -> dict[str, dict[str, Any]]:
        route_cache: dict[str, dict[str, Any]] = {}
        for route_name, route in self.config.routes.items():
            remote = route.canonical_remote
            branch = route.canonical_branch
            if not remote or not branch:
                route_cache[route_name] = {
                    "error": "canonical_remote and canonical_branch are not explicitly configured"
                }
                continue
            canonical_ref = f"refs/remotes/{remote}/{branch}"
            try:
                if refresh:
                    run_git(
                        route.path,
                        "fetch",
                        "--no-tags",
                        remote,
                        f"refs/heads/{branch}:{canonical_ref}",
                    )
                tip = run_git(route.path, "rev-parse", "--verify", canonical_ref)
                url = run_git(route.path, "remote", "get-url", remote)
                common_git_dir = str(
                    (route.path / run_git(route.path, "rev-parse", "--git-common-dir")).resolve(
                        strict=False
                    )
                )
                try:
                    wt_raw = run_git(route.path, "worktree", "list", "--porcelain")
                    worktrees = {
                        Path(line[9:].strip()).resolve(strict=False)
                        for line in wt_raw.splitlines()
                        if line.startswith("worktree ")
                    }
                except GitError:
                    worktrees = set()
                route_cache[route_name] = {
                    "remote": remote,
                    "branch": branch,
                    "canonical_ref": canonical_ref,
                    "canonical_tip": tip,
                    "canonical_url": url,
                    "common_git_dir": common_git_dir,
                    "worktrees": worktrees,
                    "error": None,
                }
            except GitError as exc:
                route_cache[route_name] = {"error": f"canonical refresh failed: {exc}"}
        return route_cache

    def _unmanaged_resources(
        self, registered: list[SlotRecord], route_cache: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        registered_paths = {slot.path.resolve(strict=False) for slot in registered}
        registered_branches: set[tuple[str, str]] = set()
        for slot in registered:
            try:
                branch = run_git(slot.path, "branch", "--show-current")
            except GitError:
                continue
            if branch:
                registered_branches.add((slot.route, branch))
        for route_name, route in self.config.routes.items():
            try:
                worktrees = _parse_worktrees(run_git(route.path, "worktree", "list", "--porcelain"))
                branches = run_git(
                    route.path,
                    "for-each-ref",
                    "--format=%(refname:short)%00%(objectname)",
                    "refs/heads",
                ).splitlines()
            except GitError as exc:
                rows.append(
                    {
                        "kind": "repository",
                        "route": route_name,
                        "classification": LifecycleClass.QUARANTINED.value,
                        "reasons": [f"inventory failed: {exc}"],
                    }
                )
                continue
            for worktree in worktrees:
                path = Path(worktree["path"]).resolve(strict=False)
                if path in registered_paths or path == route.path.resolve(strict=False):
                    continue
                dirty = "unknown"
                with suppress(GitError):
                    dirty = run_git(path, "status", "--porcelain=v1", "-uall")
                rows.append(
                    {
                        "kind": "worktree",
                        "route": route_name,
                        "path": str(path),
                        "branch": worktree.get("branch"),
                        "classification": (
                            LifecycleClass.DIRTY.value
                            if dirty not in {"", "unknown"}
                            else LifecycleClass.STALE.value
                        ),
                        "reasons": ["unregistered worktree; ownership is not proven"],
                    }
                )
            r_info = route_cache.get(route_name, {})
            canonical_ref = r_info.get("canonical_ref")
            worktree_branches = {wt.get("branch") for wt in worktrees if wt.get("branch")}
            evidence = self._load_route_evidence(route_name, route.path)
            for raw in branches:
                if "\0" not in raw:
                    continue
                branch, sha = raw.split("\0", 1)
                if branch == route.required_branch or (route_name, branch) in registered_branches:
                    continue
                is_protected = (
                    branch in ("main", "master", "dev", route.canonical_branch)
                    or branch in worktree_branches
                    or branch.startswith("slot/")
                )
                is_ancestor = bool(canonical_ref and _is_ancestor(route.path, sha, canonical_ref))
                is_patch = bool(
                    canonical_ref
                    and not is_ancestor
                    and _is_patch_equivalent(route.path, sha, canonical_ref)
                )
                branch_tree = _commit_tree(route.path, sha)
                is_tree = bool(
                    canonical_ref
                    and not (is_ancestor or is_patch)
                    and branch_tree
                    and _has_canonical_tree(route.path, branch_tree, canonical_ref)
                )
                integrated = is_ancestor or is_patch or is_tree

                acc_entry = evidence["accepted_shas"].get(sha) or evidence["accepted_branches"].get(
                    branch
                )
                rej_entry = evidence["rejected_shas"].get(sha) or evidence["rejected_branches"].get(
                    branch
                )

                row = self._classify_branch(
                    route_name=route_name,
                    branch=branch,
                    sha=sha,
                    canonical_ref=canonical_ref,
                    r_info=r_info,
                    is_protected=is_protected,
                    integrated=integrated,
                    acc_entry=acc_entry,
                    rej_entry=rej_entry,
                    schema_errors=evidence["schema_errors"],
                )
                rows.append(row)
        return rows

    def _classify_branch(
        self,
        *,
        route_name: str,
        branch: str,
        sha: str,
        canonical_ref: str | None,
        r_info: dict[str, Any],
        is_protected: bool,
        integrated: bool,
        acc_entry: tuple[str, str, str] | None,
        rej_entry: tuple[str, str, str] | None,
        schema_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        effective_review = "none"
        if rej_entry and not acc_entry:
            effective_review = "rejected"
        elif rej_entry and acc_entry:
            acc_ts = acc_entry[2] or ""
            rej_ts = rej_entry[2] or ""
            if acc_ts and rej_ts and acc_ts > rej_ts:
                effective_review = "accepted"
            else:
                effective_review = "contradictory"
        elif acc_entry:
            effective_review = "accepted"

        rej_label = f"{rej_entry[0]}:{rej_entry[1]}" if rej_entry else ""
        acc_label = f"{acc_entry[0]}:{acc_entry[1]}" if acc_entry else ""

        if schema_errors and effective_review == "accepted" and integrated and not is_protected:
            classification = LifecycleClass.QUARANTINED.value
            reasons = [
                f"unsupported legacy schema or database error ({'; '.join(schema_errors)}); fail-closed"
            ]
        elif effective_review == "rejected":
            if integrated:
                classification = LifecycleClass.QUARANTINED.value
                reasons = [f"unowned branch has rejected review record ({rej_label}); quarantined"]
            else:
                classification = LifecycleClass.UNIQUE_UNPUSHED.value
                reasons = [
                    "unowned branch with unmerged commits and rejected review record; audit never authorizes deletion"
                ]
        elif effective_review == "contradictory":
            if integrated:
                classification = LifecycleClass.QUARANTINED.value
                reasons = [
                    f"unowned branch has contradictory review records ({rej_label} vs {acc_label}); quarantined"
                ]
            else:
                classification = LifecycleClass.UNIQUE_UNPUSHED.value
                reasons = [
                    "unowned branch with contradictory review records; audit never authorizes deletion"
                ]
        elif effective_review == "accepted":
            if integrated and is_protected:
                classification = LifecycleClass.RETAINED_UNOWNED.value
                reasons = [
                    f"unowned branch with acceptance receipt ({acc_label}) is protected or in worktree; retained"
                ]
            elif integrated:
                classification = LifecycleClass.ACCEPTED_INTEGRATED.value
                reasons = [
                    f"unowned branch with valid acceptance receipt ({acc_label}); integrated"
                ]
            else:
                classification = LifecycleClass.UNIQUE_UNPUSHED.value
                reasons = [
                    "unowned branch with acceptance receipt but unmerged commits; audit never authorizes deletion"
                ]
        elif integrated:
            classification = LifecycleClass.RETAINED_UNOWNED.value
            reasons = ["unowned branch without acceptance receipt; retained"]
        else:
            classification = LifecycleClass.UNIQUE_UNPUSHED.value
            reasons = [
                "unowned branch with unique unmerged commits; audit never authorizes deletion"
            ]

        row: dict[str, Any] = {
            "kind": "branch",
            "route": route_name,
            "branch": branch,
            "sha": sha,
            "classification": classification,
            "reasons": reasons,
        }

        if classification == LifecycleClass.ACCEPTED_INTEGRATED.value and acc_entry is not None:
            frozen = FrozenBranch(
                route=route_name,
                branch=branch,
                sha=sha,
                canonical_ref=canonical_ref or "",
                canonical_tip=r_info.get("canonical_tip", ""),
                canonical_url=r_info.get("canonical_url", ""),
                common_git_dir=r_info.get("common_git_dir", ""),
                receipt_source=acc_entry[0],
                receipt_id=acc_entry[1],
            )
            row["operation_id"] = frozen.operation_id
            row["proof"] = {**frozen.__dict__}

        return row

    def _load_route_evidence(self, route_name: str, route_path: Path) -> dict[str, Any]:
        route = self.config.routes.get(route_name)
        target_route_path = route_path.resolve(strict=False)
        try:
            target_common_git_dir = str(
                (route_path / run_git(route_path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
        except GitError:
            target_common_git_dir = ""
        target_coordination_root = self.config.coordination_root.resolve(strict=False)
        target_slot_root = getattr(route, "slot_root", None) or getattr(
            self.config, "slot_root", None
        )
        if target_slot_root:
            target_slot_root = target_slot_root.resolve(strict=False)

        target_ckpt_refs: dict[str, str] = {}
        with suppress(GitError):
            for line in run_git(
                route_path,
                "for-each-ref",
                "--format=%(refname)%00%(objectname)",
                "refs/agent-control-plane/jobs",
            ).splitlines():
                if "\0" in line:
                    rname, rsha = line.split("\0", 1)
                    target_ckpt_refs[rname] = rsha

        def _is_workspace_verified(
            wp_val: str | Path | None,
            job_id: str | None = None,
            ckpt_ref: str | None = None,
            ckpt_sha: str | None = None,
        ) -> bool:
            wp_str = str(wp_val).strip() if wp_val is not None else ""
            if wp_str:
                wp = Path(wp_str).resolve(strict=False)
                if wp.exists():
                    if not target_common_git_dir:
                        return False
                    try:
                        cg = str(
                            (wp / run_git(wp, "rev-parse", "--git-common-dir")).resolve(
                                strict=False
                            )
                        )
                        if cg != target_common_git_dir:
                            return False
                        if job_id and ckpt_ref:
                            j_hash = hashlib.sha256(
                                job_id.encode("utf-8", errors="surrogatepass")
                            ).hexdigest()
                            valid_refs = {
                                f"refs/agent-control-plane/jobs/{j_hash}",
                                f"refs/agent-control-plane/jobs/{job_id}",
                            }
                            if ckpt_ref not in valid_refs:
                                return False
                        return not (
                            ckpt_ref
                            and ckpt_ref in target_ckpt_refs
                            and ckpt_sha
                            and target_ckpt_refs[ckpt_ref] != ckpt_sha
                        )
                    except (GitError, OSError):
                        return False

                # The workspace path is non-existent/removed.
                # Lexical containment alone can NEVER prove repository identity.
                is_lexical = (
                    is_same_or_child(wp, target_route_path)
                    or is_same_or_child(wp, target_coordination_root)
                    or bool(target_slot_root and is_same_or_child(wp, target_slot_root))
                )
                if not is_lexical:
                    return False

            # For non-existent historical workspaces or unprovided workspace paths:
            # An independently present exact checkpoint ref or hashed/exact job checkpoint ref
            # in this repository is strictly required (and matching SHA where supplied).
            candidate_refs: list[str] = []
            if job_id:
                j_hash = hashlib.sha256(job_id.encode("utf-8", errors="surrogatepass")).hexdigest()
                valid_job_refs = {
                    f"refs/agent-control-plane/jobs/{j_hash}",
                    f"refs/agent-control-plane/jobs/{job_id}",
                }
                if ckpt_ref:
                    if ckpt_ref not in valid_job_refs:
                        return False
                    candidate_refs.append(ckpt_ref)
                else:
                    candidate_refs.extend(sorted(valid_job_refs))
            elif ckpt_ref:
                candidate_refs.append(ckpt_ref)

            for ref in candidate_refs:
                if ref in target_ckpt_refs:
                    ref_sha = target_ckpt_refs[ref]
                    if ckpt_sha:
                        if ref_sha == ckpt_sha:
                            return True
                    else:
                        return True

            return False

        accepted_shas: dict[str, tuple[str, str, str]] = {}
        rejected_shas: dict[str, tuple[str, str, str]] = {}
        accepted_branches: dict[str, tuple[str, str, str]] = {}
        rejected_branches: dict[str, tuple[str, str, str]] = {}
        accepted_job_ids: set[str] = set()
        rejected_job_ids: set[str] = set()
        schema_errors: list[str] = []

        # Current review inbox items
        for item in self.inbox.list_items(review_status=None, limit=5000):
            if item.route != route_name:
                continue
            if not _is_workspace_verified(
                item.workspace_path,
                job_id=item.source_id,
                ckpt_ref=item.checkpoint_ref,
                ckpt_sha=item.checkpoint_sha,
            ):
                continue
            ts = item.reviewed_at or item.created_at or ""
            if item.review_status == "accepted":
                if item.source_id:
                    accepted_job_ids.add(item.source_id)
                if item.checkpoint_sha:
                    accepted_shas[item.checkpoint_sha] = ("inbox", item.item_id, ts)
            elif item.review_status == "rejected":
                if item.source_id:
                    rejected_job_ids.add(item.source_id)
                if item.checkpoint_sha:
                    rejected_shas[item.checkpoint_sha] = ("inbox", item.item_id, ts)

        # Current control database
        verified_curr_jobs: dict[str, tuple[str | None, str | None, str]] = {}
        with control_database(self.config.database_path) as db, suppress(sqlite3.Error):
            for row in db.execute(
                "select job_id, workspace_path, expected_branch, status, root_acceptance, updated_at from jobs where route = ?",
                (route_name,),
            ).fetchall():
                jid = row["job_id"]
                ws = row["workspace_path"]
                exp = row["expected_branch"]
                ra = row["root_acceptance"]
                ts = row["updated_at"] or ""
                if _is_workspace_verified(ws, job_id=jid):
                    verified_curr_jobs[jid] = (exp, ra, ts)

            for row in db.execute(
                "select task_id, job_id, review_status, accepted_sha, updated_at from plan_tasks where accepted_sha is not null"
            ).fetchall():
                jid = row["job_id"]
                asha = row["accepted_sha"]
                if not (
                    jid
                    and (
                        jid in verified_curr_jobs
                        or _is_workspace_verified(None, job_id=jid, ckpt_sha=asha)
                    )
                ):
                    continue
                ts = row["updated_at"] or ""
                if row["review_status"] == "accepted":
                    accepted_shas[row["accepted_sha"]] = ("plan_task", row["task_id"], ts)
                    accepted_job_ids.add(jid)
                elif row["review_status"] == "rejected":
                    rejected_shas[row["accepted_sha"]] = ("plan_task", row["task_id"], ts)
                    rejected_job_ids.add(jid)

            for row in db.execute(
                "select job_id, outcome, checkpoint_sha, accepted_sha, recorded_at from review_job_outcomes"
            ).fetchall():
                jid = row["job_id"]
                csha = row["checkpoint_sha"]
                asha = row["accepted_sha"]
                if not (
                    jid
                    and (
                        jid in verified_curr_jobs
                        or _is_workspace_verified(None, job_id=jid, ckpt_sha=csha or asha)
                    )
                ):
                    continue
                ts = row["recorded_at"] or ""
                if row["outcome"] == "accepted":
                    if row["accepted_sha"]:
                        accepted_shas[row["accepted_sha"]] = ("outcome", jid, ts)
                    if row["checkpoint_sha"]:
                        accepted_shas[row["checkpoint_sha"]] = ("outcome", jid, ts)
                    accepted_job_ids.add(jid)
                elif row["outcome"] == "rejected":
                    if row["checkpoint_sha"]:
                        rejected_shas[row["checkpoint_sha"]] = ("outcome", jid, ts)
                    rejected_job_ids.add(jid)

            for jid, (exp, ra, ts) in verified_curr_jobs.items():
                if not exp:
                    continue
                if ra == "accepted" or jid in accepted_job_ids:
                    accepted_branches[exp] = ("job", jid, ts)
                elif ra == "rejected" or jid in rejected_job_ids:
                    rejected_branches[exp] = ("job", jid, ts)

        # Legacy databases
        for leg_db in self.legacy_databases:
            if not leg_db.exists():
                continue
            try:
                conn = sqlite3.connect(f"file:{leg_db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
            except sqlite3.Error as exc:
                schema_errors.append(f"failed to open legacy database {leg_db.name}: {exc}")
                continue

            try:
                try:
                    cur = conn.execute("PRAGMA quick_check")
                    qc_row = cur.fetchone()
                    if not qc_row or qc_row[0] != "ok":
                        schema_errors.append(
                            f"legacy database {leg_db.name} quick_check failed: {qc_row[0] if qc_row else 'empty'}"
                        )
                        continue
                except sqlite3.Error as exc:
                    schema_errors.append(f"legacy database {leg_db.name} quick_check error: {exc}")
                    continue

                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

                leg_verified_jobs: dict[str, tuple[str | None, str | None, str]] = {}
                if "jobs" in tables:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
                    if not ("route" in cols and "job_id" in cols):
                        schema_errors.append(
                            f"legacy database {leg_db.name} has incomplete jobs table schema"
                        )
                    else:
                        try:
                            for row in conn.execute(
                                "SELECT * FROM jobs WHERE route = ?",
                                (route_name,),
                            ).fetchall():
                                col_names = set(row.keys())
                                jid = row["job_id"]
                                ws = (
                                    row["workspace_path"] if "workspace_path" in col_names else None
                                )
                                exp = (
                                    row["expected_branch"]
                                    if "expected_branch" in col_names
                                    else None
                                )
                                ra = (
                                    row["root_acceptance"]
                                    if "root_acceptance" in col_names
                                    else None
                                )
                                ts = (
                                    row["updated_at"]
                                    if "updated_at" in col_names and row["updated_at"]
                                    else ""
                                )
                                if _is_workspace_verified(ws, job_id=jid):
                                    leg_verified_jobs[jid] = (exp, ra, ts)
                        except sqlite3.Error as exc:
                            schema_errors.append(f"querying jobs in {leg_db.name} failed: {exc}")

                if "review_inbox_items" in tables:
                    cols = {
                        r[1]
                        for r in conn.execute("PRAGMA table_info(review_inbox_items)").fetchall()
                    }
                    if not (
                        "item_id" in cols
                        and "source_id" in cols
                        and "review_status" in cols
                        and "route" in cols
                    ):
                        schema_errors.append(
                            f"legacy database {leg_db.name} has incomplete review_inbox_items schema"
                        )
                    else:
                        try:
                            for row in conn.execute(
                                "SELECT * FROM review_inbox_items WHERE route = ?",
                                (route_name,),
                            ).fetchall():
                                col_names = set(row.keys())
                                jid = row["source_id"]
                                ws = (
                                    row["workspace_path"] if "workspace_path" in col_names else None
                                )
                                cref = (
                                    row["checkpoint_ref"] if "checkpoint_ref" in col_names else None
                                )
                                csha = (
                                    row["checkpoint_sha"] if "checkpoint_sha" in col_names else None
                                )
                                rev = row["reviewed_at"] if "reviewed_at" in col_names else None
                                cr = row["created_at"] if "created_at" in col_names else None
                                ts = rev or cr or ""
                                is_verified = False
                                if jid and jid in leg_verified_jobs:
                                    if (
                                        cref
                                        and cref in target_ckpt_refs
                                        and csha
                                        and target_ckpt_refs[cref] != csha
                                    ):
                                        is_verified = False
                                    else:
                                        is_verified = True
                                if not is_verified and _is_workspace_verified(
                                    ws, job_id=jid, ckpt_ref=cref, ckpt_sha=csha
                                ):
                                    is_verified = True
                                if not is_verified:
                                    continue
                                st = row["review_status"]
                                if st == "accepted":
                                    if jid:
                                        accepted_job_ids.add(jid)
                                    if csha:
                                        accepted_shas.setdefault(
                                            csha, ("legacy_inbox", row["item_id"], ts)
                                        )
                                elif st == "rejected":
                                    if jid:
                                        rejected_job_ids.add(jid)
                                    if csha:
                                        rejected_shas.setdefault(
                                            csha, ("legacy_inbox", row["item_id"], ts)
                                        )
                        except sqlite3.Error as exc:
                            schema_errors.append(
                                f"querying review_inbox_items in {leg_db.name} failed: {exc}"
                            )

                if "plan_tasks" in tables:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_tasks)").fetchall()}
                    if (
                        "task_id" in cols
                        and "job_id" in cols
                        and "review_status" in cols
                        and "accepted_sha" in cols
                    ):
                        try:
                            for row in conn.execute(
                                "SELECT * FROM plan_tasks WHERE accepted_sha IS NOT NULL"
                            ).fetchall():
                                col_names = set(row.keys())
                                jid = row["job_id"]
                                sha = row["accepted_sha"]
                                is_verified = bool(
                                    (jid and jid in leg_verified_jobs)
                                    or _is_workspace_verified(None, job_id=jid, ckpt_sha=sha)
                                )
                                if not is_verified:
                                    continue
                                ts = (
                                    row["updated_at"]
                                    if "updated_at" in col_names and row["updated_at"]
                                    else ""
                                )
                                st = row["review_status"]
                                if st == "accepted":
                                    accepted_shas.setdefault(
                                        sha, ("legacy_plan_task", row["task_id"], ts)
                                    )
                                    accepted_job_ids.add(jid)
                                elif st == "rejected":
                                    rejected_shas.setdefault(
                                        sha, ("legacy_plan_task", row["task_id"], ts)
                                    )
                                    rejected_job_ids.add(jid)
                        except sqlite3.Error as exc:
                            schema_errors.append(
                                f"querying plan_tasks in {leg_db.name} failed: {exc}"
                            )

                if "review_job_outcomes" in tables:
                    cols = {
                        r[1]
                        for r in conn.execute("PRAGMA table_info(review_job_outcomes)").fetchall()
                    }
                    if "job_id" in cols and "outcome" in cols:
                        try:
                            for row in conn.execute("SELECT * FROM review_job_outcomes").fetchall():
                                col_names = set(row.keys())
                                jid = row["job_id"]
                                csha = (
                                    row["checkpoint_sha"] if "checkpoint_sha" in col_names else None
                                )
                                asha = row["accepted_sha"] if "accepted_sha" in col_names else None
                                is_verified = bool(
                                    (jid and jid in leg_verified_jobs)
                                    or _is_workspace_verified(
                                        None, job_id=jid, ckpt_sha=csha or asha
                                    )
                                )
                                if not is_verified:
                                    continue
                                ts = (
                                    row["recorded_at"]
                                    if "recorded_at" in col_names and row["recorded_at"]
                                    else ""
                                )
                                csha = (
                                    row["checkpoint_sha"] if "checkpoint_sha" in col_names else None
                                )
                                asha = row["accepted_sha"] if "accepted_sha" in col_names else None
                                out = row["outcome"]
                                if out == "accepted":
                                    if asha:
                                        accepted_shas.setdefault(asha, ("legacy_outcome", jid, ts))
                                    if csha:
                                        accepted_shas.setdefault(csha, ("legacy_outcome", jid, ts))
                                    accepted_job_ids.add(jid)
                                elif out == "rejected":
                                    if csha:
                                        rejected_shas.setdefault(csha, ("legacy_outcome", jid, ts))
                                    rejected_job_ids.add(jid)
                        except sqlite3.Error as exc:
                            schema_errors.append(
                                f"querying review_job_outcomes in {leg_db.name} failed: {exc}"
                            )

                for jid, (exp, ra, ts) in leg_verified_jobs.items():
                    if not exp:
                        continue
                    if ra == "accepted" or jid in accepted_job_ids:
                        accepted_branches.setdefault(exp, ("legacy_job", jid, ts))
                    elif ra == "rejected" or jid in rejected_job_ids:
                        rejected_branches.setdefault(exp, ("legacy_job", jid, ts))

            finally:
                conn.close()

        # Checkpoint refs in git repo
        with suppress(GitError):
            for refname, sha in target_ckpt_refs.items():
                suffix = refname.removeprefix("refs/agent-control-plane/jobs/")
                for jid in accepted_job_ids:
                    j_hash = hashlib.sha256(jid.encode("utf-8", errors="surrogatepass")).hexdigest()
                    if suffix in (jid, j_hash):
                        accepted_shas.setdefault(sha, ("checkpoint_ref", refname, ""))
                for jid in rejected_job_ids:
                    j_hash = hashlib.sha256(jid.encode("utf-8", errors="surrogatepass")).hexdigest()
                    if suffix in (jid, j_hash):
                        rejected_shas.setdefault(sha, ("checkpoint_ref", refname, ""))

        return {
            "accepted_shas": accepted_shas,
            "rejected_shas": rejected_shas,
            "accepted_branches": accepted_branches,
            "rejected_branches": rejected_branches,
            "schema_errors": schema_errors,
        }

    def _has_acceptance_receipt(self, route_name: str, branch: str, sha: str) -> bool:
        route = self.config.routes.get(route_name)
        route_path = route.path if route else Path.cwd()
        evidence = self._load_route_evidence(route_name, route_path)
        if evidence["schema_errors"]:
            return False
        acc_entry = evidence["accepted_shas"].get(sha) or evidence["accepted_branches"].get(branch)
        rej_entry = evidence["rejected_shas"].get(sha) or evidence["rejected_branches"].get(branch)
        if not acc_entry:
            return False
        if rej_entry:
            acc_ts = acc_entry[2] or ""
            rej_ts = rej_entry[2] or ""
            if not (acc_ts and rej_ts and acc_ts > rej_ts):
                return False
        return True

    def apply(
        self,
        operation_id: str,
        *,
        crash_hook: Callable[[str], None] | None = None,
        pre_mutation_hook: Callable[[str], None] | None = None,
        executor_id: str | None = None,
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        intent = self._intent(operation_id)
        if intent is None:
            raise ValueError(f"Unknown lifecycle cleanup operation: {operation_id}")
        if intent["state"] == "completed":
            return {**intent, "action": "already_completed"}
        if intent["state"] == "quarantined":
            return {**intent, "action": "quarantined", "reason": intent.get("error")}

        actual_owner_pid = owner_pid if owner_pid is not None else os.getpid()
        actual_executor = (
            executor_id
            or f"{actual_owner_pid}_{self.clock()}_{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:8]}"
        )
        if not self.acquire_executor_lease(
            operation_id, actual_executor, owner_pid=actual_owner_pid
        ):
            raise RuntimeError(
                f"Operation {operation_id} is currently locked by another active executor"
            )
        fence_token = self.get_fence_token(operation_id, actual_executor)
        if fence_token is None:
            raise RuntimeError(f"Failed to acquire fence token for executor {actual_executor}")

        proof = intent["proof"]
        if proof.get("kind") == "branch":
            return self._apply_branch(
                operation_id,
                proof,
                executor_id=actual_executor,
                fence_token=fence_token,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
            )
        frozen = FrozenSlot(**{**proof, "path": Path(proof["path"])})

        try:
            # 1. Atomically acquire exclusive cleanup claim or re-validate existing claim
            slot = self.slots.require_slot(frozen.name)
            release_step = self._get_step(operation_id, "release_slot")
            if slot.status == "cleaning" and slot.active_job_id == operation_id:
                # Resuming an in-flight operation from crash
                if slot.generation != frozen.generation:
                    return self._quarantine(
                        operation_id,
                        f"slot generation moved from {frozen.generation} to {slot.generation}",
                        {"name": frozen.name, "generation": slot.generation},
                        frozen=frozen,
                    )
            elif release_step is not None and (
                slot.status in ("available", "deleted")
                and slot.generation == frozen.generation + 1
                and slot.active_job_id is None
            ):
                # Resuming after slot release mutation already occurred
                pass
            else:
                if slot.generation != frozen.generation:
                    return self._quarantine(
                        operation_id,
                        f"slot generation moved from {frozen.generation} to {slot.generation}",
                        {"name": frozen.name, "generation": slot.generation},
                        frozen=frozen,
                    )
                try:
                    slot = self.slots.claim_for_cleanup(
                        frozen.name,
                        operation_id,
                        frozen.generation,
                        expected_route=frozen.route,
                        expected_path=frozen.path,
                    )
                except SlotStoreError as exc:
                    return self._quarantine(
                        operation_id,
                        f"exclusive cleanup claim failed: {exc}",
                        {"name": frozen.name, "error": str(exc)},
                        frozen=frozen,
                    )

            # 2. Revalidate all pre-conditions within the exclusive claim
            # Global invariants must NEVER be bypassed on resume/recovery.
            route = self.config.routes.get(frozen.route)
            if route is None:
                raise ValueError(f"route {frozen.route} missing from config")

            if slot.route != frozen.route:
                raise ValueError(
                    f"slot registered route changed from {frozen.route} to {slot.route}"
                )
            if slot.path.resolve(strict=False) != frozen.path.resolve(strict=False):
                raise ValueError("slot registered path changed")

            if frozen.path.resolve(strict=False) == route.path.resolve(strict=False):
                raise ValueError("slot path is the canonical checkout of route")
            slot_root = self.config.slot_root_for(frozen.route)
            if not is_same_or_child(frozen.path, slot_root) or frozen.path.resolve(
                strict=False
            ) == slot_root.resolve(strict=False):
                raise ValueError("slot path is outside managed slot_root")

            if frozen.name in self.config.slots:
                configured = self.config.slots[frozen.name]
                if configured.route != frozen.route or configured.path.resolve(
                    strict=False
                ) != frozen.path.resolve(strict=False):
                    raise ValueError("configured slot route or path changed")

            remove_step = self._get_step(operation_id, "remove_worktree")
            worktree_removed = remove_step is not None and remove_step.get("status") == "completed"
            if not worktree_removed and frozen.path.exists():
                stat = frozen.path.stat()
                if (stat.st_dev, stat.st_ino) != (frozen.device, frozen.inode):
                    raise ValueError("slot filesystem identity changed")

            # Check remote URL
            current_remote_url = run_git(
                route.path, "remote", "get-url", route.canonical_remote or ""
            )
            if current_remote_url != frozen.canonical_url:
                raise ValueError("canonical remote identity changed")

            # Check repository identity
            current_common_git_dir = str(
                (route.path / run_git(route.path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
            if current_common_git_dir != frozen.common_git_dir:
                raise ValueError("repository identity changed")

            if not worktree_removed and frozen.path.exists():
                try:
                    wt_raw = run_git(route.path, "worktree", "list", "--porcelain")
                    current_worktrees = {
                        Path(line[9:].strip()).resolve(strict=False)
                        for line in wt_raw.splitlines()
                        if line.startswith("worktree ")
                    }
                except GitError:
                    current_worktrees = set()
                if frozen.path.resolve(strict=False) not in current_worktrees:
                    raise ValueError("slot path is not in route git worktree inventory")

            # Refetch exact named canonical ref inside exclusive fence before destructive mutation
            run_git(
                route.path,
                "fetch",
                "--no-tags",
                route.canonical_remote or "",
                f"refs/heads/{route.canonical_branch}:{frozen.canonical_ref}",
            )
            fetched_tip = run_git(route.path, "rev-parse", "--verify", frozen.canonical_ref)
            if fetched_tip != frozen.canonical_tip:
                raise ValueError("canonical remote ref moved")

            # Verify checkpoint sha is still canonical-reachable
            if not _is_ancestor(route.path, frozen.checkpoint_sha, fetched_tip):
                raise ValueError("checkpoint sha is no longer canonical-reachable")

            # Verify default ref has not moved or diverged (for configured slots)
            if frozen.name in self.config.slots:
                co_step = self._get_step(operation_id, "checkout_default")
                ff_step = self._get_step(operation_id, "fast_forward_default")
                current_default_sha = _run_git_or_none(
                    route.path, "rev-parse", "--verify", frozen.default_ref
                )
                expected_pre_default = (
                    frozen.canonical_tip if frozen.default_sha is None else frozen.default_sha
                )
                if ff_step is not None and ff_step.get("status") == "completed":
                    if current_default_sha != frozen.canonical_tip:
                        raise ValueError(
                            f"default branch ref {frozen.default_ref} moved after fast-forward"
                        )
                elif ff_step is not None and ff_step.get("status") == "started":
                    # Fast-forward crashed before recording step completion.
                    # Both the exact valid pre-state and exact valid post-state are recognized.
                    if current_default_sha not in (expected_pre_default, frozen.canonical_tip):
                        raise ValueError(
                            f"default branch ref {frozen.default_ref} moved or diverged during fast-forward"
                        )
                elif co_step is not None and (
                    co_step.get("status") == "completed"
                    or (
                        co_step.get("status") == "started"
                        and _run_git_or_none(frozen.path, "branch", "--show-current")
                        == frozen.default_branch
                    )
                ):
                    if current_default_sha != expected_pre_default:
                        raise ValueError(
                            f"default branch ref {frozen.default_ref} moved or disappeared after checkout_default"
                        )
                else:
                    if current_default_sha != frozen.default_sha:
                        raise ValueError(
                            f"default branch ref {frozen.default_ref} moved or disappeared"
                        )
                    if frozen.default_sha is not None and not _is_ancestor(
                        route.path, frozen.default_sha, fetched_tip
                    ):
                        raise ValueError("default branch is divergent from canonical tip")
                if current_default_sha is not None and not _is_ancestor(
                    route.path, current_default_sha, fetched_tip
                ):
                    raise ValueError("default branch is divergent from canonical tip")
            else:
                def_step = self._get_step(operation_id, "delete_default_branch")
                current_default_sha = _run_git_or_none(
                    route.path, "rev-parse", "--verify", frozen.default_ref
                )
                if frozen.default_sha is None:
                    if current_default_sha is not None:
                        raise ValueError(
                            f"default branch ref {frozen.default_ref} was absent at freeze time but was later created"
                        )
                else:
                    if def_step is not None and def_step.get("status") == "completed":
                        if current_default_sha is not None:
                            raise ValueError(
                                f"default branch ref {frozen.default_ref} reappeared after deletion"
                            )
                    elif (
                        def_step is not None
                        and def_step.get("status") == "started"
                        and current_default_sha is None
                    ):
                        # delete_default_branch mutation executed before crash
                        pass
                    else:
                        if current_default_sha != frozen.default_sha:
                            raise ValueError(
                                f"default branch ref {frozen.default_ref} moved or missing"
                            )
                        if not _is_ancestor(route.path, frozen.default_sha, fetched_tip):
                            raise ValueError("default branch is divergent from canonical tip")
                if current_default_sha is not None and not _is_ancestor(
                    route.path, current_default_sha, fetched_tip
                ):
                    raise ValueError("default branch is divergent from canonical tip")

            # Verify task branch ref if task branch is distinct and to be deleted
            need_task_branch = bool(frozen.branch and frozen.branch != frozen.default_branch)
            if need_task_branch:
                task_step = self._get_step(operation_id, "delete_task_branch")
                task_ref = f"refs/heads/{frozen.branch}"
                current_task_sha = _run_git_or_none(route.path, "rev-parse", "--verify", task_ref)
                if task_step is not None and task_step.get("status") == "completed":
                    if current_task_sha is not None:
                        raise ValueError(f"task branch ref {task_ref} reappeared after deletion")
                elif (
                    task_step is not None
                    and task_step.get("status") == "started"
                    and current_task_sha is None
                ):
                    # delete_task_branch mutation executed before crash
                    pass
                else:
                    if current_task_sha != frozen.head:
                        raise ValueError(f"task branch ref {task_ref} moved or missing")

            # Verify checkpoint ref if not yet deleted
            cp_step = self._get_step(operation_id, "delete_checkpoint")
            current_cp = _run_git_or_none(
                route.path, "rev-parse", "--verify", frozen.checkpoint_ref
            )
            if cp_step is not None and cp_step.get("status") == "completed":
                if current_cp is not None:
                    raise ValueError("checkpoint ref reappeared after deletion")
            elif cp_step is not None and cp_step.get("status") == "started" and current_cp is None:
                # delete_checkpoint mutation executed before crash
                pass
            else:
                if current_cp != frozen.checkpoint_sha:
                    raise ValueError("checkpoint ref moved or missing")

            # Verify exact accepted item receipt for slot generation
            accepted = self._accepted_item(slot, expected_generation=frozen.generation)
            if (
                accepted is None
                or accepted.checkpoint_sha != frozen.checkpoint_sha
                or accepted.source_id != frozen.job_id
                or (frozen.base_sha is not None and accepted.base_sha != frozen.base_sha)
                or (
                    accepted.workspace_path is not None
                    and accepted.workspace_path.resolve(strict=False)
                    != frozen.path.resolve(strict=False)
                )
            ):
                raise ValueError(
                    "exact accepted checkpoint receipt no longer matches slot generation"
                )

            # Validate durable job record if present
            try:
                job = self.jobs.get_job(frozen.job_id)
            except KeyError:
                job = None
            if job is not None and (
                job.slot_name != frozen.name
                or (job.slot_generation is not None and job.slot_generation != frozen.generation)
                or job.route != frozen.route
                or job.workspace_path.resolve(strict=False) != frozen.path.resolve(strict=False)
            ):
                raise ValueError("durable job identity mismatch with slot generation")

            # Verify checkpoint and checkout base HEAD are canonical-reachable
            cp_tree = (accepted.checkpoint_tree_sha if accepted else None) or _commit_tree(
                route.path, frozen.checkpoint_sha
            )
            if not (
                _is_ancestor(route.path, frozen.checkpoint_sha, fetched_tip)
                or (
                    cp_tree
                    and _has_canonical_tree(
                        route.path, cp_tree, fetched_tip, base_sha=frozen.base_sha
                    )
                )
            ):
                raise ValueError("accepted checkpoint is not canonical-reachable")

            head_tree = _commit_tree(route.path, frozen.head)
            if not (
                _is_ancestor(route.path, frozen.head, fetched_tip)
                or (
                    head_tree
                    and _has_canonical_tree(
                        route.path, head_tree, fetched_tip, base_sha=frozen.base_sha
                    )
                )
            ):
                raise ValueError("checkout base HEAD is not canonical-reachable")

            # Recheck liveness and full clean+ignored state if worktree exists
            if not worktree_removed and frozen.path.exists():
                pids = self.live_cwds(frozen.path)
                if any(pid < 0 for pid in pids) or pids:
                    raise ValueError(f"live processes detected in slot worktree: {pids}")

                snapshot = workspace_snapshot(frozen.path)
                if not snapshot.stable:
                    raise ValueError("workspace identity changed during verification")
                if snapshot.porcelain:
                    raise ValueError("tracked or untracked workspace changes present")

                ignored_raw = run_git(
                    frozen.path, "status", "--porcelain=v1", "-uall", "--ignored=matching"
                )
                if any(line.startswith("!! ") for line in ignored_raw.splitlines()):
                    raise ValueError("workspace contains unexpected ignored files")

                # Verify branch / HEAD matches current lifecycle step
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                curr_head = _run_git_or_none(frozen.path, "rev-parse", "HEAD")
                if frozen.name in self.config.slots:
                    co_step = self._get_step(operation_id, "checkout_default")
                    expected_pre_head = (
                        frozen.canonical_tip if frozen.default_sha is None else frozen.default_sha
                    )
                    if ff_step is not None and ff_step.get("status") == "completed":
                        if (
                            curr_branch != frozen.default_branch
                            or curr_head != frozen.canonical_tip
                        ):
                            raise ValueError("slot HEAD/branch drifted after fast-forward")
                    elif ff_step is not None and ff_step.get("status") == "started":
                        if curr_branch != frozen.default_branch:
                            raise ValueError("slot branch drifted during fast-forward")
                        if curr_head not in (expected_pre_head, frozen.canonical_tip):
                            raise ValueError("slot HEAD drifted during fast-forward")
                    elif co_step is not None and (
                        co_step.get("status") == "completed" or curr_branch == frozen.default_branch
                    ):
                        if curr_branch != frozen.default_branch:
                            raise ValueError("slot branch drifted after default checkout")
                    else:
                        if curr_branch != frozen.branch or curr_head != frozen.head:
                            raise ValueError("slot branch or HEAD drifted from audited snapshot")
                else:
                    if curr_branch != frozen.branch or curr_head != frozen.head:
                        raise ValueError("slot branch or HEAD drifted from audited snapshot")

            # 3. Execute external mutations through crash-recoverable step journal
            self._apply_steps_journal(
                frozen,
                operation_id,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=actual_executor,
                fence_token=fence_token,
            )

        except (GitError, OSError, ValueError, SlotStoreError) as exc:
            with suppress(Exception):
                self.slots.quarantine_cleanup(
                    frozen.name,
                    operation_id,
                    frozen.generation,
                    expected_route=frozen.route,
                    expected_path=frozen.path,
                    note=str(exc),
                )
            return self._quarantine(
                operation_id, str(exc), {"name": frozen.name, "error": str(exc)}, frozen=frozen
            )
        finally:
            with suppress(Exception):
                self.release_executor_lease(operation_id, actual_executor, fence_token=fence_token)

        self._set_intent_state(operation_id, "completed", None)
        result = {"operation_id": operation_id, "action": "completed", "slot": frozen.name}
        self._event("cleanup_completed", operation_id, result)
        return result

    def _apply_steps_journal(
        self,
        frozen: FrozenSlot,
        operation_id: str,
        *,
        crash_hook: Callable[[str], None] | None = None,
        pre_mutation_hook: Callable[[str], None] | None = None,
        executor_id: str,
        fence_token: int,
    ) -> None:
        route = self.config.routes[frozen.route]
        step_order = 0
        is_temporary = frozen.name not in self.config.slots

        if is_temporary:
            # Temporary dynamic worktree lifecycle:
            # Step 1: delete_checkpoint
            step_order += 1

            def _pre_del_cp() -> bool:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                return curr == frozen.checkpoint_sha

            def _post_del_cp() -> bool:
                return (
                    _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                    is None
                )

            def _mutate_del_cp() -> None:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                if curr is not None:
                    run_git(
                        route.path, "update-ref", "-d", frozen.checkpoint_ref, frozen.checkpoint_sha
                    )

            self._execute_journal_step(
                operation_id,
                "delete_checkpoint",
                step_order,
                is_pre_state=_pre_del_cp,
                is_post_state=_post_del_cp,
                mutation=_mutate_del_cp,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 2: remove_worktree
            step_order += 1

            def _pre_remove_wt() -> bool:
                if not frozen.path.exists():
                    return True
                pids = self.live_cwds(frozen.path)
                if any(pid < 0 for pid in pids) or pids:
                    raise ValueError(f"live process CWDs detected before worktree removal: {pids}")
                snapshot = workspace_snapshot(frozen.path)
                if not snapshot.stable or snapshot.porcelain:
                    raise ValueError("workspace dirty or unstable before worktree removal")
                ignored_raw = run_git(
                    frozen.path, "status", "--porcelain=v1", "-uall", "--ignored=matching"
                )
                if any(line.startswith("!! ") for line in ignored_raw.splitlines()):
                    raise ValueError("unexpected ignored files present before worktree removal")
                return True

            def _post_remove_wt() -> bool:
                return not frozen.path.exists()

            def _mutate_remove_wt() -> None:
                if frozen.path.exists():
                    run_git(route.path, "worktree", "remove", str(frozen.path))

            self._execute_journal_step(
                operation_id,
                "remove_worktree",
                step_order,
                is_pre_state=_pre_remove_wt,
                is_post_state=_post_remove_wt,
                mutation=_mutate_remove_wt,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 3: delete_task_branch
            need_task_branch = bool(frozen.branch and frozen.branch != frozen.default_branch)
            if need_task_branch:
                step_order += 1
                task_ref = f"refs/heads/{frozen.branch}"

                def _pre_del_task_branch() -> bool:
                    curr = _run_git_or_none(route.path, "rev-parse", "--verify", task_ref)
                    return curr == frozen.head

                def _post_del_task_branch() -> bool:
                    return _run_git_or_none(route.path, "rev-parse", "--verify", task_ref) is None

                def _mutate_del_task_branch() -> None:
                    run_git(route.path, "update-ref", "-d", task_ref, frozen.head)

                self._execute_journal_step(
                    operation_id,
                    "delete_task_branch",
                    step_order,
                    is_pre_state=_pre_del_task_branch,
                    is_post_state=_post_del_task_branch,
                    mutation=_mutate_del_task_branch,
                    crash_hook=crash_hook,
                    pre_mutation_hook=pre_mutation_hook,
                    executor_id=executor_id,
                    fence_token=fence_token,
                )

            # Step 4: delete_default_branch
            step_order += 1
            def_ref = f"refs/heads/{frozen.default_branch}"

            def _pre_del_def_branch() -> bool:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", def_ref)
                if frozen.default_sha is None:
                    return curr is None
                return curr == frozen.default_sha

            def _post_del_def_branch() -> bool:
                return _run_git_or_none(route.path, "rev-parse", "--verify", def_ref) is None

            def _mutate_del_def_branch() -> None:
                if frozen.default_sha is None:
                    curr = _run_git_or_none(route.path, "rev-parse", "--verify", def_ref)
                    if curr is not None:
                        raise ValueError(
                            f"default branch ref {def_ref} was absent at freeze time but was later created"
                        )
                    return
                run_git(
                    route.path,
                    "update-ref",
                    "-d",
                    def_ref,
                    frozen.default_sha,
                )

            self._execute_journal_step(
                operation_id,
                "delete_default_branch",
                step_order,
                is_pre_state=_pre_del_def_branch,
                is_post_state=_post_del_def_branch,
                mutation=_mutate_del_def_branch,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 5: release_slot
            step_order += 1

            def _pre_release_slot_temp() -> bool:
                current = self.slots.get_slot(frozen.name)
                return current is not None and (current.status in ("cleaning", "deleted"))

            def _post_release_slot_temp() -> bool:
                current = self.slots.get_slot(frozen.name)
                return (
                    current is not None
                    and current.status == "deleted"
                    and current.active_job_id is None
                )

            def _mutate_release_slot_temp() -> None:
                current = self.slots.get_slot(frozen.name)
                if current is not None and current.status == "cleaning":
                    self.slots.release_cleanup(
                        frozen.name,
                        operation_id,
                        frozen.generation,
                        expected_route=frozen.route,
                        expected_path=frozen.path,
                        status="deleted",
                        note="accepted temporary worktree removed",
                    )

            self._execute_journal_step(
                operation_id,
                "release_slot",
                step_order,
                is_pre_state=_pre_release_slot_temp,
                is_post_state=_post_release_slot_temp,
                mutation=_mutate_release_slot_temp,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 6: prune
            step_order += 1

            def _pre_prune_temp() -> bool:
                return True

            def _post_prune_temp() -> bool:
                return True

            def _mutate_prune_temp() -> None:
                run_git(route.path, "worktree", "prune")

            self._execute_journal_step(
                operation_id,
                "prune",
                step_order,
                is_pre_state=_pre_prune_temp,
                is_post_state=_post_prune_temp,
                mutation=_mutate_prune_temp,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

        else:
            # Configured slot lifecycle:
            need_switch = bool(frozen.branch and frozen.branch != frozen.default_branch)

            # Step 1: checkout_default
            step_order += 1

            def _pre_checkout() -> bool:
                if not frozen.path.exists():
                    raise ValueError("slot path missing before checkout")
                pids = self.live_cwds(frozen.path)
                if any(pid < 0 for pid in pids) or pids:
                    raise ValueError(f"live process CWDs detected before checkout: {pids}")
                snapshot = workspace_snapshot(frozen.path)
                if not snapshot.stable or snapshot.porcelain:
                    raise ValueError("workspace dirty or unstable before checkout")
                ignored_raw = run_git(
                    frozen.path, "status", "--porcelain=v1", "-uall", "--ignored=matching"
                )
                if any(line.startswith("!! ") for line in ignored_raw.splitlines()):
                    raise ValueError("unexpected ignored files present before checkout")
                if not need_switch:
                    return True
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                curr_head = _run_git_or_none(frozen.path, "rev-parse", "HEAD")
                return (curr_branch == frozen.branch and curr_head == frozen.head) or (
                    curr_branch == frozen.default_branch
                )

            def _post_checkout() -> bool:
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                return curr_branch == frozen.default_branch

            def _mutate_checkout() -> None:
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                if curr_branch == frozen.default_branch:
                    return
                exists = (
                    _run_git_or_none(route.path, "rev-parse", "--verify", frozen.default_ref)
                    is not None
                )
                if not exists:
                    run_git(
                        frozen.path, "checkout", "-b", frozen.default_branch, frozen.canonical_tip
                    )
                else:
                    run_git(frozen.path, "checkout", frozen.default_branch)

            self._execute_journal_step(
                operation_id,
                "checkout_default",
                step_order,
                is_pre_state=_pre_checkout,
                is_post_state=_post_checkout,
                mutation=_mutate_checkout,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 2: fast_forward_default
            step_order += 1
            expected_pre_head = (
                frozen.canonical_tip if frozen.default_sha is None else frozen.default_sha
            )

            def _pre_ff() -> bool:
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                curr_head = _run_git_or_none(frozen.path, "rev-parse", "HEAD")
                return curr_branch == frozen.default_branch and curr_head in (
                    expected_pre_head,
                    frozen.canonical_tip,
                )

            def _post_ff() -> bool:
                curr_branch = _run_git_or_none(frozen.path, "branch", "--show-current")
                curr_head = _run_git_or_none(frozen.path, "rev-parse", "HEAD")
                return curr_branch == frozen.default_branch and curr_head == frozen.canonical_tip

            def _mutate_ff() -> None:
                curr_head = _run_git_or_none(frozen.path, "rev-parse", "HEAD")
                if curr_head != frozen.canonical_tip:
                    run_git(frozen.path, "merge", "--ff-only", frozen.canonical_tip)

            self._execute_journal_step(
                operation_id,
                "fast_forward_default",
                step_order,
                is_pre_state=_pre_ff,
                is_post_state=_post_ff,
                mutation=_mutate_ff,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 3: delete_task_branch (if task branch was distinct from default branch)
            if need_switch:
                step_order += 1
                task_ref = f"refs/heads/{frozen.branch}"

                def _pre_del_branch() -> bool:
                    curr = _run_git_or_none(route.path, "rev-parse", "--verify", task_ref)
                    return curr == frozen.head

                def _post_del_branch() -> bool:
                    return _run_git_or_none(route.path, "rev-parse", "--verify", task_ref) is None

                def _mutate_del_branch() -> None:
                    run_git(route.path, "update-ref", "-d", task_ref, frozen.head)

                self._execute_journal_step(
                    operation_id,
                    "delete_task_branch",
                    step_order,
                    is_pre_state=_pre_del_branch,
                    is_post_state=_post_del_branch,
                    mutation=_mutate_del_branch,
                    crash_hook=crash_hook,
                    pre_mutation_hook=pre_mutation_hook,
                    executor_id=executor_id,
                    fence_token=fence_token,
                )

            # Step 4: delete_checkpoint
            step_order += 1

            def _pre_del_checkpoint() -> bool:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                return curr == frozen.checkpoint_sha

            def _post_del_checkpoint() -> bool:
                return (
                    _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                    is None
                )

            def _mutate_del_checkpoint() -> None:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", frozen.checkpoint_ref)
                if curr is not None:
                    run_git(
                        route.path, "update-ref", "-d", frozen.checkpoint_ref, frozen.checkpoint_sha
                    )

            self._execute_journal_step(
                operation_id,
                "delete_checkpoint",
                step_order,
                is_pre_state=_pre_del_checkpoint,
                is_post_state=_post_del_checkpoint,
                mutation=_mutate_del_checkpoint,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 5: release_slot
            step_order += 1

            def _pre_release_slot() -> bool:
                if frozen.path.exists():
                    pids = self.live_cwds(frozen.path)
                    if any(pid < 0 for pid in pids) or pids:
                        raise ValueError(f"live process CWDs detected before slot release: {pids}")
                    snapshot = workspace_snapshot(frozen.path)
                    if not snapshot.stable or snapshot.porcelain:
                        raise ValueError("workspace dirty or unstable before slot release")
                    ignored_raw = run_git(
                        frozen.path, "status", "--porcelain=v1", "-uall", "--ignored=matching"
                    )
                    if any(line.startswith("!! ") for line in ignored_raw.splitlines()):
                        raise ValueError("unexpected ignored files present before slot release")
                current = self.slots.get_slot(frozen.name)
                return current is not None and (current.status in ("cleaning", "available"))

            def _post_release_slot() -> bool:
                current = self.slots.get_slot(frozen.name)
                return (
                    current is not None
                    and current.status == "available"
                    and current.active_job_id is None
                )

            def _mutate_release_slot() -> None:
                current = self.slots.get_slot(frozen.name)
                if current is not None and current.status == "cleaning":
                    self.slots.release_cleanup(
                        frozen.name,
                        operation_id,
                        frozen.generation,
                        expected_route=frozen.route,
                        expected_path=frozen.path,
                        status="available",
                        note="accepted lifecycle cleanup completed",
                    )

            self._execute_journal_step(
                operation_id,
                "release_slot",
                step_order,
                is_pre_state=_pre_release_slot,
                is_post_state=_post_release_slot,
                mutation=_mutate_release_slot,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

            # Step 6: prune
            step_order += 1

            def _pre_prune() -> bool:
                return True

            def _post_prune() -> bool:
                return True

            def _mutate_prune() -> None:
                run_git(route.path, "worktree", "prune")

            self._execute_journal_step(
                operation_id,
                "prune",
                step_order,
                is_pre_state=_pre_prune,
                is_post_state=_post_prune,
                mutation=_mutate_prune,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

    def _execute_journal_step(
        self,
        operation_id: str,
        step_name: str,
        step_order: int,
        *,
        is_pre_state: Callable[[], bool],
        is_post_state: Callable[[], bool],
        mutation: Callable[[], None],
        crash_hook: Callable[[str], None] | None = None,
        pre_mutation_hook: Callable[[str], None] | None = None,
        executor_id: str,
        fence_token: int,
    ) -> None:
        step = self._get_step(operation_id, step_name)
        if step and step["status"] == "completed":
            return
        if step and step["status"] == "started":
            # Restart after crash
            if is_post_state():
                self._set_step_status(operation_id, step_name, "completed", None)
                if crash_hook:
                    crash_hook(step_name)
                return
            if is_pre_state():
                self.verify_fence(operation_id, executor_id, fence_token)
                self.heartbeat_executor_lease(operation_id, executor_id, fence_token)
                if pre_mutation_hook:
                    pre_mutation_hook(step_name)
                mutation()
                if not is_post_state():
                    raise ValueError(f"step {step_name} post-state verification failed after retry")
                if crash_hook:
                    crash_hook(step_name)
                self._set_step_status(operation_id, step_name, "completed", None)
                return
            raise ValueError(
                f"step {step_name} crash recovery failed: neither expected pre-state nor post-state found"
            )

        # First attempt for this step
        if not is_pre_state():
            raise ValueError(f"step {step_name} pre-state verification failed")

        self._record_step(operation_id, step_name, step_order, "started")
        self.verify_fence(operation_id, executor_id, fence_token)
        self.heartbeat_executor_lease(operation_id, executor_id, fence_token)
        if pre_mutation_hook:
            pre_mutation_hook(step_name)
        mutation()
        if not is_post_state():
            raise ValueError(f"step {step_name} post-state verification failed after mutation")
        if crash_hook:
            crash_hook(step_name)
        self._set_step_status(operation_id, step_name, "completed", None)

    def _apply_branch(
        self,
        operation_id: str,
        proof: dict[str, Any],
        *,
        executor_id: str,
        fence_token: int,
        crash_hook: Callable[[str], None] | None = None,
        pre_mutation_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        frozen = FrozenBranch(**proof)
        route = self.config.routes.get(frozen.route)
        if route is None:
            return self._quarantine(
                operation_id,
                f"route {frozen.route} missing from config",
                {"branch": frozen.branch},
                frozen=frozen,
            )

        # 1. Revalidate repository identity and remote URL
        try:
            current_remote_url = run_git(
                route.path, "remote", "get-url", route.canonical_remote or ""
            )
            current_common_git_dir = str(
                (route.path / run_git(route.path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
        except GitError as exc:
            return self._quarantine(
                operation_id,
                f"git repository inspection failed: {exc}",
                {"branch": frozen.branch},
                frozen=frozen,
            )

        if current_remote_url != frozen.canonical_url:
            return self._quarantine(
                operation_id,
                "canonical remote identity changed",
                {"expected": frozen.canonical_url, "current": current_remote_url},
                frozen=frozen,
            )
        if current_common_git_dir != frozen.common_git_dir:
            return self._quarantine(
                operation_id,
                "repository identity changed",
                {"expected": frozen.common_git_dir, "current": current_common_git_dir},
                frozen=frozen,
            )

        # 2. Refetch exact canonical ref inside exclusive lease
        try:
            run_git(
                route.path,
                "fetch",
                "--no-tags",
                route.canonical_remote or "",
                f"refs/heads/{route.canonical_branch}:{frozen.canonical_ref}",
            )
            fetched_tip = run_git(route.path, "rev-parse", "--verify", frozen.canonical_ref)
        except GitError as exc:
            return self._quarantine(
                operation_id,
                f"canonical refetch failed: {exc}",
                {"branch": frozen.branch},
                frozen=frozen,
            )

        if fetched_tip != frozen.canonical_tip:
            return self._quarantine(
                operation_id,
                "canonical remote ref moved",
                {"expected": frozen.canonical_tip, "current": fetched_tip},
                frozen=frozen,
            )

        # 3. Verify branch is not protected
        protected = {"main", "master", "dev", route.required_branch, route.canonical_branch}
        if frozen.branch in protected:
            return self._quarantine(
                operation_id,
                f"cannot delete protected branch {frozen.branch}",
                {"branch": frozen.branch},
                frozen=frozen,
            )
        if frozen.branch.startswith("slot/"):
            return self._quarantine(
                operation_id,
                f"cannot delete slot default branch {frozen.branch}",
                {"branch": frozen.branch},
                frozen=frozen,
            )

        # 4. Check worktree checkout
        try:
            wt_raw = run_git(route.path, "worktree", "list", "--porcelain")
            worktrees = _parse_worktrees(wt_raw)
        except GitError as exc:
            return self._quarantine(
                operation_id,
                f"failed to list worktrees: {exc}",
                {"branch": frozen.branch},
                frozen=frozen,
            )
        for wt in worktrees:
            if wt.get("branch") == frozen.branch:
                return self._quarantine(
                    operation_id,
                    f"branch {frozen.branch} is checked out in worktree {wt.get('path')}",
                    {"worktree": wt.get("path")},
                    frozen=frozen,
                )

        # 5. Check ref and compare-and-delete
        branch_ref = f"refs/heads/{frozen.branch}"
        del_step = self._get_step(operation_id, "delete_branch")
        current_sha = _run_git_or_none(route.path, "rev-parse", "--verify", branch_ref)

        if del_step is not None and del_step.get("status") == "completed":
            if current_sha is not None:
                return self._quarantine(
                    operation_id,
                    f"branch ref {branch_ref} reappeared after deletion",
                    {"branch": frozen.branch, "sha": current_sha},
                    frozen=frozen,
                )
        elif del_step is not None and del_step.get("status") == "started" and current_sha is None:
            self._set_step_status(operation_id, "delete_branch", "completed", None)
        else:
            if current_sha is None:
                return self._quarantine(
                    operation_id,
                    f"branch ref {branch_ref} missing",
                    {"branch": frozen.branch},
                    frozen=frozen,
                )
            if current_sha != frozen.sha:
                return self._quarantine(
                    operation_id,
                    f"branch ref {branch_ref} moved from {frozen.sha} to {current_sha}",
                    {"expected": frozen.sha, "current": current_sha},
                    frozen=frozen,
                )

            is_anc = _is_ancestor(route.path, current_sha, fetched_tip)
            is_pe = False if is_anc else _is_patch_equivalent(route.path, current_sha, fetched_tip)
            branch_tree = _commit_tree(route.path, current_sha)
            is_te = (
                False
                if (is_anc or is_pe or not branch_tree)
                else _has_canonical_tree(route.path, branch_tree, fetched_tip)
            )
            if not (is_anc or is_pe or is_te):
                return self._quarantine(
                    operation_id,
                    "branch is no longer integrated into canonical tip",
                    {"branch": frozen.branch, "sha": current_sha},
                    frozen=frozen,
                )

            def _pre_del_branch() -> bool:
                curr = _run_git_or_none(route.path, "rev-parse", "--verify", branch_ref)
                return curr == frozen.sha

            def _post_del_branch() -> bool:
                return _run_git_or_none(route.path, "rev-parse", "--verify", branch_ref) is None

            def _mutate_del_branch() -> None:
                run_git(route.path, "update-ref", "-d", branch_ref, frozen.sha)

            self._execute_journal_step(
                operation_id,
                "delete_branch",
                1,
                is_pre_state=_pre_del_branch,
                is_post_state=_post_del_branch,
                mutation=_mutate_del_branch,
                crash_hook=crash_hook,
                pre_mutation_hook=pre_mutation_hook,
                executor_id=executor_id,
                fence_token=fence_token,
            )

        self._set_intent_state(operation_id, "completed", None)
        self._event("cleanup_applied", operation_id, proof)
        return {
            "operation_id": operation_id,
            "state": "completed",
            "proof": proof,
            "error": None,
            "action": "completed",
        }

    def reconcile(
        self,
        *,
        refresh: bool = True,
        enqueue: bool = True,
        auto_return: bool = False,
    ) -> dict[str, Any]:
        self._initialize()
        auto_returned: list[dict[str, Any]] = []
        if auto_return:
            auto_returned = self.auto_return_slots(refresh=refresh)

        pending_by_route: dict[str, list[int]] = {}
        if refresh:
            for req in self.pending_reconciliation_requests():
                pending_by_route.setdefault(req["route"], []).append(req["id"])

        audit = self.audit(refresh=refresh)
        enqueued: list[str] = []
        if enqueue:
            for resource in audit["resources"]:
                if (
                    resource["classification"]
                    not in (
                        LifecycleClass.SAFE_TO_DELETE.value,
                        LifecycleClass.ACCEPTED_INTEGRATED.value,
                    )
                    or not resource.get("operation_id")
                    or not resource.get("proof")
                ):
                    continue
                operation_id = str(resource["operation_id"])
                self._record_intent(operation_id, resource["proof"])
                enqueued.append(operation_id)

        acknowledged_requests: list[int] = []
        if refresh:
            for route_name, req_ids in pending_by_route.items():
                route_failed = any(
                    res.get("route") == route_name
                    and res.get("reasons")
                    and any(
                        "canonical refresh failed" in r
                        or "inventory failed" in r
                        or "not explicitly configured" in r
                        for r in res["reasons"]
                    )
                    for res in audit["resources"]
                )
                if not route_failed:
                    self.acknowledge_reconciliation_requests(req_ids)
                    acknowledged_requests.extend(req_ids)

        result = {**audit, "enqueued": enqueued, "acknowledged_requests": acknowledged_requests}
        if auto_return:
            result["auto_returned"] = auto_returned
        return result

    def auto_return_slot(
        self,
        slot_name: str,
        *,
        refresh: bool = True,
    ) -> dict[str, Any]:
        self._initialize()
        slot = self.slots.get_slot(slot_name)
        if slot is None:
            return {"slot": slot_name, "status": "failed", "reason": f"slot {slot_name} not found"}
        if slot.name not in self.config.slots:
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": f"slot {slot_name} is not a configured reusable slot",
            }
        route = self.config.routes.get(slot.route)
        if route is None:
            return {"slot": slot_name, "status": "failed", "reason": f"unknown route {slot.route}"}
        if not route.canonical_remote or not route.canonical_branch:
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": f"canonical remote/branch not configured for route {slot.route}",
            }
        if slot.active_job_id is not None or slot.status != "available":
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": f"slot {slot_name} is not available (status={slot.status}, active_job_id={slot.active_job_id})",
            }
        if not slot.path.exists():
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"slot path {slot.path} does not exist",
            }

        slot_root = self.config.slot_root_for(slot.route)
        if not is_same_or_child(slot.path, slot_root) or slot.path.resolve(
            strict=False
        ) == slot_root.resolve(strict=False):
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "slot path is outside managed slot_root",
            }

        try:
            slot_common = str(
                (slot.path / run_git(slot.path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
            route_common = str(
                (route.path / run_git(route.path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
        except GitError as exc:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"failed to resolve git-common-dir: {exc}",
            }
        if slot_common != route_common:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "slot git-common-dir differs from route common git dir",
            }

        try:
            snapshot = workspace_snapshot(slot.path)
        except (GitError, OSError) as exc:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"failed to snapshot workspace: {exc}",
            }

        if not snapshot.stable or snapshot.porcelain:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "workspace dirty or unstable; refusing to return slot",
            }

        # Check ignored files and preparation artifacts
        ok_ignored, err_ignored, _ = _inspect_ignored_entries(slot.path, slot.route, self.config)
        if not ok_ignored:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": err_ignored or "unexpected ignored files present",
            }

        # Check live process CWDs
        try:
            pids = self.live_cwds(slot.path)
        except (PermissionError, OSError) as exc:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"process CWD inspection failed: {exc}",
            }
        if any(pid < 0 for pid in pids) or pids:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"live process CWDs detected: {pids}",
            }

        # Fetch canonical remote tip
        canonical_ref = f"refs/remotes/{route.canonical_remote}/{route.canonical_branch}"
        if refresh:
            try:
                run_git(
                    route.path,
                    "fetch",
                    "--no-tags",
                    route.canonical_remote,
                    f"refs/heads/{route.canonical_branch}:{canonical_ref}",
                )
            except GitError as exc:
                return {
                    "slot": slot_name,
                    "status": "failed",
                    "reason": f"canonical fetch failed: {exc}",
                }

        try:
            canonical_tip = run_git(route.path, "rev-parse", "--verify", canonical_ref)
        except GitError as exc:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"canonical tip resolution failed: {exc}",
            }

        default_branch = f"slot/{slot.name}"
        default_ref = f"refs/heads/{default_branch}"
        default_sha = _run_git_or_none(route.path, "rev-parse", "--verify", default_ref)

        if default_sha is not None:
            def_tree = _commit_tree(route.path, default_sha)
            if not (
                _is_ancestor(route.path, default_sha, canonical_tip)
                or (def_tree and _has_canonical_tree(route.path, def_tree, canonical_tip))
            ):
                return {
                    "slot": slot_name,
                    "status": "failed",
                    "reason": f"default branch {default_branch} has divergent or unmerged commits",
                }

        # If already on default branch
        if snapshot.branch == default_branch:
            if snapshot.head == canonical_tip:
                return {
                    "slot": slot_name,
                    "status": "unchanged",
                    "branch": default_branch,
                    "head": canonical_tip,
                    "generation": slot.generation,
                }
            if not snapshot.head:
                return {
                    "slot": slot_name,
                    "status": "failed",
                    "reason": "slot has no HEAD commit",
                }
            head_tree = _commit_tree(slot.path, snapshot.head)
            if not (
                _is_ancestor(slot.path, snapshot.head, canonical_tip)
                or (head_tree and _has_canonical_tree(slot.path, head_tree, canonical_tip))
            ):
                return {
                    "slot": slot_name,
                    "status": "failed",
                    "reason": f"default branch head {snapshot.head} is not canonical-reachable",
                }
            try:
                run_git(slot.path, "merge", "--ff-only", canonical_tip)
            except GitError as exc:
                return {
                    "slot": slot_name,
                    "status": "failed",
                    "reason": f"fast-forward failed: {exc}",
                }
            return {
                "slot": slot_name,
                "status": "fast_forwarded",
                "branch": default_branch,
                "head": canonical_tip,
                "generation": slot.generation,
            }

        # Slot is on a non-default branch.
        accepted = self._accepted_item(slot)
        if accepted is None or accepted.checkpoint_sha is None or accepted.checkpoint_ref is None:
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": "no exact accepted checkpoint receipt for slot generation",
            }
        if accepted.review_status != "accepted":
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": f"review status is {accepted.review_status}, not accepted",
            }

        current_cp_sha = _run_git_or_none(
            slot.path, "show-ref", "--verify", "--hash", accepted.checkpoint_ref
        )
        if current_cp_sha != accepted.checkpoint_sha:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "checkpoint ref moved or missing",
            }

        checkpoint_tree = accepted.checkpoint_tree_sha or _commit_tree(
            slot.path, accepted.checkpoint_sha
        )
        checkpoint_integrated = _is_ancestor(slot.path, accepted.checkpoint_sha, canonical_tip) or (
            checkpoint_tree is not None
            and _has_canonical_tree(
                slot.path, checkpoint_tree, canonical_tip, base_sha=accepted.base_sha
            )
        )
        if not checkpoint_integrated:
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": "accepted checkpoint is not canonical-integrated",
            }

        if not snapshot.head:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "slot has no HEAD commit",
            }

        head_tree = _commit_tree(slot.path, snapshot.head)
        valid_heads = {accepted.checkpoint_sha}
        if accepted.base_sha is not None:
            valid_heads.add(accepted.base_sha)
        if snapshot.head not in valid_heads and head_tree != checkpoint_tree:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": "slot HEAD differs from accepted checkpoint and base",
            }

        head_integrated = _is_ancestor(slot.path, snapshot.head, canonical_tip) or (
            head_tree is not None
            and _has_canonical_tree(slot.path, head_tree, canonical_tip, base_sha=accepted.base_sha)
        )
        if not head_integrated:
            return {
                "slot": slot_name,
                "status": "skipped",
                "reason": "slot HEAD is not canonical-integrated",
            }

        # Atomically claim slot for return
        op_id = f"auto-return:{slot.name}:{slot.generation}:{int(self.clock())}"
        try:
            self.slots.claim_for_cleanup(
                slot.name,
                op_id,
                expected_generation=slot.generation,
                expected_route=slot.route,
                expected_path=slot.path,
            )
        except SlotStoreError as exc:
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"failed to claim slot: {exc}",
            }

        try:
            post_claim_snap = workspace_snapshot(slot.path)
            if not post_claim_snap.stable or post_claim_snap.porcelain:
                raise ValueError("workspace dirty or unstable after claim")

            post_pids = self.live_cwds(slot.path)
            if any(pid < 0 for pid in post_pids) or post_pids:
                raise ValueError(f"live process CWDs detected after claim: {post_pids}")

            # Re-snapshot ignored state immediately after acquiring the cleanup claim
            ok_post, reason_post, caches_to_remove = _inspect_ignored_entries(
                slot.path, slot.route, self.config
            )
            if not ok_post:
                raise ValueError(f"ignored workspace state invalid after claim: {reason_post}")

            # Remove known disposable controller-generated caches safely
            for cache_path in sorted(caches_to_remove, key=lambda p: len(p.parts), reverse=True):
                if not os.path.lexists(cache_path):
                    continue
                if not is_same_or_child(cache_path, slot.path) or cache_path.resolve(
                    strict=False
                ) == slot.path.resolve(strict=False):
                    raise ValueError(f"disposable cache path outside slot: {cache_path}")
                if cache_path.is_symlink():
                    raise ValueError(f"refusing to remove symlink as cache: {cache_path}")
                if cache_path.is_dir():
                    shutil.rmtree(cache_path)
                elif cache_path.is_file():
                    cache_path.unlink()

            # Re-verify ignored state after cache disposal
            ok_after_clean, reason_after_clean, remaining_caches = _inspect_ignored_entries(
                slot.path, slot.route, self.config
            )
            if not ok_after_clean:
                raise ValueError(
                    f"workspace contains invalid ignored entries after cache removal: {reason_after_clean}"
                )
            if remaining_caches:
                raise ValueError(
                    f"disposable caches remained after disposal: {[str(c) for c in remaining_caches]}"
                )

            default_exists = (
                _run_git_or_none(route.path, "rev-parse", "--verify", default_ref) is not None
            )
            if not default_exists:
                run_git(slot.path, "checkout", "-b", default_branch, canonical_tip)
            else:
                run_git(slot.path, "checkout", default_branch)
                curr_head = _run_git_or_none(slot.path, "rev-parse", "HEAD")
                if curr_head != canonical_tip:
                    run_git(slot.path, "merge", "--ff-only", canonical_tip)

            final_snap = workspace_snapshot(slot.path)
            if not final_snap.stable or final_snap.porcelain:
                raise ValueError("workspace dirty after checkout")

            # Re-verify prep symlinks after checkout
            allowed_links = _get_allowed_prep_symlinks(slot.path, slot.route, self.config)
            for prep_link in allowed_links.values():
                if os.path.lexists(slot.path / prep_link.link_rel_path):
                    ok_sym, reason_sym = _verify_prep_symlink(slot.path, prep_link)
                    if not ok_sym:
                        raise ValueError(f"prep symlink broken after checkout: {reason_sym}")

            self.slots.release_cleanup(
                slot.name,
                op_id,
                expected_generation=slot.generation,
                expected_route=slot.route,
                expected_path=slot.path,
                status="available",
                note="auto-return to default branch completed",
            )
        except (GitError, OSError, ValueError, SlotStoreError) as exc:
            with suppress(Exception):
                self.slots.quarantine_cleanup(
                    slot.name,
                    op_id,
                    slot.generation,
                    expected_route=slot.route,
                    expected_path=slot.path,
                    note=f"auto-return failed: {exc}",
                )
            return {
                "slot": slot_name,
                "status": "failed",
                "reason": f"auto-return transition failed: {exc}",
            }

        self._event(
            "slot_auto_returned",
            op_id,
            {
                "slot": slot.name,
                "route": slot.route,
                "previous_branch": snapshot.branch,
                "default_branch": default_branch,
                "head": canonical_tip,
                "generation": slot.generation + 1,
            },
        )
        return {
            "slot": slot.name,
            "status": "returned",
            "previous_branch": snapshot.branch,
            "branch": default_branch,
            "head": canonical_tip,
            "generation": slot.generation + 1,
        }

    def auto_return_slots(
        self,
        *,
        route: str | None = None,
        refresh: bool = True,
    ) -> list[dict[str, Any]]:
        self._initialize()
        results: list[dict[str, Any]] = []
        fetched_routes: set[str] = set()
        for slot in self.slots.list_slots():
            if slot.name not in self.config.slots:
                continue
            if route is not None and slot.route != route:
                continue
            should_refresh = refresh and (slot.route not in fetched_routes)
            res = self.auto_return_slot(slot.name, refresh=should_refresh)
            if should_refresh:
                fetched_routes.add(slot.route)
            results.append(res)
        return results

    def poll(
        self, *, passes: int, interval_sec: float, max_interval_sec: float
    ) -> list[dict[str, Any]]:
        if passes <= 0 or interval_sec <= 0 or max_interval_sec < interval_sec:
            raise ValueError("poll bounds must be positive and max_interval_sec >= interval_sec")
        results = []
        delay = interval_sec
        for index in range(passes):
            results.append(self.reconcile(refresh=True, enqueue=True))
            if index + 1 < passes:
                self.sleep(delay)
                delay = min(max_interval_sec, delay * 2)
        return results

    def _audit_slot(
        self,
        slot: SlotRecord,
        *,
        route_cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        route = self.config.routes.get(slot.route)
        base = {
            "kind": "slot",
            "name": slot.name,
            "route": slot.route,
            "path": str(slot.path),
            "generation": slot.generation,
        }
        if route is None or not slot.path.exists():
            return {
                **base,
                "classification": LifecycleClass.STALE.value,
                "reasons": ["unknown route" if route is None else "registered path missing"],
            }
        if slot.active_job_id is not None or slot.status in {"active", "finalizing", "cleaning"}:
            return {
                **base,
                "classification": LifecycleClass.ACTIVE.value,
                "reasons": [f"slot owned by {slot.active_job_id}"],
            }
        try:
            snapshot = workspace_snapshot(slot.path)
            stat = slot.path.stat()
        except (GitError, OSError) as exc:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [str(exc)],
            }
        if not snapshot.stable:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["workspace identity changed during audit"],
            }
        if snapshot.porcelain:
            return {
                **base,
                "classification": LifecycleClass.DIRTY.value,
                "reasons": ["tracked or untracked workspace changes"],
            }

        # Check ignored files and preparation artifacts
        ok_ignored, err_ignored, _ = _inspect_ignored_entries(slot.path, slot.route, self.config)
        if not ok_ignored:
            return {
                **base,
                "classification": LifecycleClass.DIRTY.value,
                "reasons": [
                    err_ignored
                    or "unexpected ignored files present; workspace cannot be proven clean"
                ],
            }

        # Process CWD check
        try:
            pids = self.live_cwds(slot.path)
        except (PermissionError, OSError) as exc:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [f"process CWD inspection permission/namespace uncertainty: {exc}"],
            }
        if any(pid < 0 for pid in pids):
            reason = (
                "process CWD inspection is unavailable on this platform"
                if -1 in pids
                else "process CWD inspection permission or namespace uncertainty; cannot prove slot is inactive"
            )
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [reason],
            }
        if pids:
            return {
                **base,
                "classification": LifecycleClass.ACTIVE.value,
                "reasons": [f"live process CWDs: {pids}"],
            }

        r_info = route_cache.get(slot.route, {})
        if r_info.get("error"):
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [r_info["error"]],
            }
        canonical_ref = r_info["canonical_ref"]
        canonical_tip = r_info["canonical_tip"]
        canonical_url = r_info["canonical_url"]
        common_git_dir = r_info["common_git_dir"]

        if slot.path.resolve(strict=False) == route.path.resolve(strict=False):
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["slot path is canonical checkout of route"],
            }
        slot_root = self.config.slot_root_for(slot.route)
        if not is_same_or_child(slot.path, slot_root) or slot.path.resolve(
            strict=False
        ) == slot_root.resolve(strict=False):
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["slot path is outside managed slot_root"],
            }
        if slot.name in self.config.slots:
            configured = self.config.slots[slot.name]
            if configured.route != slot.route or configured.path.resolve(
                strict=False
            ) != slot.path.resolve(strict=False):
                return {
                    **base,
                    "classification": LifecycleClass.QUARANTINED.value,
                    "reasons": ["configured slot route or path mismatch"],
                }

        try:
            slot_common = str(
                (slot.path / run_git(slot.path, "rev-parse", "--git-common-dir")).resolve(
                    strict=False
                )
            )
        except GitError as exc:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [f"failed to resolve slot git-common-dir: {exc}"],
            }
        if slot_common != common_git_dir:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["slot git-common-dir differs from route common git dir"],
            }
        route_worktrees = r_info.get("worktrees")
        if route_worktrees is not None and slot.path.resolve(strict=False) not in route_worktrees:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["slot path is not in route git worktree inventory"],
            }

        # Validate default branch
        default_branch = f"slot/{slot.name}"
        default_ref = f"refs/heads/{default_branch}"
        default_sha = _run_git_or_none(route.path, "rev-parse", "--verify", default_ref)
        if default_sha is not None:
            def_tree = _commit_tree(route.path, default_sha)
            if not (
                _is_ancestor(route.path, default_sha, canonical_tip)
                or (def_tree and _has_canonical_tree(route.path, def_tree, canonical_tip))
            ):
                return {
                    **base,
                    "classification": LifecycleClass.QUARANTINED.value,
                    "reasons": [
                        f"default branch {default_branch} has divergent or unmerged commits"
                    ],
                }

        # Check if configured reusable slot is already returned to its default branch
        if slot.name in self.config.slots and snapshot.branch == default_branch:
            if not snapshot.head:
                return {
                    **base,
                    "classification": LifecycleClass.QUARANTINED.value,
                    "reasons": ["slot has no HEAD commit"],
                }
            head_tree = _commit_tree(slot.path, snapshot.head)
            if not (
                _is_ancestor(slot.path, snapshot.head, canonical_tip)
                or (head_tree and _has_canonical_tree(slot.path, head_tree, canonical_tip))
            ):
                return {
                    **base,
                    "classification": LifecycleClass.UNIQUE_UNPUSHED.value,
                    "reasons": ["default branch HEAD is not canonical-reachable"],
                }
            return {
                **base,
                "classification": LifecycleClass.AVAILABLE.value,
                "reasons": [],
            }

        accepted = self._accepted_item(slot)
        if accepted is None or accepted.checkpoint_sha is None or accepted.checkpoint_ref is None:
            return {
                **base,
                "classification": LifecycleClass.UNIQUE_UNPUSHED.value,
                "reasons": ["no exact accepted checkpoint receipt for slot generation"],
            }
        if accepted.workspace_path is not None and accepted.workspace_path.resolve(
            strict=False
        ) != slot.path.resolve(strict=False):
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["accepted receipt workspace_path mismatch"],
            }
        try:
            job = self.jobs.get_job(accepted.source_id)
        except KeyError:
            job = None
        if job is not None and (
            job.slot_name != slot.name
            or (job.slot_generation is not None and job.slot_generation != slot.generation)
            or job.route != slot.route
            or job.workspace_path.resolve(strict=False) != slot.path.resolve(strict=False)
        ):
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["durable job identity mismatch with slot generation"],
            }

        valid_heads = {accepted.checkpoint_sha}
        if accepted.base_sha is not None:
            valid_heads.add(accepted.base_sha)
        if snapshot.head not in valid_heads:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["slot HEAD differs from accepted checkpoint and base"],
            }
        try:
            checkpoint_sha = run_git(
                slot.path, "show-ref", "--verify", "--hash", accepted.checkpoint_ref
            )
        except GitError as exc:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": [f"checkpoint ref unavailable: {exc}"],
            }
        if checkpoint_sha != accepted.checkpoint_sha:
            return {
                **base,
                "classification": LifecycleClass.QUARANTINED.value,
                "reasons": ["checkpoint ref moved"],
            }

        checkpoint_tree = accepted.checkpoint_tree_sha or _commit_tree(
            slot.path, accepted.checkpoint_sha
        )
        checkpoint_integrated = _is_ancestor(slot.path, accepted.checkpoint_sha, canonical_tip) or (
            checkpoint_tree is not None
            and _has_canonical_tree(
                slot.path, checkpoint_tree, canonical_tip, base_sha=accepted.base_sha
            )
        )
        if not checkpoint_integrated:
            return {
                **base,
                "classification": LifecycleClass.UNIQUE_UNPUSHED.value,
                "reasons": ["accepted checkpoint is not canonical-reachable"],
            }

        head_tree = _commit_tree(slot.path, snapshot.head)
        head_integrated = _is_ancestor(slot.path, snapshot.head, canonical_tip) or (
            head_tree is not None
            and _has_canonical_tree(slot.path, head_tree, canonical_tip, base_sha=accepted.base_sha)
        )
        if not head_integrated:
            return {
                **base,
                "classification": LifecycleClass.UNIQUE_UNPUSHED.value,
                "reasons": ["checkout base HEAD is not canonical-reachable"],
            }

        frozen = FrozenSlot(
            name=slot.name,
            route=slot.route,
            path=slot.path,
            generation=slot.generation,
            device=stat.st_dev,
            inode=stat.st_ino,
            branch=snapshot.branch,
            head=snapshot.head or "",
            canonical_ref=canonical_ref,
            canonical_tip=canonical_tip,
            canonical_url=canonical_url,
            common_git_dir=common_git_dir,
            checkpoint_ref=accepted.checkpoint_ref,
            checkpoint_sha=accepted.checkpoint_sha,
            default_branch=default_branch,
            default_ref=default_ref,
            default_sha=default_sha,
            job_id=accepted.source_id,
            base_sha=accepted.base_sha,
        )
        proof = {**frozen.__dict__, "path": str(frozen.path)}
        return {
            **base,
            "classification": LifecycleClass.SAFE_TO_DELETE.value,
            "reasons": [],
            "operation_id": frozen.operation_id,
            "proof": proof,
        }

    def _accepted_item(
        self, slot: SlotRecord, *, expected_generation: int | None = None
    ) -> ReviewInboxItem | None:
        target_gen = expected_generation if expected_generation is not None else slot.generation
        items = self.inbox.list_items(review_status="accepted", limit=500)
        matches = [
            item
            for item in items
            if item.slot_name == slot.name
            and item.route == slot.route
            and (
                item.slot_generation == target_gen
                or (item.slot_generation is None and target_gen == 1)
            )
        ]
        return (
            max(matches, key=lambda item: item.reviewed_at or item.updated_at) if matches else None
        )

    def enqueue_reconciliation_request(self, route: str, reason: str = "acceptance") -> int:
        self._initialize()
        now = utc_now()
        with control_database(self.config.database_path) as db:
            cursor = db.execute(
                """
                insert into lifecycle_reconciliation_requests (route, reason, requested_at, status)
                values (?, ?, ?, 'pending')
                """,
                (route, reason, now),
            )
            request_id = cursor.lastrowid or 0
        self._event(
            "reconciliation_enqueued",
            None,
            {"route": route, "reason": reason, "request_id": request_id},
        )
        return request_id

    def pending_reconciliation_requests(self, route: str | None = None) -> list[dict[str, Any]]:
        self._initialize()
        with control_database(self.config.database_path) as db:
            if route:
                rows = db.execute(
                    "select * from lifecycle_reconciliation_requests where route = ? and status = 'pending' order by id",
                    (route,),
                ).fetchall()
            else:
                rows = db.execute(
                    "select * from lifecycle_reconciliation_requests where status = 'pending' order by id",
                ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_reconciliation_requests(self, request_ids: list[int]) -> int:
        if not request_ids:
            return 0
        self._initialize()
        now = utc_now()
        with control_database(self.config.database_path) as db:
            placeholders = ",".join("?" for _ in request_ids)
            cursor = db.execute(
                f"""
                update lifecycle_reconciliation_requests
                set status = 'processed', processed_at = ?
                where id in ({placeholders}) and status = 'pending'
                """,  # nosec B608
                (now, *request_ids),
            )
            return cursor.rowcount

    def mark_reconciliation_requests_processed(self, route: str) -> None:
        self._initialize()
        now = utc_now()
        with control_database(self.config.database_path) as db:
            db.execute(
                "update lifecycle_reconciliation_requests set status = 'processed', processed_at = ? where route = ? and status = 'pending'",
                (now, route),
            )

    def acquire_executor_lease(
        self,
        operation_id: str,
        executor_id: str,
        lease_duration_sec: float = 30.0,
        *,
        owner_pid: int | None = None,
    ) -> bool:
        self._initialize()
        now_sec = self.clock()
        expires_at = now_sec + lease_duration_sec
        pid_to_record = owner_pid if owner_pid is not None else os.getpid()
        with control_database(self.config.database_path) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select executor_id, owner_pid, lease_expires_at, fence_token from lifecycle_cleanup_leases where operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                curr_executor = row["executor_id"]
                curr_pid = row["owner_pid"]
                curr_expires = float(row["lease_expires_at"])
                curr_fence = int(row["fence_token"]) if row["fence_token"] is not None else 1

                if curr_executor == executor_id:
                    db.execute(
                        """
                        update lifecycle_cleanup_leases
                        set lease_expires_at = ?, heartbeat_at = ?, owner_pid = ?
                        where operation_id = ?
                        """,
                        (expires_at, utc_now(), pid_to_record, operation_id),
                    )
                    return True

                # Different executor attempting takeover:
                # Recovery must require proven process death or expired renewable ownership, not age alone.
                if curr_pid is not None:
                    if self.process_is_alive(curr_pid):
                        # Original executor process is still alive: reject takeover even if lease_expires_at <= now_sec
                        return False
                else:
                    # No owner PID recorded: require lease expiry
                    if curr_expires > now_sec:
                        return False

                # Proven process death or expired unowned lease: allow takeover with incremented monotonic fence token
                new_fence = curr_fence + 1
                db.execute(
                    """
                    update lifecycle_cleanup_leases
                    set executor_id = ?, owner_pid = ?, lease_expires_at = ?, fence_token = ?, acquired_at = ?, heartbeat_at = ?
                    where operation_id = ?
                    """,
                    (
                        executor_id,
                        pid_to_record,
                        expires_at,
                        new_fence,
                        utc_now(),
                        utc_now(),
                        operation_id,
                    ),
                )
                return True

            # First acquisition
            db.execute(
                """
                insert into lifecycle_cleanup_leases (operation_id, executor_id, owner_pid, fence_token, lease_expires_at, acquired_at, heartbeat_at)
                values (?, ?, ?, 1, ?, ?, ?)
                """,
                (operation_id, executor_id, pid_to_record, expires_at, utc_now(), utc_now()),
            )
            return True

    def get_fence_token(self, operation_id: str, executor_id: str) -> int | None:
        self._initialize()
        with control_database(self.config.database_path) as db:
            row = db.execute(
                "select fence_token from lifecycle_cleanup_leases where operation_id = ? and executor_id = ?",
                (operation_id, executor_id),
            ).fetchone()
            if row is not None and row["fence_token"] is not None:
                return int(row["fence_token"])
            return None

    def verify_fence(self, operation_id: str, executor_id: str, fence_token: int) -> None:
        self._initialize()
        with control_database(self.config.database_path) as db:
            row = db.execute(
                "select executor_id, fence_token from lifecycle_cleanup_leases where operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"Lease for operation {operation_id} no longer exists; executor {executor_id} is stale"
                )
            if row["executor_id"] != executor_id or int(row["fence_token"]) != fence_token:
                raise RuntimeError(
                    f"Fencing token mismatch for operation {operation_id}: current owner is {row['executor_id']} with fence {row['fence_token']}, but caller is {executor_id} with fence {fence_token}"
                )

    def heartbeat_executor_lease(
        self,
        operation_id: str,
        executor_id: str,
        fence_token: int,
        lease_duration_sec: float = 30.0,
    ) -> bool:
        self._initialize()
        now_sec = self.clock()
        expires_at = now_sec + lease_duration_sec
        with control_database(self.config.database_path) as db:
            cursor = db.execute(
                """
                update lifecycle_cleanup_leases
                set lease_expires_at = ?, heartbeat_at = ?
                where operation_id = ? and executor_id = ? and fence_token = ?
                """,
                (expires_at, utc_now(), operation_id, executor_id, fence_token),
            )
            return cursor.rowcount > 0

    def release_executor_lease(
        self, operation_id: str, executor_id: str, fence_token: int | None = None
    ) -> None:
        self._initialize()
        with control_database(self.config.database_path) as db:
            if fence_token is not None:
                db.execute(
                    "delete from lifecycle_cleanup_leases where operation_id = ? and executor_id = ? and fence_token = ?",
                    (operation_id, executor_id, fence_token),
                )
            else:
                db.execute(
                    "delete from lifecycle_cleanup_leases where operation_id = ? and executor_id = ?",
                    (operation_id, executor_id),
                )

    def _initialize(self) -> None:
        apply_schema_migration(
            self.config.database_path,
            component="slot_lifecycle",
            version=3,
            checksum="slot-lifecycle-v3-leases-20260914",
            migrate=_migrate,
        )
        apply_schema_migration(
            self.config.database_path,
            component="slot_lifecycle",
            version=4,
            checksum="slot-lifecycle-v4-fencing-20260914",
            migrate=_migrate_v4,
        )

    def _record_intent(self, operation_id: str, proof: dict[str, Any]) -> None:
        with control_database(self.config.database_path) as db:
            db.execute(
                "insert or ignore into lifecycle_cleanup_intents values (?, 'planned', ?, ?, null)",
                (operation_id, json.dumps(proof, sort_keys=True), utc_now()),
            )
        self._event("cleanup_enqueued", operation_id, proof)

    def _intent(self, operation_id: str) -> dict[str, Any] | None:
        with control_database(self.config.database_path) as db:
            row = db.execute(
                "select * from lifecycle_cleanup_intents where operation_id = ?", (operation_id,)
            ).fetchone()
        return (
            None
            if row is None
            else {
                "operation_id": row["operation_id"],
                "state": row["state"],
                "proof": json.loads(row["proof_json"]),
                "error": row["error"],
            }
        )

    def _set_intent_state(self, operation_id: str, state: str, error: str | None) -> None:
        with control_database(self.config.database_path) as db:
            db.execute(
                "update lifecycle_cleanup_intents set state = ?, error = ? where operation_id = ?",
                (state, error, operation_id),
            )

    def _record_step(self, operation_id: str, step_name: str, step_order: int, status: str) -> None:
        with control_database(self.config.database_path) as db:
            db.execute(
                """
                insert into lifecycle_cleanup_steps (operation_id, step_name, step_order, status, started_at, completed_at, error)
                values (?, ?, ?, ?, ?, null, null)
                on conflict(operation_id, step_name) do update set
                    status = excluded.status,
                    started_at = excluded.started_at
                """,
                (operation_id, step_name, step_order, status, utc_now()),
            )

    def _set_step_status(
        self, operation_id: str, step_name: str, status: str, error: str | None
    ) -> None:
        with control_database(self.config.database_path) as db:
            completed_at = utc_now() if status == "completed" else None
            db.execute(
                """
                update lifecycle_cleanup_steps
                set status = ?, completed_at = coalesce(?, completed_at), error = ?
                where operation_id = ? and step_name = ?
                """,
                (status, completed_at, error, operation_id, step_name),
            )

    def _get_step(self, operation_id: str, step_name: str) -> dict[str, Any] | None:
        with control_database(self.config.database_path) as db:
            row = db.execute(
                "select * from lifecycle_cleanup_steps where operation_id = ? and step_name = ?",
                (operation_id, step_name),
            ).fetchone()
        return dict(row) if row else None

    def _is_step_completed(self, operation_id: str, step_name: str) -> bool:
        step = self._get_step(operation_id, step_name)
        return step is not None and step.get("status") == "completed"

    def _has_any_steps_started(self, operation_id: str) -> bool:
        with control_database(self.config.database_path) as db:
            row = db.execute(
                "select 1 from lifecycle_cleanup_steps where operation_id = ?", (operation_id,)
            ).fetchone()
        return row is not None

    def _quarantine(
        self,
        operation_id: str,
        reason: str,
        evidence: dict[str, Any],
        *,
        frozen: FrozenSlot | FrozenBranch | None = None,
    ) -> dict[str, Any]:
        if isinstance(frozen, FrozenSlot):
            with suppress(Exception):
                self.slots.quarantine_cleanup(
                    frozen.name,
                    operation_id,
                    frozen.generation,
                    expected_route=frozen.route,
                    expected_path=frozen.path,
                    note=reason,
                )
        self._set_intent_state(operation_id, "quarantined", reason)
        result = {
            "operation_id": operation_id,
            "action": "quarantined",
            "reason": reason,
            "evidence": evidence,
        }
        self._event("cleanup_quarantined", operation_id, result)
        return result

    def _event(self, kind: str, operation_id: str | None, payload: dict[str, Any]) -> None:
        with control_database(self.config.database_path) as db:
            db.execute(
                "insert into lifecycle_audit_events(event_type, operation_id, payload_json, created_at) values (?, ?, ?, ?)",
                (kind, operation_id, json.dumps(payload, sort_keys=True), utc_now()),
            )


def _migrate(db: sqlite3.Connection) -> None:
    db.execute(
        """
        create table if not exists lifecycle_cleanup_intents (
            operation_id text primary key,
            state text not null,
            proof_json text not null,
            created_at text not null,
            error text
        )
        """
    )
    db.execute(
        """
        create table if not exists lifecycle_audit_events (
            id integer primary key autoincrement,
            event_type text not null,
            operation_id text,
            payload_json text not null,
            created_at text not null
        )
        """
    )
    db.execute(
        """
        create table if not exists lifecycle_cleanup_steps (
            operation_id text not null,
            step_name text not null,
            step_order integer not null,
            status text not null,
            started_at text,
            completed_at text,
            error text,
            primary key (operation_id, step_name)
        )
        """
    )
    db.execute(
        """
        create table if not exists lifecycle_reconciliation_requests (
            id integer primary key autoincrement,
            route text not null,
            reason text not null,
            requested_at text not null,
            status text not null,
            processed_at text
        )
        """
    )
    db.execute(
        """
        create table if not exists lifecycle_cleanup_leases (
            operation_id text primary key,
            executor_id text not null,
            owner_pid integer,
            fence_token integer not null default 1,
            lease_expires_at real not null,
            acquired_at text not null,
            heartbeat_at text
        )
        """
    )
    cols = {
        row["name"]
        for row in db.execute("pragma table_info(lifecycle_reconciliation_requests)").fetchall()
    }
    if "processed_at" not in cols:
        db.execute("alter table lifecycle_reconciliation_requests add column processed_at text")


def _migrate_v4(db: sqlite3.Connection) -> None:
    cols = {
        row["name"] for row in db.execute("pragma table_info(lifecycle_cleanup_leases)").fetchall()
    }
    if "owner_pid" not in cols:
        db.execute("alter table lifecycle_cleanup_leases add column owner_pid integer")
    if "fence_token" not in cols:
        db.execute(
            "alter table lifecycle_cleanup_leases add column fence_token integer not null default 1"
        )
    if "heartbeat_at" not in cols:
        db.execute("alter table lifecycle_cleanup_leases add column heartbeat_at text")


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    except GitError:
        return False
    return True


def _is_patch_equivalent(repo: Path, commit_sha: str, upstream_ref: str) -> bool:
    try:
        # If candidate-only range contains any merge commit, fail closed.
        merges_raw = run_git(repo, "rev-list", "--merges", f"{upstream_ref}..{commit_sha}")
        if merges_raw.strip():
            return False

        # Get all candidate-only non-merge commits.
        cand_raw = run_git(repo, "rev-list", "--no-merges", f"{upstream_ref}..{commit_sha}")
        candidate_non_merge_shas = {c.strip() for c in cand_raw.splitlines() if c.strip()}
        if not candidate_non_merge_shas:
            return False

        raw = run_git(repo, "cherry", upstream_ref, commit_sha)
    except GitError:
        return False

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return False

    cherry_shas: set[str] = set()
    for line in lines:
        if line.startswith("- "):
            sha = line[2:].strip()
            if sha:
                cherry_shas.add(sha)
        else:
            return False

    if not cherry_shas:
        return False

    return candidate_non_merge_shas == cherry_shas


def _commit_tree(repo: Path, commit_sha: str) -> str | None:
    if not commit_sha:
        return None
    return _run_git_or_none(repo, "rev-parse", "--verify", f"{commit_sha}^{{tree}}")


def _has_canonical_tree(
    repo: Path,
    tree_sha: str,
    canonical_tip: str,
    base_sha: str | None = None,
    max_commits: int = 500,
) -> bool:
    if not tree_sha or not canonical_tip:
        return False
    tip_tree = _run_git_or_none(repo, "rev-parse", "--verify", f"{canonical_tip}^{{tree}}")
    if tip_tree == tree_sha:
        return True
    try:
        if base_sha and _is_ancestor(repo, base_sha, canonical_tip):
            rev_spec = [f"-n{max_commits}", f"{base_sha}..{canonical_tip}"]
        else:
            rev_spec = [f"-n{max_commits}", canonical_tip]
        output = run_git(repo, "log", "--format=%T", *rev_spec)
        trees = {line.strip() for line in output.splitlines() if line.strip()}
        return tree_sha in trees
    except GitError:
        return False


def _run_git_or_none(repo: Path, *args: str) -> str | None:
    try:
        return run_git(repo, *args)
    except GitError:
        return None


def _live_process_cwds(root: Path) -> list[int]:
    if os.name == "nt" or not Path("/proc").is_dir():
        return [-1]
    canonical = root.resolve(strict=False)
    matches: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except (PermissionError, OSError):
        return [-2]
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OSError:
            return [-2]
        if cwd == canonical or cwd.is_relative_to(canonical):
            matches.append(int(entry.name))
    return matches


def _parse_worktrees(output: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "HEAD":
            current["head"] = value
    return records


@dataclass(frozen=True)
class AllowedPrepSymlink:
    name: str
    link_rel_path: str
    expected_target: Path
    marker: Path | None


# Explicit exact list of known disposable controller-generated cache names
KNOWN_DISPOSABLE_CACHE_NAMES: frozenset[str] = frozenset(
    {
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
    }
)


def _get_allowed_prep_symlinks(
    slot_path: Path,
    route_name: str,
    config: ControlConfig,
) -> dict[str, AllowedPrepSymlink]:
    allowed: dict[str, AllowedPrepSymlink] = {}
    for cmd in config.slot_prepare:
        if cmd.routes and route_name not in cmd.routes:
            continue
        working_dir = (
            (slot_path / cmd.working_dir).resolve(strict=False)
            if not cmd.working_dir.is_absolute()
            else cmd.working_dir.resolve(strict=False)
        )
        parts = list(cmd.command)
        if not parts:
            continue
        # Support ln -s [options] <target> [link_name]
        if parts[0] == "ln":
            flags = [p for p in parts[1:] if p.startswith("-")]
            if any("s" in f or f == "--symbolic" for f in flags):
                pos_args = [p for p in parts[1:] if not p.startswith("-") and p != "--"]
                if len(pos_args) == 2:
                    target_str, link_str = pos_args[0], pos_args[1]
                elif len(pos_args) == 1:
                    target_str = pos_args[0]
                    link_str = Path(target_str).name
                else:
                    continue
                target_p = Path(target_str)
                if target_p.is_absolute():
                    expected_target = target_p.resolve(strict=False)
                else:
                    expected_target = (working_dir / target_p).resolve(strict=False)
                link_full = working_dir / link_str
                try:
                    rel_p = Path(os.path.relpath(link_full, slot_path)).as_posix()
                    if rel_p.startswith(".."):
                        continue
                except ValueError:
                    continue
                marker_full = (
                    (slot_path / cmd.marker).resolve(strict=False)
                    if cmd.marker is not None
                    else None
                )
                allowed[rel_p] = AllowedPrepSymlink(
                    name=cmd.name,
                    link_rel_path=rel_p,
                    expected_target=expected_target,
                    marker=marker_full,
                )
        # Support mklink on Windows
        elif parts[0].lower() == "mklink" or (
            len(parts) >= 3
            and parts[0].lower() == "cmd"
            and parts[1].lower() == "/c"
            and parts[2].lower() == "mklink"
        ):
            mklink_args = parts[1:] if parts[0].lower() == "mklink" else parts[3:]
            non_flags = [p for p in mklink_args if not p.startswith("/")]
            if len(non_flags) >= 2:
                link_str, target_str = non_flags[0], non_flags[1]
                target_p = Path(target_str)
                if target_p.is_absolute():
                    expected_target = target_p.resolve(strict=False)
                else:
                    expected_target = (working_dir / target_p).resolve(strict=False)
                link_full = working_dir / link_str
                try:
                    rel_p = Path(os.path.relpath(link_full, slot_path)).as_posix()
                    if rel_p.startswith(".."):
                        continue
                except ValueError:
                    continue
                marker_full = (
                    (slot_path / cmd.marker).resolve(strict=False)
                    if cmd.marker is not None
                    else None
                )
                allowed[rel_p] = AllowedPrepSymlink(
                    name=cmd.name,
                    link_rel_path=rel_p,
                    expected_target=expected_target,
                    marker=marker_full,
                )
    return allowed


def _verify_prep_symlink(
    slot_path: Path,
    prep: AllowedPrepSymlink,
) -> tuple[bool, str | None]:
    full_path = slot_path / prep.link_rel_path
    if not os.path.lexists(full_path):
        return False, f"prep symlink does not exist: {prep.link_rel_path}"

    if not full_path.is_symlink():
        return False, f"prep path {prep.link_rel_path} is not a symlink (expected symlink)"

    try:
        raw_target = os.readlink(full_path)
    except OSError as exc:
        return False, f"could not read symlink {prep.link_rel_path}: {exc}"

    target_p = Path(raw_target)
    if target_p.is_absolute():
        actual_target = target_p.resolve(strict=False)
    else:
        actual_target = (full_path.parent / target_p).resolve(strict=False)

    if actual_target != prep.expected_target:
        return (
            False,
            f"prep symlink {prep.link_rel_path} target drift: {actual_target} != {prep.expected_target}",
        )

    if prep.marker is not None and not prep.marker.exists():
        return False, f"prep marker does not exist: {prep.marker}"

    return True, None


def _is_known_disposable_cache(slot_path: Path, rel_str: str) -> tuple[bool, Path | None]:
    rel_path = Path(rel_str.rstrip("/"))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return False, None

    full_path = slot_path / rel_path
    if not os.path.lexists(full_path):
        return False, None

    # Never treat symlinks as disposable caches
    if full_path.is_symlink():
        return False, None

    # Fail closed: check that no parent component under slot_path is a symlink
    curr = full_path.parent
    try:
        slot_resolved = slot_path.resolve(strict=False)
        while curr.resolve(strict=False) != slot_resolved and is_same_or_child(curr, slot_path):
            if curr.is_symlink():
                return False, None
            if curr == curr.parent:
                break
            curr = curr.parent
    except OSError:
        return False, None

    # Check if this exact path is a known cache directory
    if rel_path.name in KNOWN_DISPOSABLE_CACHE_NAMES:
        if full_path.is_dir():
            return True, full_path
        return False, None

    # Check if this path is inside a known cache directory
    for parent in rel_path.parents:
        if parent.name in KNOWN_DISPOSABLE_CACHE_NAMES:
            parent_full = slot_path / parent
            if parent_full.is_dir() and not parent_full.is_symlink():
                if (
                    parent.name == "__pycache__"
                    and full_path.is_file()
                    and not full_path.name.endswith((".pyc", ".pyo"))
                ):
                    return False, None
                return True, parent_full

    return False, None


def _inspect_ignored_entries(
    slot_path: Path,
    route_name: str,
    config: ControlConfig,
) -> tuple[bool, str | None, set[Path]]:
    try:
        ignored_raw = run_git(slot_path, "status", "--porcelain=v1", "-uall", "--ignored=matching")
    except GitError as exc:
        return False, f"failed to check ignored workspace files: {exc}", set()

    ignored_lines = [
        line[3:].strip() for line in ignored_raw.splitlines() if line.startswith("!! ")
    ]
    if not ignored_lines:
        return True, None, set()

    allowed_symlinks = _get_allowed_prep_symlinks(slot_path, route_name, config)

    # If any allowed prep symlink is present on disk, verify it!
    for rel_p, prep_link in allowed_symlinks.items():
        full_p = slot_path / rel_p
        if os.path.lexists(full_p):
            ok, reason = _verify_prep_symlink(slot_path, prep_link)
            if not ok:
                return False, f"invalid preparation symlink: {reason}", set()

    caches_to_remove: set[Path] = set()
    unknown_entries: list[str] = []

    for raw_entry in ignored_lines:
        rel_norm = raw_entry.rstrip("/")
        if rel_norm in allowed_symlinks:
            continue
        is_cache, cache_root = _is_known_disposable_cache(slot_path, raw_entry)
        if is_cache and cache_root is not None:
            caches_to_remove.add(cache_root)
        else:
            unknown_entries.append(raw_entry)

    if unknown_entries:
        return (
            False,
            f"unexpected ignored files present; workspace cannot be proven clean: {unknown_entries[:3]}",
            set(),
        )

    return True, None, caches_to_remove
