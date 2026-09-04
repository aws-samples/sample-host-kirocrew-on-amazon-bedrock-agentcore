from __future__ import annotations

import json

PACKAGE_NAME = "kirocrew-agentcore-headless"


def main() -> None:
    print(json.dumps({"component": PACKAGE_NAME, "status": "workspace-ready"}))
