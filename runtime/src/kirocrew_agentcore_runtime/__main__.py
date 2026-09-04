from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from kirocrew_agentcore_runtime import PACKAGE_NAME
from kirocrew_agentcore_runtime.image_runtime import ImageMetadata, smoke_gateway


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog=PACKAGE_NAME)
    parser.add_argument("command", choices=("diagnostics", "serve", "smoke"))
    parsed = parser.parse_args(arguments)
    if parsed.command == "serve":
        from kirocrew_agentcore_runtime.aws_runtime import serve_runtime

        serve_runtime()
        return
    metadata = ImageMetadata.from_environment()
    result = metadata.diagnostics() if parsed.command == "diagnostics" else smoke_gateway(metadata)
    print(json.dumps({"component": PACKAGE_NAME, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
