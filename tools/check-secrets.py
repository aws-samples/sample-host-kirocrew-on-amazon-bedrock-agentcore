from __future__ import annotations

import re
import sys
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".venv",
    "node_modules",
    # Local, untracked working directories: scratch notes and scan reports under
    # temp/, and the generated publication bundle under _public/ which is a copy
    # of files already scanned in place.
    "temp",
    "_public",
}
EXCLUDED_FILES = {Path("tools/check-secrets.py")}
PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "credential assignment": re.compile(
        r"(?i)\b(?:password|passwd|secret|client_secret)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
    ),
}


def candidate_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
        and path.relative_to(root) not in EXCLUDED_FILES
        and path.stat().st_size <= 2_000_000
    )


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[str] = []
    for path in candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(root)}:{line_number}: {label}")
    if findings:
        print("Potential secrets detected:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print(f"Secret scan passed ({len(candidate_files(root))} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
