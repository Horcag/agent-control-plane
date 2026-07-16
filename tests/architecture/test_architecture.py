from pathlib import Path

import pytest_fsd
from pytest_fsd import validate_fsd_architecture
from pytest_fsd.config import load_config


def test_project_architecture(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(str(project_root))

    # pytest-fsd uses base_path both as an import package and as a filesystem root.
    # In a src-layout project, the package root is one level below the repo root.
    monkeypatch.setattr(pytest_fsd, "load_config", lambda _: config)

    validate_fsd_architecture(str(project_root / "src"))
