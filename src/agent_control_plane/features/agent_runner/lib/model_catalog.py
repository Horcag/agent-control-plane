from __future__ import annotations

import fnmatch
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from agent_control_plane.shared.config import CodexModelCatalogConfig


@dataclass(frozen=True)
class CatalogRate:
    """One token rate expressed per million tokens."""

    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class CatalogModelMetadata:
    """Explicit ACP policy and accounting metadata for one model."""

    model: str
    premium: bool = False
    quota_domain: str | None = None
    capacity_units: tuple[tuple[str, int], ...] = ()
    credit_rate: CatalogRate | None = None
    api_usd_rate: CatalogRate | None = None
    rate_card_version: str | None = None
    rate_card_source: str | None = None


@dataclass(frozen=True)
class CatalogModelRule:
    """ACP policy and accounting rule matching model IDs by glob pattern."""

    match: str
    premium: bool = False
    quota_domain: str | None = None
    capacity_units: tuple[tuple[str, int], ...] = ()
    credit_rate: CatalogRate | None = None
    api_usd_rate: CatalogRate | None = None
    rate_card_version: str | None = None
    rate_card_source: str | None = None


@dataclass(frozen=True)
class CatalogModel:
    model: str
    visible: bool
    priority: int | None
    default_reasoning_effort: str | None
    supported_reasoning_efforts: tuple[str, ...]


@dataclass(frozen=True)
class CatalogPriceEstimate:
    """Current catalog pricing reconstructed from raw token counts."""

    estimated_credits: float | None
    estimated_api_usd: float | None
    rate_card_version: str | None
    rate_card_source: str | None


@dataclass(frozen=True)
class CatalogSnapshot:
    """Immutable snapshot of the model catalog at a single point in time."""

    models: dict[str, CatalogModel]
    metadata: dict[str, CatalogModelMetadata]
    model_rules: tuple[CatalogModelRule, ...] = ()
    cache_status: str = "missing"
    source: str = "cache"
    version: str | None = None
    fetched_at: str | None = None
    etag: str | None = None
    client_version: str | None = None
    snapshot_state: str = "current"
    on_disk_version: str | None = None
    label: str = "Codex"
    history_models: dict[str, tuple[CatalogModel, str]] = field(default_factory=dict)
    unknown_model_policy: str = "warn"

    def model(self, model: str) -> CatalogModel | None:
        norm = _normalize_model(model)
        if norm in self.models:
            return self.models[norm]
        if norm in self.history_models:
            return self.history_models[norm][0]
        return None

    def validate_automatic_profile(self, model: str, reasoning_effort: str) -> None:
        self.resolve_automatic_profile(model, reasoning_effort)

    def resolve_automatic_profile(self, model: str, reasoning_effort: str) -> str:
        candidate = self._automatic_candidate(model)
        self._validate_known_effort(candidate, reasoning_effort)
        return candidate.model

    def _automatic_candidate(self, model: str) -> CatalogModel:
        normalized_model = _normalize_model(model)
        candidate = (
            self._default_visible_candidate()
            if normalized_model == "default"
            else self.model(model)
        )
        if candidate is None:
            if self.cache_status != "loaded":
                raise ValueError(
                    f"{self.label} model catalog is "
                    f"{self.cache_status}; automatic routing needs a current cache inventory"
                )
            if normalized_model == "default":
                raise ValueError(
                    f"{self.label} model selector 'default' could not resolve to a visible "
                    "candidate in the current model catalog"
                )
            raise ValueError(
                f"{self.label} model {model!r} is not a visible candidate "
                "in the current model catalog"
            )
        if not candidate.visible:
            raise ValueError(
                f"{self.label} model {model!r} is not visible in the current model catalog"
            )
        return candidate

    def _default_visible_candidate(self) -> CatalogModel | None:
        candidates = tuple(model for model in self.models.values() if model.visible)
        if not candidates:
            return None
        _, candidate = min(
            enumerate(candidates),
            key=lambda item: (
                item[1].priority is None,
                item[1].priority if item[1].priority is not None else 0,
                item[0],
            ),
        )
        return candidate

    def validate_explicit_profile(self, model: str, reasoning_effort: str) -> None:
        candidate = self.model(model)
        if candidate is not None:
            self._validate_known_effort(candidate, reasoning_effort)

    def _match_rule(self, model: str) -> CatalogModelMetadata | None:
        norm = _normalize_model(model)
        for rule in self.model_rules:
            if fnmatch.fnmatch(norm, rule.match.strip().lower()):
                return CatalogModelMetadata(
                    model=model,
                    premium=rule.premium,
                    quota_domain=rule.quota_domain,
                    capacity_units=rule.capacity_units,
                    credit_rate=rule.credit_rate,
                    api_usd_rate=rule.api_usd_rate,
                    rate_card_version=rule.rate_card_version,
                    rate_card_source=rule.rate_card_source,
                )
        return None

    def rate_metadata_for(self, model: str) -> CatalogModelMetadata | None:
        norm = _normalize_model(model)
        if norm in self.metadata:
            return self.metadata[norm]
        return self._match_rule(model)

    def launch_disposition(self, model: str, *, override_reason: str | None = None) -> str:
        """Return 'allow', 'require_override', or 'reject' based on catalog policy."""
        meta = self.rate_metadata_for(model)
        if meta is not None and meta.premium:
            if override_reason is not None and override_reason.strip():
                return "allow"
            return "require_override"
        if meta is None and self.unknown_model_policy == "require_override":
            if override_reason is not None and override_reason.strip():
                return "allow"
            return "require_override"
        return "allow"

    def reprice(
        self,
        model: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> CatalogPriceEstimate:
        """Recompute current estimates from raw usage instead of stale stored currency."""
        metadata = self.rate_metadata_for(model)
        if metadata is None:
            return CatalogPriceEstimate(None, None, None, None)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        return CatalogPriceEstimate(
            estimated_credits=_estimate_rate(
                metadata.credit_rate,
                uncached_input_tokens=uncached_input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
            ),
            estimated_api_usd=_estimate_rate(
                metadata.api_usd_rate,
                uncached_input_tokens=uncached_input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
            ),
            rate_card_version=metadata.rate_card_version,
            rate_card_source=metadata.rate_card_source,
        )

    def compute_alerts(
        self,
        *,
        policy_first_candidates: Mapping[str, str] | None = None,
        recent_observations: list[dict[str, Any]] | None = None,
        first_seen_at_by_model: dict[str, str] | None = None,
        newness_window_days: float = 7.0,
        has_prior_observations: bool = False,
        prior_observed_models: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []

        if prior_observed_models is None and recent_observations:
            current_ver = self.version
            prior_obs = [obs for obs in recent_observations if obs.get("version") != current_ver]
            if prior_obs:
                has_prior_observations = True
                prior_observed_models = set()
                for obs in prior_obs:
                    for m in obs.get("models", []):
                        if isinstance(m, dict) and isinstance(m.get("model"), str):
                            prior_observed_models.add(_normalize_model(m["model"]))

        # 1. unclassified_model
        for key, model_item in self.models.items():
            if self.rate_metadata_for(key) is None:
                model_name = model_item.model
                priority = model_item.priority
                first_seen_at = (
                    first_seen_at_by_model.get(key)
                    if first_seen_at_by_model and key in first_seen_at_by_model
                    else self.fetched_at
                )
                in_prior = prior_observed_models is not None and key in prior_observed_models
                within_window = _is_within_newness_window(
                    first_seen_at, self.fetched_at, newness_window_days
                )
                is_new = has_prior_observations and (not in_prior) and within_window
                severity = "warning" if (is_new and model_item.visible) else "info"
                alerts.append(
                    {
                        "code": "unclassified_model",
                        "message": f"Model {model_name!r} is in inventory but has no ACP metadata",
                        "severity": severity,
                        "model": model_name,
                        "priority": priority,
                        "first_seen_at": first_seen_at,
                    }
                )

        # 2. outranks_configured_ladder
        if policy_first_candidates:
            outranking_models: list[CatalogModel] = []
            policy_map_by_model: dict[str, list[tuple[str, str, int]]] = {}

            for policy_name, first_candidate in policy_first_candidates.items():
                cand_item = self.model(first_candidate)
                if cand_item is not None and cand_item.priority is not None:
                    conf_p = cand_item.priority
                    conf_model = cand_item.model
                    for vis_item in self.models.values():
                        if (
                            vis_item.visible
                            and vis_item.priority is not None
                            and vis_item.priority < conf_p
                        ):
                            norm_m = _normalize_model(vis_item.model)
                            if norm_m not in policy_map_by_model:
                                outranking_models.append(vis_item)
                                policy_map_by_model[norm_m] = []
                            policy_map_by_model[norm_m].append((policy_name, conf_model, conf_p))

            if outranking_models:
                best_vis = min(
                    outranking_models,
                    key=lambda m: (m.priority if m.priority is not None else float("inf"), m.model),
                )
                best_key = _normalize_model(best_vis.model)
                first_seen_at = (
                    first_seen_at_by_model.get(best_key)
                    if first_seen_at_by_model and best_key in first_seen_at_by_model
                    else self.fetched_at
                )
                is_unclassified = self.rate_metadata_for(best_key) is None
                is_new = (
                    has_prior_observations
                    and (
                        prior_observed_models is not None and best_key not in prior_observed_models
                    )
                    and _is_within_newness_window(
                        first_seen_at, self.fetched_at, newness_window_days
                    )
                )

                if is_unclassified or is_new:
                    entries = policy_map_by_model[best_key]
                    unique_pols = sorted(list(dict.fromkeys(e[0] for e in entries)))
                    first_entry = entries[0]
                    conf_m = first_entry[1]
                    conf_p = first_entry[2]
                    alerts.append(
                        {
                            "code": "outranks_configured_ladder",
                            "message": (
                                f"Visible model {best_vis.model!r} (priority {best_vis.priority}) outranks first candidate "
                                f"{conf_m!r} (priority {conf_p}) of configured routing policy {', '.join(unique_pols)}"
                            ),
                            "severity": "warning",
                            "policy_names": unique_pols,
                            "visible_model": best_vis.model,
                            "visible_priority": best_vis.priority,
                            "configured_model": conf_m,
                            "configured_priority": conf_p,
                        }
                    )

        # 3. metadata_without_inventory
        for key, meta_item in self.metadata.items():
            if key not in self.models:
                alerts.append(
                    {
                        "code": "metadata_without_inventory",
                        "message": f"ACP metadata exists for model {meta_item.model!r} which is absent from current inventory",
                        "severity": "info",
                        "model": meta_item.model,
                    }
                )

        # 4. inventory_shrank
        if recent_observations:
            current_slugs = set(self.models.keys())
            for obs in recent_observations:
                obs_models_raw = obs.get("models", [])
                obs_slugs = {
                    _normalize_model(m["model"])
                    for m in obs_models_raw
                    if isinstance(m, dict)
                    and isinstance(m.get("model"), str)
                    and m["model"].strip()
                }
                if current_slugs and obs_slugs and current_slugs < obs_slugs:
                    vanished = sorted(
                        [
                            m["model"].strip()
                            for m in obs_models_raw
                            if isinstance(m, dict)
                            and isinstance(m.get("model"), str)
                            and _normalize_model(m["model"]) not in current_slugs
                        ]
                    )
                    alerts.append(
                        {
                            "code": "inventory_shrank",
                            "message": (
                                f"Current model inventory ({len(current_slugs)} models) is a strict subset "
                                f"of observation {obs.get('version')} ({len(obs_slugs)} models); vanished: {', '.join(vanished)}"
                            ),
                            "severity": "warning",
                            "vanished_slugs": vanished,
                            "current_client_version": self.client_version,
                            "observed_client_version": obs.get("client_version"),
                        }
                    )
                    break

        # 5. client_version_regressed
        if self.client_version and recent_observations:
            for obs in recent_observations:
                obs_cv = obs.get("client_version")
                if obs_cv and obs_cv != self.client_version:
                    if _is_version_regressed(self.client_version, obs_cv):
                        alerts.append(
                            {
                                "code": "client_version_regressed",
                                "message": f"Client version regressed from {obs_cv} to {self.client_version}",
                                "severity": "info",
                                "current_client_version": self.client_version,
                                "previous_client_version": obs_cv,
                            }
                        )
                    break

        # 6. snapshot_drifted
        if self.snapshot_state == "drifted":
            alerts.append(
                {
                    "code": "snapshot_drifted",
                    "message": (
                        f"Model catalog snapshot has drifted: serving version {self.version} "
                        f"while on-disk version is {self.on_disk_version}"
                    ),
                    "severity": "warning",
                    "version": self.version,
                    "on_disk_version": self.on_disk_version,
                }
            )

        return alerts

    def inspection_payload(
        self,
        *,
        policy_first_candidates: Mapping[str, str] | None = None,
        recent_observations: list[dict[str, Any]] | None = None,
        first_seen_at_by_model: dict[str, str] | None = None,
        newness_window_days: float = 7.0,
        has_prior_observations: bool = False,
        prior_observed_models: set[str] | None = None,
    ) -> dict[str, Any]:
        """Return bounded, policy-safe catalog data without cache instruction blobs."""
        union_keys: list[str] = []
        for k in self.models:
            if k not in union_keys:
                union_keys.append(k)
        for k in self.history_models:
            if k not in union_keys:
                union_keys.append(k)
        for k in self.metadata:
            if k not in union_keys:
                union_keys.append(k)

        model_entries: list[dict[str, Any]] = []
        for key in union_keys:
            cache_item = self.models.get(key)
            history_tuple = self.history_models.get(key)
            meta_item = self.metadata.get(key)

            last_seen_at: str | None = None

            if cache_item is not None:
                model_name = cache_item.model
                visible: bool | None = cache_item.visible
                priority: int | None = cache_item.priority
                default_effort: str | None = cache_item.default_reasoning_effort
                supported_efforts: list[str] = list(cache_item.supported_reasoning_efforts)
                inventory_state = "listed" if cache_item.visible else "hidden"
                last_seen_at = self.fetched_at
            elif history_tuple is not None:
                hist_item, hist_last_seen = history_tuple
                model_name = hist_item.model
                visible = hist_item.visible
                priority = hist_item.priority
                default_effort = hist_item.default_reasoning_effort
                supported_efforts = list(hist_item.supported_reasoning_efforts)
                inventory_state = "last_seen"
                last_seen_at = hist_last_seen
            else:
                model_name = meta_item.model if meta_item is not None else key
                visible = None
                priority = None
                default_effort = None
                supported_efforts = []
                inventory_state = "absent_from_inventory"
                last_seen_at = None

            if meta_item is not None:
                metadata_state = "configured"
                quota_domain = meta_item.quota_domain
                premium: bool | None = meta_item.premium
                premium_state = "known"
                rate_card_version = meta_item.rate_card_version
                rate_card_source = meta_item.rate_card_source
                has_credit_rate = meta_item.credit_rate is not None
                has_api_usd_rate = meta_item.api_usd_rate is not None
            else:
                rule_meta = self._match_rule(model_name)
                if rule_meta is not None:
                    metadata_state = "rule"
                    quota_domain = rule_meta.quota_domain
                    premium = rule_meta.premium
                    premium_state = "known"
                    rate_card_version = rule_meta.rate_card_version
                    rate_card_source = rule_meta.rate_card_source
                    has_credit_rate = rule_meta.credit_rate is not None
                    has_api_usd_rate = rule_meta.api_usd_rate is not None
                else:
                    metadata_state = "unconfigured"
                    quota_domain = None
                    premium = None
                    premium_state = "unknown"
                    rate_card_version = None
                    rate_card_source = None
                    has_credit_rate = False
                    has_api_usd_rate = False

            entry = {
                "model": model_name,
                "visible": visible,
                "priority": priority,
                "default_reasoning_effort": default_effort,
                "supported_reasoning_efforts": supported_efforts,
                "quota_domain": quota_domain,
                "premium": premium,
                "premium_state": premium_state,
                "rate_card_version": rate_card_version,
                "rate_card_source": rate_card_source,
                "has_credit_rate": has_credit_rate,
                "has_api_usd_rate": has_api_usd_rate,
                "inventory_state": inventory_state,
                "metadata_state": metadata_state,
                "launch_disposition": self.launch_disposition(model_name),
                "last_seen_at": last_seen_at,
            }
            model_entries.append(entry)

        alerts = self.compute_alerts(
            policy_first_candidates=policy_first_candidates,
            recent_observations=recent_observations,
            first_seen_at_by_model=first_seen_at_by_model,
            newness_window_days=newness_window_days,
            has_prior_observations=has_prior_observations,
            prior_observed_models=prior_observed_models,
        )

        return {
            "status": self.cache_status,
            "snapshot_state": self.snapshot_state,
            "source": self.source,
            "version": self.version,
            "on_disk_version": self.on_disk_version,
            "fetched_at": self.fetched_at,
            "etag": self.etag,
            "client_version": self.client_version,
            "models": model_entries,
            "alerts": alerts,
        }

    def quota_domain_for(self, model: str | None) -> str:
        if model is None:
            return "primary"
        metadata = self.rate_metadata_for(model)
        if metadata is None or metadata.quota_domain is None:
            return "primary"
        return metadata.quota_domain

    def capacity_units_for(
        self,
        model: str,
        reasoning_effort: str,
        *,
        full_capacity: int,
    ) -> int:
        metadata = self.rate_metadata_for(model)
        if metadata is None:
            return full_capacity
        requested_effort = reasoning_effort.strip().lower()
        for effort, units in metadata.capacity_units:
            if effort == requested_effort:
                return min(full_capacity, units)
        return full_capacity

    def _validate_known_effort(self, candidate: CatalogModel, reasoning_effort: str) -> None:
        effort = reasoning_effort.strip().lower()
        if not effort:
            raise ValueError(f"{self.label} reasoning effort must not be empty")
        supported = candidate.supported_reasoning_efforts
        if effort in supported:
            return
        allowed = ", ".join(supported) or "none declared by the catalog"
        raise ValueError(
            f"{self.label} model {candidate.model!r} does not support reasoning effort "
            f"{effort!r}. Expected one of: {allowed}"
        )


class ModelCatalog:
    """Read-only merger of local inventory and ACP-owned metadata, with stat-based auto-revalidation."""

    def __init__(
        self,
        *,
        models: dict[str, CatalogModel],
        metadata: dict[str, CatalogModelMetadata],
        model_rules: tuple[CatalogModelRule, ...] = (),
        cache_status: str,
        source: str,
        version: str | None,
        label: str = "Codex",
        fetched_at: str | None = None,
        etag: str | None = None,
        client_version: str | None = None,
        snapshot_state: str = "current",
        on_disk_version: str | None = None,
        cache_path: Path | None = None,
        max_cache_age_sec: float = 0.0,
        observation_store: Any = None,
        db_path: Path | None = None,
        observation_window_days: float = 7.0,
        observation_retention_days: float = 30.0,
        max_observation_rows: int = 500,
        newness_window_days: float = 7.0,
        history_models: dict[str, tuple[CatalogModel, str]] | None = None,
        unknown_model_policy: str = "warn",
    ) -> None:
        self._lock = threading.Lock()
        self._cache_path = cache_path
        self._max_cache_age_sec = max_cache_age_sec
        self._metadata_dict = metadata
        self._model_rules = tuple(model_rules or ())
        self._source = source
        self._label = label
        self._unknown_model_policy = unknown_model_policy
        self._last_stat_key: tuple[int, int] | None = None
        self._observation_window_days = observation_window_days
        self._observation_retention_days = observation_retention_days
        self._max_observation_rows = max_observation_rows
        self._newness_window_days = newness_window_days

        if observation_store is not None:
            self._observation_store = observation_store
        elif db_path is not None:
            from agent_control_plane.entities.job import ModelObservationStore

            self._observation_store = ModelObservationStore(db_path)
        else:
            self._observation_store = None

        if cache_path is not None:
            try:
                st = cache_path.stat()
                self._last_stat_key = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._last_stat_key = None

        self._snapshot = CatalogSnapshot(
            models=models,
            metadata=metadata,
            model_rules=self._model_rules,
            cache_status=cache_status,
            source=source,
            version=version,
            fetched_at=fetched_at,
            etag=etag,
            client_version=client_version,
            snapshot_state=snapshot_state,
            on_disk_version=on_disk_version if on_disk_version is not None else version,
            label=label,
            history_models=history_models or {},
            unknown_model_policy=unknown_model_policy,
        )

    @classmethod
    def load(
        cls,
        *,
        cache_path: Path,
        max_cache_age_sec: float,
        metadata: tuple[CatalogModelMetadata, ...] = (),
        model_rules: tuple[CatalogModelRule, ...] = (),
        now: float | None = None,
        db_path: Path | None = None,
        observation_store: Any = None,
        observation_window_days: float = 7.0,
        observation_retention_days: float = 30.0,
        max_observation_rows: int = 500,
        newness_window_days: float = 7.0,
        unknown_model_policy: str = "warn",
    ) -> ModelCatalog:
        normalized_metadata = _metadata_by_model(metadata)
        current_time = time.time() if now is None else now
        cache_status, models, version, fetched_at, etag, client_version, raw_sha = (
            _read_and_load_cache(
                cache_path,
                max_cache_age_sec=max_cache_age_sec,
                now=current_time,
            )
        )
        obs_store = observation_store
        if obs_store is None and db_path is not None:
            from agent_control_plane.entities.job import ModelObservationStore

            obs_store = ModelObservationStore(db_path)

        history_models: dict[str, tuple[CatalogModel, str]] = {}
        if obs_store is not None:
            now_iso = _timestamp_to_iso(current_time)
            if cache_status in ("loaded", "stale") and version is not None:
                obs_models = [
                    {
                        "model": m.model,
                        "visible": m.visible,
                        "priority": m.priority,
                        "default_reasoning_effort": m.default_reasoning_effort,
                        "supported_reasoning_efforts": list(m.supported_reasoning_efforts),
                    }
                    for m in models.values()
                ]
                obs_store.record_observation(
                    version=version,
                    content_hash=version,
                    fetched_at=fetched_at,
                    etag=etag,
                    client_version=client_version,
                    observed_models=obs_models,
                    now=now_iso,
                    retention_days=observation_retention_days,
                    max_rows=max_observation_rows,
                )
            history_models = _get_history_models(
                obs_store=obs_store,
                current_models=models,
                window_days=observation_window_days,
                now=now_iso,
            )

        return cls(
            models=models,
            metadata=normalized_metadata,
            model_rules=model_rules,
            cache_status=cache_status,
            source=cache_path.name,
            version=version,
            fetched_at=fetched_at,
            etag=etag,
            client_version=client_version,
            snapshot_state="current",
            on_disk_version=raw_sha if raw_sha is not None else version,
            label="Codex",
            cache_path=cache_path,
            max_cache_age_sec=max_cache_age_sec,
            observation_store=obs_store,
            db_path=db_path,
            observation_window_days=observation_window_days,
            observation_retention_days=observation_retention_days,
            max_observation_rows=max_observation_rows,
            newness_window_days=newness_window_days,
            history_models=history_models,
            unknown_model_policy=unknown_model_policy,
        )

    @classmethod
    def from_config(
        cls,
        config: CodexModelCatalogConfig,
        db_path: Path | None = None,
        observation_store: Any = None,
    ) -> ModelCatalog:
        return cls.load(
            cache_path=config.cache_path,
            max_cache_age_sec=config.max_cache_age_sec,
            metadata=tuple(
                CatalogModelMetadata(
                    model=item.model,
                    premium=item.premium,
                    quota_domain=item.quota_domain,
                    capacity_units=item.capacity_units,
                    credit_rate=(
                        CatalogRate(
                            item.credit_rate.input,
                            item.credit_rate.cached_input,
                            item.credit_rate.output,
                        )
                        if item.credit_rate is not None
                        else None
                    ),
                    api_usd_rate=(
                        CatalogRate(
                            item.api_usd_rate.input,
                            item.api_usd_rate.cached_input,
                            item.api_usd_rate.output,
                        )
                        if item.api_usd_rate is not None
                        else None
                    ),
                    rate_card_version=item.rate_card_version,
                    rate_card_source=item.rate_card_source,
                )
                for item in config.models
            ),
            model_rules=tuple(
                CatalogModelRule(
                    match=item.match,
                    premium=item.premium,
                    quota_domain=item.quota_domain,
                    capacity_units=item.capacity_units,
                    credit_rate=(
                        CatalogRate(
                            item.credit_rate.input,
                            item.credit_rate.cached_input,
                            item.credit_rate.output,
                        )
                        if item.credit_rate is not None
                        else None
                    ),
                    api_usd_rate=(
                        CatalogRate(
                            item.api_usd_rate.input,
                            item.api_usd_rate.cached_input,
                            item.api_usd_rate.output,
                        )
                        if item.api_usd_rate is not None
                        else None
                    ),
                    rate_card_version=item.rate_card_version,
                    rate_card_source=item.rate_card_source,
                )
                for item in config.model_rules
            ),
            db_path=db_path,
            observation_store=observation_store,
            observation_window_days=getattr(config, "observation_window_days", 7.0),
            observation_retention_days=getattr(config, "observation_retention_days", 30.0),
            max_observation_rows=getattr(config, "max_observation_rows", 500),
            newness_window_days=getattr(config, "newness_window_days", 7.0),
            unknown_model_policy=getattr(config, "unknown_model_policy", "warn"),
        )

    @property
    def snapshot(self) -> CatalogSnapshot:
        self._ensure_fresh()
        return self._snapshot

    @property
    def cache_status(self) -> str:
        return self.snapshot.cache_status

    @property
    def source(self) -> str:
        return self.snapshot.source

    @property
    def version(self) -> str | None:
        return self.snapshot.version

    @property
    def label(self) -> str:
        return self.snapshot.label

    @property
    def _models(self) -> dict[str, CatalogModel]:
        return self.snapshot.models

    @property
    def _metadata(self) -> dict[str, CatalogModelMetadata]:
        return self.snapshot.metadata

    def _ensure_fresh(self) -> None:
        if self._cache_path is None:
            return

        try:
            st = self._cache_path.stat()
            current_key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            current_key = None

        if current_key == self._last_stat_key:
            return

        with self._lock:
            # Double-check inside lock
            try:
                st = self._cache_path.stat()
                current_key = (st.st_mtime_ns, st.st_size)
            except OSError:
                current_key = None

            if current_key == self._last_stat_key:
                return

            cache_status, models, version, fetched_at, etag, client_version, raw_sha = (
                _read_and_load_cache(
                    self._cache_path,
                    max_cache_age_sec=self._max_cache_age_sec,
                    now=time.time(),
                )
            )

            if current_key is not None and cache_status in ("loaded", "stale"):
                history_models: dict[str, tuple[CatalogModel, str]] = {}
                if self._observation_store is not None:
                    now_iso = _timestamp_to_iso(time.time())
                    if version is not None:
                        obs_models = [
                            {
                                "model": m.model,
                                "visible": m.visible,
                                "priority": m.priority,
                                "default_reasoning_effort": m.default_reasoning_effort,
                                "supported_reasoning_efforts": list(m.supported_reasoning_efforts),
                            }
                            for m in models.values()
                        ]
                        self._observation_store.record_observation(
                            version=version,
                            content_hash=version,
                            fetched_at=fetched_at,
                            etag=etag,
                            client_version=client_version,
                            observed_models=obs_models,
                            now=now_iso,
                            retention_days=self._observation_retention_days,
                            max_rows=self._max_observation_rows,
                        )
                    history_models = _get_history_models(
                        obs_store=self._observation_store,
                        current_models=models,
                        window_days=self._observation_window_days,
                        now=now_iso,
                    )
                self._last_stat_key = current_key
                self._snapshot = CatalogSnapshot(
                    models=models,
                    metadata=self._metadata_dict,
                    model_rules=self._model_rules,
                    cache_status=cache_status,
                    source=self._source,
                    version=version,
                    fetched_at=fetched_at,
                    etag=etag,
                    client_version=client_version,
                    snapshot_state="current",
                    on_disk_version=raw_sha if raw_sha is not None else version,
                    label=self._label,
                    history_models=history_models,
                    unknown_model_policy=self._unknown_model_policy,
                )
            else:
                # Refresh failed (file missing, unreadable, or invalid JSON)
                if self._snapshot.version is not None:
                    # Keep serving previous good snapshot, but mark snapshot_state as "drifted"
                    self._last_stat_key = current_key
                    self._snapshot = dataclass_replace(
                        self._snapshot,
                        snapshot_state="drifted",
                        on_disk_version=raw_sha,
                    )
                else:
                    self._last_stat_key = current_key
                    self._snapshot = CatalogSnapshot(
                        models=models,
                        metadata=self._metadata_dict,
                        model_rules=self._model_rules,
                        cache_status=cache_status,
                        source=self._source,
                        version=version,
                        fetched_at=fetched_at,
                        etag=etag,
                        client_version=client_version,
                        snapshot_state="current",
                        on_disk_version=raw_sha,
                        label=self._label,
                        unknown_model_policy=self._unknown_model_policy,
                    )

    def model(self, model: str) -> CatalogModel | None:
        return self.snapshot.model(model)

    def validate_automatic_profile(self, model: str, reasoning_effort: str) -> None:
        self.snapshot.validate_automatic_profile(model, reasoning_effort)

    def resolve_automatic_profile(self, model: str, reasoning_effort: str) -> str:
        return self.snapshot.resolve_automatic_profile(model, reasoning_effort)

    def validate_explicit_profile(self, model: str, reasoning_effort: str) -> None:
        self.snapshot.validate_explicit_profile(model, reasoning_effort)

    def rate_metadata_for(self, model: str) -> CatalogModelMetadata | None:
        return self.snapshot.rate_metadata_for(model)

    def launch_disposition(self, model: str, *, override_reason: str | None = None) -> str:
        return self.snapshot.launch_disposition(model, override_reason=override_reason)

    def reprice(
        self,
        model: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> CatalogPriceEstimate:
        return self.snapshot.reprice(
            model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    def inspection_payload(
        self,
        *,
        routing: Any = None,
        policy_first_candidates: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self._ensure_fresh()
        recent_obs = None
        first_seen_at_by_model: dict[str, str] = {}
        prior_observed_models: set[str] = set()
        has_prior_observations = False
        if self._observation_store is not None:
            try:
                retention_obs = self._observation_store.get_recent_observations(
                    window_days=self._observation_retention_days,
                    now=self.snapshot.fetched_at,
                )
                current_ver = self.snapshot.version
                prior_obs = [obs for obs in retention_obs if obs.get("version") != current_ver]
                if prior_obs:
                    has_prior_observations = True
                    for obs in prior_obs:
                        for m in obs.get("models", []):
                            if isinstance(m, dict) and isinstance(m.get("model"), str):
                                prior_observed_models.add(_normalize_model(m["model"]))

                for obs in reversed(retention_obs):
                    ts = obs.get("first_seen_at") or obs.get("last_seen_at")
                    for m in obs.get("models", []):
                        if isinstance(m, dict) and isinstance(m.get("model"), str):
                            norm = _normalize_model(m["model"])
                            if norm not in first_seen_at_by_model and ts:
                                first_seen_at_by_model[norm] = ts

                recent_obs = self._observation_store.get_recent_observations(
                    window_days=self._observation_window_days,
                    now=self.snapshot.fetched_at,
                )
            except (AttributeError, OSError, RuntimeError, ValueError):
                recent_obs = None

        merged_policy_candidates: dict[str, str] = {}
        if policy_first_candidates:
            merged_policy_candidates.update(policy_first_candidates)
        if routing is not None and hasattr(routing, "policy_names"):
            for pol_name in routing.policy_names:
                try:
                    ladder = routing.ladder_for_policy(pol_name)
                    if ladder:
                        merged_policy_candidates[pol_name] = ladder[0].model
                except ValueError:
                    pass

        return self.snapshot.inspection_payload(
            policy_first_candidates=merged_policy_candidates,
            recent_observations=recent_obs,
            first_seen_at_by_model=first_seen_at_by_model,
            newness_window_days=self._newness_window_days,
            has_prior_observations=has_prior_observations,
            prior_observed_models=prior_observed_models,
        )

    def quota_domain_for(self, model: str | None) -> str:
        return self.snapshot.quota_domain_for(model)

    def capacity_units_for(
        self,
        model: str,
        reasoning_effort: str,
        *,
        full_capacity: int,
    ) -> int:
        return self.snapshot.capacity_units_for(
            model,
            reasoning_effort,
            full_capacity=full_capacity,
        )


def _read_and_load_cache(
    cache_path: Path,
    *,
    max_cache_age_sec: float,
    now: float,
) -> tuple[
    str, dict[str, CatalogModel], str | None, str | None, str | None, str | None, str | None
]:
    try:
        modified_at = cache_path.stat().st_mtime
    except OSError:
        return "missing", {}, None, None, None, None, None

    if now - modified_at > max_cache_age_sec:
        return "stale", {}, None, None, None, None, None

    try:
        raw = cache_path.read_bytes()
        raw_sha = hashlib.sha256(raw).hexdigest()[:16]
    except OSError:
        return "invalid", {}, None, None, None, None, None

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return "invalid", {}, None, None, None, None, raw_sha

    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        return "invalid", {}, None, None, None, None, raw_sha

    models: dict[str, CatalogModel] = {}
    for item in value["models"]:
        parsed = _parse_model(item)
        if parsed is None:
            return "invalid", {}, None, None, None, None, raw_sha
        models[_normalize_model(parsed.model)] = parsed

    fetched_at = value.get("fetched_at") if isinstance(value.get("fetched_at"), str) else None
    etag = value.get("etag") if isinstance(value.get("etag"), str) else None
    client_version = (
        value.get("client_version") if isinstance(value.get("client_version"), str) else None
    )

    return "loaded", models, raw_sha, fetched_at, etag, client_version, raw_sha


def _load_cache(
    cache_path: Path,
    *,
    max_cache_age_sec: float,
    now: float,
) -> tuple[str, dict[str, CatalogModel], str | None]:
    cache_status, models, version, _, _, _, _ = _read_and_load_cache(
        cache_path,
        max_cache_age_sec=max_cache_age_sec,
        now=now,
    )
    return cache_status, models, version


def _parse_model(value: Any) -> CatalogModel | None:
    if not isinstance(value, dict):
        return None
    slug = value.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return None
    supported = _supported_reasoning_efforts(value.get("supported_reasoning_levels", []))
    if supported is None:
        return None
    default_effort = value.get("default_reasoning_level")
    if default_effort is not None and not isinstance(default_effort, str):
        return None
    priority = value.get("priority")
    if priority is not None and not isinstance(priority, int):
        return None
    return CatalogModel(
        model=slug.strip(),
        visible=_visible(value.get("visibility")),
        priority=priority,
        default_reasoning_effort=default_effort.strip().lower() if default_effort else None,
        supported_reasoning_efforts=supported,
    )


def _supported_reasoning_efforts(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    efforts: list[str] = []
    for item in value:
        effort = (
            item
            if isinstance(item, str)
            else item.get("effort")
            if isinstance(item, dict)
            else None
        )
        if not isinstance(effort, str) or not effort.strip():
            return None
        efforts.append(effort.strip().lower())
    return tuple(efforts)


def _metadata_by_model(
    metadata: tuple[CatalogModelMetadata, ...],
) -> dict[str, CatalogModelMetadata]:
    normalized: dict[str, CatalogModelMetadata] = {}
    for item in metadata:
        key = _normalize_model(item.model)
        if not key:
            raise ValueError("Model catalog metadata model must not be empty")
        if key in normalized:
            raise ValueError(f"Duplicate model catalog metadata: {item.model}")
        _validate_metadata(item)
        normalized[key] = item
    return normalized


def _validate_metadata(metadata: CatalogModelMetadata) -> None:
    if metadata.quota_domain is not None and not metadata.quota_domain.strip():
        raise ValueError(f"Model catalog quota domain must not be empty: {metadata.model}")
    for effort, units in metadata.capacity_units:
        if not effort.strip() or units <= 0:
            raise ValueError(f"Model catalog capacity metadata is invalid: {metadata.model}")
    rates = (metadata.credit_rate, metadata.api_usd_rate)
    if any(rate is not None for rate in rates) and (
        metadata.rate_card_version is None or metadata.rate_card_source is None
    ):
        raise ValueError(f"Model catalog rate metadata needs version and source: {metadata.model}")


def _visible(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"hide", "hidden", "disabled", "unavailable"}
    return True


def _normalize_model(model: str) -> str:
    return model.strip().lower()


def _estimate_rate(
    rate: CatalogRate | None,
    *,
    uncached_input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float | None:
    if rate is None:
        return None
    return (
        uncached_input_tokens * rate.input
        + max(0, cached_input_tokens) * rate.cached_input
        + max(0, output_tokens) * rate.output
    ) / 1_000_000


def _timestamp_to_iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _get_history_models(
    *,
    obs_store: Any,
    current_models: dict[str, CatalogModel],
    window_days: float,
    now: str,
) -> dict[str, tuple[CatalogModel, str]]:
    history: dict[str, tuple[CatalogModel, str]] = {}
    try:
        recent = obs_store.get_recent_observations(window_days=window_days, now=now)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return history
    for record in recent:
        last_seen = record.get("last_seen_at", now)
        obs_models = record.get("models", [])
        for m in obs_models:
            if not isinstance(m, dict):
                continue
            slug = m.get("model")
            if not isinstance(slug, str) or not slug.strip():
                continue
            norm = _normalize_model(slug)
            if norm not in current_models and norm not in history:
                efforts = m.get("supported_reasoning_efforts", ())
                if isinstance(efforts, list):
                    efforts = tuple(efforts)
                cat_model = CatalogModel(
                    model=slug.strip(),
                    visible=bool(m.get("visible", True)),
                    priority=m.get("priority") if isinstance(m.get("priority"), int) else None,
                    default_reasoning_effort=m.get("default_reasoning_effort"),
                    supported_reasoning_efforts=tuple(efforts),
                )
                history[norm] = (cat_model, last_seen)
    return history


def _is_version_regressed(current_ver: str, previous_ver: str) -> bool:
    import re

    def _parse(v: str) -> tuple[int, ...]:
        nums = re.findall(r"\d+", v)
        return tuple(int(n) for n in nums)

    try:
        return _parse(current_ver) < _parse(previous_ver)
    except (TypeError, ValueError):
        return False


def _is_within_newness_window(
    first_seen_at: str | None,
    reference_at: str | None,
    window_days: float,
) -> bool:
    if not first_seen_at or not reference_at:
        return True
    try:
        from datetime import UTC, datetime

        def _parse(ts: str) -> datetime:
            ts_norm = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_norm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt

        dt_first = _parse(first_seen_at)
        dt_ref = _parse(reference_at)
        diff_sec = (dt_ref - dt_first).total_seconds()
        return -86400.0 <= diff_sec <= (window_days * 86400.0)
    except (ValueError, TypeError):
        return True
