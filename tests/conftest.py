"""Shared unit-test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _preserve_cwd() -> Iterator[None]:
    """The supervisor re-anchors the process into the workspace on start.

    Tests that exercise it would otherwise leak a temporary working
    directory into later tests (or leave the process inside a deleted
    tmp path), so every unit test gets its working directory restored.
    """
    cwd = Path.cwd()
    yield
    os.chdir(cwd)
