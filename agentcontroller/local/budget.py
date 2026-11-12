"""Token arithmetic done by code, never by the model."""
from __future__ import annotations

import subprocess

from .. import NO_WINDOW
from pathlib import Path

from .config import Model


def tokens(text: str, m: Model) -> int:
    return int(len(text) / m.chars_per_token * 1.15) + 1  # 15% margin for dense code


def file_tokens(root: Path, rel: str, m: Model) -> int:
    p = root / rel
    return tokens(p.read_text(encoding="utf-8", errors="replace"), m) if p.is_file() else 0


def repo_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True, **NO_WINDOW).stdout
    return [f for f in out.splitlines() if f and not f.startswith(".") and (root / f).stat().st_size < 400_000]


def annotated_map(root: Path, m: Model, files: list[str] | None = None) -> str:
    """'path  1.2k tok' lines, so the model can size stages."""
    lines = []
    for f in files or repo_files(root):
        t = file_tokens(root, f, m)
        lines.append(f"{f}  {t / 1000:.1f}k tok")
    return "\n".join(lines)


def preflight(input_tokens: int, max_output: int, m: Model) -> str | None:
    """None if the call fits the window, else why not."""
    total = input_tokens + max_output + m.reserve
    if total > m.context_tokens:
        return f"needs ~{total} tokens (input {input_tokens} + output {max_output} + reserve {m.reserve}) > {m.context_tokens}"
    return None
