from __future__ import annotations

import hashlib
import json

from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModel,
    CatalogModelMetadata,
    CatalogRate,
    ModelCatalog,
)
from agent_control_plane.features.agent_runner.lib.model_routing import ModelProfile
from agent_control_plane.shared.config import ClaudeModelCatalogConfig

CLAUDE_CATALOG_LABEL = "Claude"
CLAUDE_CATALOG_SOURCE = "claude-builtin"

_FULL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_LEGACY_EFFORTS = ("low", "medium", "high", "max")
_BASIC_EFFORTS = ("low", "medium", "high")

# Claude Code has no models_cache.json analog, so the inventory is
# controller-owned and must stay byte-stable for routing-evidence identity.
CLAUDE_BUILTIN_MODELS: tuple[CatalogModel, ...] = (
    CatalogModel(
        model="claude-opus-4-8",
        visible=True,
        priority=1,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_FULL_EFFORTS,
    ),
    CatalogModel(
        model="claude-sonnet-5",
        visible=True,
        priority=2,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_FULL_EFFORTS,
    ),
    CatalogModel(
        model="claude-fable-5",
        visible=True,
        priority=3,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_FULL_EFFORTS,
    ),
    CatalogModel(
        model="claude-opus-4-7",
        visible=True,
        priority=4,
        default_reasoning_effort="xhigh",
        supported_reasoning_efforts=_FULL_EFFORTS,
    ),
    CatalogModel(
        model="claude-opus-4-6",
        visible=True,
        priority=5,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_LEGACY_EFFORTS,
    ),
    CatalogModel(
        model="claude-sonnet-4-6",
        visible=True,
        priority=6,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_LEGACY_EFFORTS,
    ),
    CatalogModel(
        model="claude-haiku-4-5",
        visible=True,
        priority=7,
        default_reasoning_effort="high",
        supported_reasoning_efforts=_BASIC_EFFORTS,
    ),
)


def build_claude_model_catalog(config: ClaudeModelCatalogConfig) -> ModelCatalog:
    models: dict[str, CatalogModel] = {
        model.model.strip().lower(): model for model in CLAUDE_BUILTIN_MODELS
    }
    for item in config.inventory:
        parsed = CatalogModel(
            model=item.model.strip(),
            visible=item.visible,
            priority=item.priority,
            default_reasoning_effort=(
                item.default_reasoning_effort.strip().lower()
                if item.default_reasoning_effort
                else None
            ),
            supported_reasoning_efforts=tuple(
                effort.strip().lower() for effort in item.supported_reasoning_efforts
            ),
        )
        if not parsed.model or not parsed.supported_reasoning_efforts:
            raise ValueError(
                f"Claude model inventory entry needs a model and supported efforts: {item.model!r}"
            )
        models[parsed.model.lower()] = parsed
    metadata = {
        item.model.strip().lower(): CatalogModelMetadata(
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
    }
    return ModelCatalog(
        models=models,
        metadata=metadata,
        cache_status="loaded",
        source=CLAUDE_CATALOG_SOURCE,
        version=_inventory_version(models),
        label=CLAUDE_CATALOG_LABEL,
    )


def claude_ladder_for_explicit_model(
    catalog: ModelCatalog,
    model: str,
    reasoning_effort: str,
) -> tuple[ModelProfile, ...]:
    """Resolve one fixed Claude profile with the same rules as the Codex path."""
    resolved_model = model.strip()
    effort = reasoning_effort.strip().lower()
    if not resolved_model:
        raise ValueError("Claude model must not be empty")
    if not effort:
        raise ValueError("Claude reasoning effort must not be empty")
    if resolved_model.lower() == "default":
        resolved_model = catalog.resolve_automatic_profile(resolved_model, effort)
    else:
        catalog.validate_explicit_profile(resolved_model, effort)
    return (ModelProfile(model=resolved_model, reasoning_effort=effort),)


def _inventory_version(models: dict[str, CatalogModel]) -> str:
    canonical = json.dumps(
        [
            {
                "model": model.model,
                "visible": model.visible,
                "priority": model.priority,
                "default_reasoning_effort": model.default_reasoning_effort,
                "supported_reasoning_efforts": list(model.supported_reasoning_efforts),
            }
            for _, model in sorted(models.items())
        ],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
