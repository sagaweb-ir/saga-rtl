"""Clipboard access service.

A thin, dependency-injected wrapper around :mod:`pyperclip` so the rest of
the application never talks to the clipboard backend directly. Windows may
briefly lock the clipboard while another process owns it, so reads retry a
couple of times before giving up.
"""

from __future__ import annotations

import time
from typing import Optional

import pyperclip

_READ_ATTEMPTS = 4
_RETRY_DELAY_S = 0.02


class ClipboardService:
    """Reads and writes plain text on the system clipboard."""

    def read_text(self) -> str:
        """Return the current clipboard text (``""`` when empty/unavailable)."""

        last_error: Optional[Exception] = None
        for _ in range(_READ_ATTEMPTS):
            try:
                text = pyperclip.paste()
            except pyperclip.PyperclipException as exc:  # pragma: no cover
                last_error = exc
                time.sleep(_RETRY_DELAY_S)
                continue
            return text or ""
        if last_error is not None:  # pragma: no cover
            print(f"[rtl-viewer] clipboard read failed: {last_error}")
        return ""

    def write_text(self, text: str) -> bool:
        """Put *text* on the clipboard; report whether it succeeded."""

        try:
            pyperclip.copy(text)
            return True
        except pyperclip.PyperclipException:  # pragma: no cover
            return False
