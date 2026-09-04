from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.contract
def test_frontend_declares_external_protocol_version() -> None:
    root = Path(__file__).parents[2]
    source = (root / "frontend-shell/src/index.ts").read_text(encoding="utf-8")
    assert json.dumps("kirocrew-agentcore.v1") in source
