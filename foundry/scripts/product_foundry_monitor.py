#!/usr/bin/env python3
"""Hermes monitor wrapper that emits stable meaningful Foundry signal bytes."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def repository_root() -> Path:
    candidates: list[Path] = []
    explicit = os.environ.get("HERMES_PRODUCT_FOUNDRY_ROOT")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    cwd = Path.cwd()
    candidates.extend((cwd, cwd.parent))
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate / "foundry" / "src" / "orchestrator.py").is_file():
            return candidate
        if candidate.name == "foundry" and (candidate / "src" / "orchestrator.py").is_file():
            return candidate.parent
    raise RuntimeError("run this wrapper with the repository's foundry directory as its workdir")


ROOT = repository_root()
sys.path.insert(0, str(ROOT))

from foundry.src.orchestrator import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["--repo-root", str(ROOT), "monitor"]))
