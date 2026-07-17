from __future__ import annotations

import json
import time
from dataclasses import dataclass
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


class ModelCatalog:
    """Read-only merger of the local Codex inventory and ACP-owned metadata."""

    def __init__(
        self,
        *,
        models: dict[str, CatalogModel],
        metadata: dict[str, CatalogModelMetadata],
        cache_status: str,
    ) -> None:
        self._models = models
        self._metadata = metadata
        self.cache_status = cache_status

    @classmethod
    def load(
        cls,
        *,
        cache_path: Path,
        max_cache_age_sec: float,
        metadata: tuple[CatalogModelMetadata, ...] = (),
        now: float | None = None,
    ) -> ModelCatalog:
        normalized_metadata = _metadata_by_model(metadata)
        cache_status, models = _load_cache(
            cache_path,
            max_cache_age_sec=max_cache_age_sec,
            now=time.time() if now is None else now,
        )
        return cls(models=models, metadata=normalized_metadata, cache_status=cache_status)

    @classmethod
    def from_config(cls, config: CodexModelCatalogConfig) -> ModelCatalog:
        return cls.load(
            cache_path=config.cache_path,
            max_cache_age_sec=config.max_cache_age_sec,
            metadata=tuple(
                CatalogModelMetadata(
                    model=item.model,
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
        )

    def model(self, model: str) -> CatalogModel | None:
        return self._models.get(_normalize_model(model))

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
                    "Codex model catalog is "
                    f"{self.cache_status}; automatic routing needs a current cache inventory"
                )
            if normalized_model == "default":
                raise ValueError(
                    "Codex model selector 'default' could not resolve to a visible candidate "
                    "in the current model catalog"
                )
            raise ValueError(
                f"Codex model {model!r} is not a visible candidate in the current model catalog"
            )
        if not candidate.visible:
            raise ValueError(f"Codex model {model!r} is not visible in the current model catalog")
        return candidate

    def _default_visible_candidate(self) -> CatalogModel | None:
        candidates = tuple(model for model in self._models.values() if model.visible)
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

    def rate_metadata_for(self, model: str) -> CatalogModelMetadata | None:
        return self._metadata.get(_normalize_model(model))

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

    @staticmethod
    def _validate_known_effort(candidate: CatalogModel, reasoning_effort: str) -> None:
        effort = reasoning_effort.strip().lower()
        if not effort:
            raise ValueError("Codex reasoning effort must not be empty")
        supported = candidate.supported_reasoning_efforts
        if effort in supported:
            return
        allowed = ", ".join(supported) or "none declared by the catalog"
        raise ValueError(
            f"Codex model {candidate.model!r} does not support reasoning effort {effort!r}. "
            f"Expected one of: {allowed}"
        )


def _load_cache(
    cache_path: Path,
    *,
    max_cache_age_sec: float,
    now: float,
) -> tuple[str, dict[str, CatalogModel]]:
    try:
        modified_at = cache_path.stat().st_mtime
    except OSError:
        return "missing", {}
    if now - modified_at > max_cache_age_sec:
        return "stale", {}
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", {}
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        return "invalid", {}
    models: dict[str, CatalogModel] = {}
    for item in value["models"]:
        parsed = _parse_model(item)
        if parsed is None:
            return "invalid", {}
        models[_normalize_model(parsed.model)] = parsed
    return "loaded", models


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
