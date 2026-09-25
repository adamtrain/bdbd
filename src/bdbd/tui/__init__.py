"""bdbd's full-screen app (Textual). `bdbd` with no command opens it."""

from __future__ import annotations

from pathlib import Path


def run(path: Path, *, tidy: bool = True) -> None:
    """Open the app on the budget at `path` (offering to create it when it's missing)."""
    from bdbd.tui.app import BdbdApp

    BdbdApp(path, tidy=tidy).run()
