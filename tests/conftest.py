"""Shared pytest fixtures for the HELM test suite."""

from __future__ import annotations

import pytest

from helm.config import load_settings


@pytest.fixture(scope="session")
def settings():
    """Real settings loaded from config/settings.yaml (paper-mode defaults)."""
    return load_settings()
