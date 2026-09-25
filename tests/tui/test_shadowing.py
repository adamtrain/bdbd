"""No widget of ours may reuse an attribute name Textual uses itself.

Textual keeps its state in plain attributes (`MessagePump._closed`, `._running`, …). A widget
that sets one of its own by the same name silently breaks Textual: a card that set `_closed`
could never be removed, and dismissing it hung the app.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import pkgutil
import re

from textual.message_pump import MessagePump

import bdbd.tui
import bdbd.tui.views

# Textual's public attributes that are meant to be set
MEANT_TO_BE_SET = {
    "border_subtitle",
    "border_title",
    "can_focus",
    "disabled",
    "display",
    "highlighted",
    "loading",
    "styles",
    "theme",
    "tooltip",
}

ASSIGNED = re.compile(r"self\.([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)")


def _ours() -> list[type]:
    classes: list[type] = []
    for package in (bdbd.tui, bdbd.tui.views):
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{package.__name__}.{info.name}")
            classes += [
                cls
                for _, cls in inspect.getmembers(module, inspect.isclass)
                if cls.__module__ == module.__name__ and issubclass(cls, MessagePump)
            ]
    return classes


def _textuals(cls: type) -> set[str]:
    names: set[str] = set()
    for base in cls.__mro__:
        if base.__module__.startswith("textual"):
            names |= set(vars(base))
            with contextlib.suppress(OSError, TypeError):  # no source to read
                names |= set(ASSIGNED.findall(inspect.getsource(base)))
    return names


def test_no_widget_shadows_textual() -> None:
    classes = _ours()
    assert len(classes) > 30  # the search found the app's widgets
    clashes: dict[str, list[str]] = {}
    for cls in classes:
        assigned = set(ASSIGNED.findall(inspect.getsource(cls)))
        if names := (assigned & _textuals(cls)) - MEANT_TO_BE_SET:
            clashes[cls.__qualname__] = sorted(names)
    assert clashes == {}
