from __future__ import annotations

from agent_control_plane.shared.agent_backends import globally_disabled_backends


def test_globally_disabled_backends_normalizes_aliases(monkeypatch) -> None:
    monkeypatch.setenv("ACP_DISABLED_BACKENDS", " claude-code, agy,claude ")

    assert globally_disabled_backends() == frozenset({"claude", "agy"})


def test_globally_disabled_backends_is_empty_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("ACP_DISABLED_BACKENDS", raising=False)

    assert globally_disabled_backends() == frozenset()
