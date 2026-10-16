"""Fail closed on unexpected files in the Git index; never print file contents."""
from __future__ import annotations

from pathlib import PurePosixPath
import subprocess
import sys


PUBLIC_FILES = {
    ".gitignore", ".env.example", "README.md", "LICENSE", "SECURITY.md",
    "pyproject.toml", "docs/architecture.md", "docs/operations.md",
    "docs/research/hermes.md", "docs/research/recommendation_resilience.md",
    ".githooks/pre-commit", ".github/workflows/tests.yml",
}


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if name in PUBLIC_FILES:
        return True
    if path.is_absolute() or ".." in path.parts or any(p.startswith(".") for p in path.parts):
        return False
    return (
        path.parts[0] in {"app", "tests", "scripts"}
        and path.suffix in {".py", ".sh"}
        and not any(part in {"__pycache__", "secrets", "private", "backups"} for part in path.parts)
    )


def main() -> int:
    rows = subprocess.check_output(["git", "ls-files", "--stage", "-z"]).decode().split("\0")
    rejected = []
    for row in filter(None, rows):
        metadata, name = row.split("\t", 1)
        if not allowed_path(name) or metadata.split()[0] not in {"100644", "100755"}:
            rejected.append(name)
    if rejected:
        print("Public-tree check rejected paths:\n" + "\n".join(rejected), file=sys.stderr)
        return 1
    print("Public-tree boundary check passed (index paths only; run secret scanning too).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
