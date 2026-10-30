from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SOURCE = Path(__file__).parents[2] / "infrastructure/modules/tool-authorization/src/tools.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("demo_tools", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(tool: str | None) -> Any:
    custom = None if tool is None else {"bedrockAgentCoreToolName": tool}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def test_each_demo_tool_answers_under_its_gateway_name() -> None:
    tools = _load()
    assert tools.handler({}, _context("demo-tools___finance_report"))["tool"] == "finance_report"
    assert tools.handler({}, _context("demo-tools___eng_status"))["tool"] == "eng_status"


def test_unknown_or_missing_tool_name_is_an_error_not_a_default() -> None:
    tools = _load()
    assert "error" in tools.handler({}, _context("demo-tools___other"))
    assert "error" in tools.handler({}, _context(None))
    assert "error" in tools.handler({}, SimpleNamespace(client_context=None))
