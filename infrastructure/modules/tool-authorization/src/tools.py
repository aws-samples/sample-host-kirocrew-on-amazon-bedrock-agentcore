"""Two demo tools behind the tool-authorization Gateway (phase 1 of #40).

They do nothing useful on purpose: the point of phase 1 is to prove that the
Gateway's Cedar policies tell the two groups apart, so each tool only reports
which tool ran and who the Gateway says called it.
"""

from __future__ import annotations

from typing import Any

_TOOL_SEPARATOR = "___"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    qualified = str(custom.get("bedrockAgentCoreToolName", ""))
    tool = qualified.split(_TOOL_SEPARATOR, 1)[-1]
    if tool == "finance_report":
        return {"tool": tool, "result": "finance report (demo data)"}
    if tool == "eng_status":
        return {"tool": tool, "result": "engineering status (demo data)"}
    return {"error": f"unknown tool {qualified!r}"}
