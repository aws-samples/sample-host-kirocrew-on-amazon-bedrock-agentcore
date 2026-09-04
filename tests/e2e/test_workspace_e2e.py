from __future__ import annotations

import os

import pytest


@pytest.mark.e2e
def test_e2e_profile_is_explicit_and_supported() -> None:
    assert os.environ["DEPLOYMENT_MODE"] in {"microvm", "instances"}
