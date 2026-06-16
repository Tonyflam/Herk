"""Shared pytest fixtures for the HELM test suite."""

from __future__ import annotations

import os

import pytest

from helm.config import load_settings


@pytest.fixture(scope="session")
def settings():
    """Deterministic base settings for unit tests.

    Pins the *balanced* profile regardless of any ambient ``HELM_PROFILE`` or
    ``.env`` value (``helm.config`` auto-loads ``.env``), so the suite is
    reproducible on every machine — including when the live contest profile
    (``max``) is pre-staged in ``.env``. Tests that exercise a specific profile
    load it explicitly (e.g. via ``dataclasses.replace``) instead of relying on
    this fixture.
    """
    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "balanced"
    try:
        return load_settings()
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev

