from __future__ import annotations

from importlib import import_module

import pytest
from kirocrew_agentcore_headless import main as headless_main

EXPECTED_PACKAGES = {
    "kirocrew_agentcore_adapter": "kirocrew-agentcore-adapter",
    "kirocrew_agentcore_control": "kirocrew-agentcore-control",
    "kirocrew_agentcore_headless": "kirocrew-agentcore-headless",
    "kirocrew_agentcore_persistence": "kirocrew-agentcore-persistence",
    "kirocrew_agentcore_runtime": "kirocrew-agentcore-runtime",
}


def test_python_workspace_members_are_installed() -> None:
    discovered = {
        module_name: import_module(module_name).PACKAGE_NAME for module_name in EXPECTED_PACKAGES
    }
    assert discovered == EXPECTED_PACKAGES


def test_headless_cli_reports_workspace_status(capsys: pytest.CaptureFixture[str]) -> None:
    headless_main()
    assert capsys.readouterr().out == (
        '{"component": "kirocrew-agentcore-headless", "status": "workspace-ready"}\n'
    )
