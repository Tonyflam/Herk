"""Shared pytest fixtures for the HELM test suite."""

from __future__ import annotations

import os

import pytest

from helm.config import load_settings

# ---------------------------------------------------------------------------
# Hermetic arming environment.
#
# ``helm.config`` auto-loads ``.env`` at import time. During the live contest
# that file is *armed* (``HELM_MODE=live`` + the execute trio + ``HELM_QUOTE_ONLY=0``),
# so every ``load_settings()`` in the suite would otherwise inherit live mode and
# break the paper / dry-run invariants these tests pin. Mirror the profile pin in
# the ``settings`` fixture below and force the arming knobs back to their safe
# defaults here — at import, right after ``.env`` was loaded — so the suite is
# deterministic whether or not the bot is armed. Tests that exercise a live path
# arm it explicitly (in-memory settings, or their own ``monkeypatch`` env — see
# ``tests/test_arming.py``), which composes correctly on top of these defaults.
# ---------------------------------------------------------------------------
os.environ["HELM_MODE"] = "paper"
os.environ["HELM_EXECUTE_TRADES"] = "0"
os.environ["HELM_EXECUTE_CHAIN"] = "0"
os.environ["HELM_QUOTE_ONLY"] = "1"
os.environ["HELM_EXECUTION_ADAPTER"] = "paper"


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

