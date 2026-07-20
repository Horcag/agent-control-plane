import pytest

from agent_control_plane.features.agent_runner.lib.claude_model_catalog import (
    CLAUDE_CATALOG_SOURCE,
    build_claude_model_catalog,
    claude_ladder_for_explicit_model,
)
from agent_control_plane.shared.config import (
    ClaudeModelCatalogConfig,
    ClaudeModelInventoryConfig,
    CodexModelMetadataConfig,
    CodexTokenRateConfig,
)


def _catalog(config: ClaudeModelCatalogConfig | None = None):
    return build_claude_model_catalog(config or ClaudeModelCatalogConfig())


def test_builtin_inventory_is_loaded_with_stable_identity() -> None:
    catalog = _catalog()
    assert catalog.cache_status == "loaded"
    assert catalog.source == CLAUDE_CATALOG_SOURCE
    assert catalog.label == "Claude"
    assert catalog.version == _catalog().version


def test_default_selector_resolves_to_highest_priority_visible_model() -> None:
    profile = claude_ladder_for_explicit_model(_catalog(), "default", "high")[0]
    assert profile.model == "claude-opus-4-8"
    assert profile.reasoning_effort == "high"


def test_full_effort_range_is_accepted_for_current_models() -> None:
    catalog = _catalog()
    for effort in ("low", "medium", "high", "xhigh", "max"):
        profile = claude_ladder_for_explicit_model(catalog, "claude-sonnet-5", effort)[0]
        assert profile.reasoning_effort == effort


def test_unsupported_effort_is_rejected_with_claude_wording() -> None:
    with pytest.raises(ValueError, match="Claude model 'claude-haiku-4-5' does not support"):
        claude_ladder_for_explicit_model(_catalog(), "claude-haiku-4-5", "xhigh")


def test_unknown_explicit_model_passes_through_like_codex() -> None:
    profile = claude_ladder_for_explicit_model(_catalog(), "claude-future-6", "turbo")[0]
    assert profile.model == "claude-future-6"
    assert profile.reasoning_effort == "turbo"


def test_blank_model_and_effort_are_rejected() -> None:
    with pytest.raises(ValueError, match="Claude model must not be empty"):
        claude_ladder_for_explicit_model(_catalog(), "  ", "high")
    with pytest.raises(ValueError, match="Claude reasoning effort must not be empty"):
        claude_ladder_for_explicit_model(_catalog(), "claude-sonnet-5", "  ")


def test_inventory_override_extends_and_changes_version() -> None:
    base = _catalog()
    catalog = _catalog(
        ClaudeModelCatalogConfig(
            inventory=(
                ClaudeModelInventoryConfig(
                    model="claude-nova-7",
                    priority=0,
                    default_reasoning_effort="high",
                    supported_reasoning_efforts=("low", "high"),
                ),
            ),
        )
    )
    assert catalog.version != base.version
    profile = claude_ladder_for_explicit_model(catalog, "default", "high")[0]
    assert profile.model == "claude-nova-7"
    with pytest.raises(ValueError, match="does not support reasoning effort 'max'"):
        claude_ladder_for_explicit_model(catalog, "claude-nova-7", "max")


def test_metadata_rate_card_reprices_claude_usage() -> None:
    catalog = _catalog(
        ClaudeModelCatalogConfig(
            models=(
                CodexModelMetadataConfig(
                    model="claude-opus-4-8",
                    premium=True,
                    quota_domain=None,
                    capacity_units=(),
                    credit_rate=None,
                    api_usd_rate=CodexTokenRateConfig(
                        input=5.0,
                        cached_input=0.5,
                        output=25.0,
                    ),
                    rate_card_version="2026-07",
                    rate_card_source="operator",
                ),
            ),
        )
    )
    estimate = catalog.reprice(
        "claude-opus-4-8",
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=100_000,
    )
    assert estimate.estimated_api_usd == pytest.approx(600_000 * 5.0 / 1e6 + 0.2 + 2.5)
    assert estimate.rate_card_version == "2026-07"
    metadata = catalog.rate_metadata_for("claude-opus-4-8")
    assert metadata is not None and metadata.premium is True
