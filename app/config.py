"""Load local .env into the environment, once, at import.

Cloud Run injects configuration as real environment variables, so this file does
nothing in production -- `setdefault` means a real env var always wins. It exists so
local development doesn't require exporting secrets into your shell on every run.

`.env` is gitignored. Nothing here ever reads a file that gets committed.
"""

from __future__ import annotations

import os
import pathlib

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load(path: pathlib.Path = ENV_FILE) -> list[str]:
    """Apply KEY=VALUE lines from `path`. Returns the names it set."""
    if not path.exists():
        return []

    applied = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and os.environ.get(key) is None:
            os.environ[key] = value
            applied.append(key)
    return applied


load()
